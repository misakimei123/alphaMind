"""OpenAI-compatible provider：结构化输出、有限重试与 fail-closed。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

import openai
import yaml
from openai import OpenAI
from openai.types.chat import ChatCompletion
from openai.types.responses import Response

from alphamind.ai.usage import (
    BudgetExceededError,
    CostPolicy,
    Usage,
    UsageLedger,
    UsageLedgerError,
    UsageSummary,
)
from alphamind.config import EffectiveConfig
from alphamind.decision import (
    BoundDecisionContext,
    BoundModelDecision,
    ContractErrorCode,
    ContractValidationError,
    DecisionContractBinder,
)

JsonObject = dict[str, Any]


class ProviderErrorCode(StrEnum):
    API_KEY_MISSING = "api_key_missing"  # pragma: allowlist secret
    AUTHENTICATION_ERROR = "authentication_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    BUSINESS_VALIDATION_ERROR = "business_validation_error"
    INPUT_TOO_LARGE = "input_too_large"
    MALFORMED_STRUCTURED_OUTPUT = "malformed_structured_output"
    POLICY_REFUSAL = "policy_refusal"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    USAGE_INVALID = "usage_invalid"


class ResponsesResource(Protocol):
    def create(self, **kwargs: Any) -> Response: ...


class ChatCompletionsResource(Protocol):
    def create(self, **kwargs: Any) -> ChatCompletion: ...


class ChatResource(Protocol):
    completions: ChatCompletionsResource


class ProviderClient(Protocol):
    responses: ResponsesResource
    chat: ChatResource


def _provider_error(error: openai.APIError) -> tuple[ProviderErrorCode, str | None]:
    request_id = error.request_id if isinstance(error, openai.APIStatusError) else None
    if isinstance(error, openai.APITimeoutError):
        return ProviderErrorCode.TIMEOUT, request_id
    if isinstance(error, openai.APIStatusError) and error.status_code == 402:
        return ProviderErrorCode.BUDGET_EXCEEDED, request_id
    if isinstance(error, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ProviderErrorCode.AUTHENTICATION_ERROR, request_id
    if isinstance(error, openai.RateLimitError):
        body = error.body
        provider_code = ""
        if isinstance(body, dict):
            provider_code = str(body.get("code", ""))
            nested = body.get("error")
            if not provider_code and isinstance(nested, dict):
                provider_code = str(nested.get("code", ""))
        if provider_code in {"insufficient_quota", "billing_not_active"}:
            return ProviderErrorCode.BUDGET_EXCEEDED, request_id
        return ProviderErrorCode.RATE_LIMIT, request_id
    if isinstance(error, openai.InternalServerError) or (
        isinstance(error, openai.APIStatusError) and error.status_code >= 500
    ):
        return ProviderErrorCode.SERVER_ERROR, request_id
    return ProviderErrorCode.PROVIDER_ERROR, request_id


@dataclass(frozen=True, slots=True)
class ProviderResult:
    status: str
    decision: BoundModelDecision | None
    error_code: ProviderErrorCode | None
    model_id: str
    response_id: str | None
    request_id: str | None
    prompt_sha256: str
    config_sha256: str
    input_sha256: str
    usage_summary: UsageSummary

    def to_safe_dict(self) -> JsonObject:
        return {
            "status": self.status,
            "error_code": self.error_code.value if self.error_code else None,
            "model_id": self.model_id,
            "response_id": self.response_id,
            "request_id": self.request_id,
            "prompt_sha256": self.prompt_sha256,
            "config_sha256": self.config_sha256,
            "input_sha256": self.input_sha256,
            "usage": self.usage_summary.to_dict(),
            "decision": self.decision.document if self.decision else None,
        }


class _ResponseFailure(RuntimeError):
    def __init__(
        self,
        code: ProviderErrorCode,
        *,
        usage: Usage | None = None,
        response_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        self.code = code
        self.usage = usage
        self.response_id = response_id
        self.model_id = model_id
        super().__init__(code.value)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_yaml_object(path: Path, *, label: str) -> JsonObject:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError(f"{label} could not be loaded") from None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _provider_schema(value: object) -> object:
    """移除 API 生成子集未承诺的断言；R2-01 仍执行完整 schema。"""

    if isinstance(value, dict):
        return {
            key: _provider_schema(item)
            for key, item in value.items()
            if key not in {"$schema", "$id", "$comment", "title", "uniqueItems"}
        }
    if isinstance(value, list):
        return [_provider_schema(item) for item in value]
    return value


def _validate_provider_schema(value: object, *, location: str = "schema") -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            properties = value.get("properties")
            required = value.get("required")
            if not isinstance(properties, dict) or value.get("additionalProperties") is not False:
                raise ValueError(f"{location} is not strict-object compatible")
            if not isinstance(required, list) or set(required) != set(properties):
                raise ValueError(f"{location} must require every property")
        for key, item in value.items():
            _validate_provider_schema(item, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_provider_schema(item, location=f"{location}.{index}")


def _usage(response: Response) -> Usage:
    raw = response.usage
    if raw is None:
        raise _ResponseFailure(ProviderErrorCode.USAGE_INVALID)
    cached = raw.input_tokens_details.cached_tokens
    try:
        return Usage(raw.input_tokens, cached, raw.output_tokens)
    except ValueError:
        raise _ResponseFailure(ProviderErrorCode.USAGE_INVALID) from None


def _chat_usage(response: ChatCompletion) -> Usage:
    raw = response.usage
    if raw is None or raw.total_tokens != raw.prompt_tokens + raw.completion_tokens:
        raise _ResponseFailure(ProviderErrorCode.USAGE_INVALID)
    cached = getattr(raw, "prompt_cache_hit_tokens", None)
    if not isinstance(cached, int):
        details = raw.prompt_tokens_details
        cached = details.cached_tokens if details and details.cached_tokens is not None else 0
    try:
        return Usage(raw.prompt_tokens, cached, raw.completion_tokens)
    except ValueError:
        raise _ResponseFailure(ProviderErrorCode.USAGE_INVALID) from None


def _output_text(response: Response, usage: Usage, response_id: str, model_id: str) -> str:
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "refusal":
                    raise _ResponseFailure(
                        ProviderErrorCode.POLICY_REFUSAL,
                        usage=usage,
                        response_id=response_id,
                        model_id=model_id,
                    )
    output_text = response.output_text
    if not output_text:
        raise _ResponseFailure(
            ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT,
            usage=usage,
            response_id=response_id,
            model_id=model_id,
        )
    return output_text


class OpenAICompatibleProvider:
    def __init__(
        self,
        effective: EffectiveConfig,
        *,
        usage_ledger: UsageLedger,
        client: ProviderClient | None = None,
        environ: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.effective = effective
        self.profile = effective.ai_profile
        self.usage_ledger = usage_ledger
        self.client = client
        self.environ = dict(os.environ if environ is None else environ)
        self.sleep = sleep
        provider_key = (
            str(self.profile["provider"]["id"]),
            str(self.profile["provider"]["api"]),
        )
        allowed_endpoints = {
            ("openai", "responses"): "https://api.openai.com/v1",
            ("deepseek", "chat_completions"): "https://api.deepseek.com",
        }
        if self.profile["provider"]["base_url"] != allowed_endpoints.get(provider_key):
            raise ValueError("AI provider base URL is not allowed")
        self.binder = DecisionContractBinder(effective)
        prompt_path = effective.project_root / self.profile["prompt"]["path"]
        schema_path = effective.project_root / self.profile["structured_output"]["schema_path"]
        self.prompt = prompt_path.read_text(encoding="utf-8")
        schema = _load_yaml_object(schema_path, label="model decision schema")
        prepared = _provider_schema(schema)
        if not isinstance(prepared, dict):
            raise ValueError("provider schema must be an object")
        _validate_provider_schema(prepared)
        self.schema = prepared

    def request_payload(self, context: BoundDecisionContext) -> JsonObject:
        context_json = _canonical_json(context.document)
        if self.profile["provider"]["api"] == "chat_completions":
            # DeepSeek JSON Output 只保证合法 JSON，不承诺服务端 JSON Schema 约束；
            # 因此把 schema 明确放入 system message，并在响应后继续执行完整本地 binder。
            system_message = (
                self.prompt
                + "\n\nReturn exactly one JSON object matching this JSON Schema. "
                + "Do not add markdown fences or commentary.\n"
                + _canonical_json(self.schema)
            )
            return {
                "model": self.profile["model"]["id"],
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": context_json},
                ],
                "max_tokens": self.profile["request"]["max_output_tokens"],
                "temperature": self.profile["model"]["temperature"],
                "response_format": {"type": "json_object"},
                "stream": False,
                "extra_body": {"thinking": {"type": self.profile["model"]["thinking"]}},
            }
        return {
            "model": self.profile["model"]["id"],
            "instructions": self.prompt,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": context_json}],
                }
            ],
            "max_output_tokens": self.profile["request"]["max_output_tokens"],
            "reasoning": {"effort": self.profile["model"]["reasoning_effort"]},
            "text": {
                "verbosity": self.profile["model"]["verbosity"],
                "format": {
                    "type": "json_schema",
                    "name": self.profile["structured_output"]["schema_name"],
                    "strict": True,
                    "schema": deepcopy(self.schema),
                },
            },
            "tools": [],
            "tool_choice": "none",
            "store": False,
            "background": False,
        }

    def decide(
        self,
        context: BoundDecisionContext,
        *,
        now_utc: datetime | None = None,
    ) -> ProviderResult:
        now = now_utc or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now_utc must use UTC")
        context_timeout = timedelta(
            seconds=int(self.effective.runtime["scheduler"]["cycle_timeout_seconds"])
        )
        if now < context.as_of_utc or now - context.as_of_utc > context_timeout:
            return self._result(context, ProviderErrorCode.BUSINESS_VALIDATION_ERROR)
        api_key = self.environ.get(str(self.profile["provider"]["api_key_env"]), "").strip()
        if not api_key:
            return self._result(context, ProviderErrorCode.API_KEY_MISSING)
        payload = self.request_payload(context)
        input_payload = (
            self.prompt
            + "\n"
            + _canonical_json(context.document)
            + "\n"
            + _canonical_json(self.schema)
        )
        if len(input_payload.encode("utf-8")) > int(self.profile["request"]["max_input_tokens"]):
            return self._result(context, ProviderErrorCode.INPUT_TOO_LARGE)
        client = self.client or cast(
            ProviderClient,
            OpenAI(
                api_key=api_key,
                base_url=str(self.profile["provider"]["base_url"]),
                timeout=float(self.profile["request"]["timeout_seconds"]),
                max_retries=0,
            ),
        )

        maximum_attempts = int(self.profile["retry"]["maximum_attempts"])
        retry_on = set(str(value) for value in self.profile["retry"]["retry_on"])
        backoffs = [float(value) for value in self.profile["retry"]["backoff_seconds"]]
        last_error = ProviderErrorCode.PROVIDER_ERROR
        last_response_id: str | None = None
        last_request_id: str | None = None
        last_model_id = str(self.profile["model"]["id"])
        # 这里不交给 SDK 或通用 retry decorator：每次实际尝试前必须先原子预留预算，
        # 并按本次 provider usage/失败类型独立结算，才能维持周期与 UTC 日成本上限。
        for attempt_number in range(1, maximum_attempts + 1):
            try:
                attempt_id = self.usage_ledger.reserve(
                    cycle_id=context.cycle_id,
                    attempt_number=attempt_number,
                    attempted_at_utc=now,
                    prompt_sha256=str(self.profile["prompt"]["sha256"]),
                    config_sha256=self.effective.effective_sha256,
                    input_sha256=context.sha256,
                )
            except BudgetExceededError:
                return self._result(context, ProviderErrorCode.BUDGET_EXCEEDED)
            except UsageLedgerError:
                return self._result(context, ProviderErrorCode.PROVIDER_ERROR)

            response: Response | ChatCompletion | None = None
            try:
                if self.profile["provider"]["api"] == "responses":
                    response = client.responses.create(**payload)
                else:
                    response = client.chat.completions.create(**payload)
                request_id = getattr(response, "_request_id", None)
                if not isinstance(request_id, str):
                    request_id = None
                decision, usage, response_id, model_id = self._parse_response(response, context)
                self.usage_ledger.settle(
                    attempt_id,
                    settled_at_utc=now,
                    status="COMPLETED",
                    outcome_code="success",
                    usage=usage,
                    charge_reservation=False,
                    provider_response_id=response_id,
                    provider_request_id=request_id,
                    model_id=model_id,
                )
                return self._result(
                    context,
                    None,
                    decision=decision,
                    response_id=response_id,
                    request_id=request_id,
                    model_id=model_id,
                )
            except openai.APIError as error:
                last_error, last_request_id = _provider_error(error)
                try:
                    self.usage_ledger.settle(
                        attempt_id,
                        settled_at_utc=now,
                        status="FAILED",
                        outcome_code=last_error.value,
                        usage=None,
                        charge_reservation=last_error is ProviderErrorCode.TIMEOUT,
                        provider_request_id=last_request_id,
                    )
                except UsageLedgerError:
                    return self._result(context, ProviderErrorCode.PROVIDER_ERROR)
            except _ResponseFailure as error:
                last_error = error.code
                last_response_id = error.response_id
                last_model_id = error.model_id or last_model_id
                request_id = getattr(response, "_request_id", None)
                if not isinstance(request_id, str):
                    request_id = None
                try:
                    self.usage_ledger.settle(
                        attempt_id,
                        settled_at_utc=now,
                        status="FAILED",
                        outcome_code=error.code.value,
                        usage=error.usage,
                        charge_reservation=error.usage is None,
                        provider_response_id=error.response_id,
                        provider_request_id=request_id,
                        model_id=error.model_id,
                    )
                except UsageLedgerError:
                    return self._result(context, ProviderErrorCode.PROVIDER_ERROR)
                last_request_id = request_id
            except UsageLedgerError:
                return self._result(context, ProviderErrorCode.PROVIDER_ERROR)

            if last_error.value not in retry_on or attempt_number == maximum_attempts:
                break
            self.sleep(backoffs[attempt_number - 1])
        return self._result(
            context,
            last_error,
            response_id=last_response_id,
            request_id=last_request_id,
            model_id=last_model_id,
        )

    def _parse_response(
        self,
        response: Response | ChatCompletion,
        context: BoundDecisionContext,
    ) -> tuple[BoundModelDecision, Usage, str, str]:
        if isinstance(response, ChatCompletion):
            return self._parse_chat_response(response, context)
        return self._parse_responses_response(response, context)

    def _parse_responses_response(
        self,
        response: Response,
        context: BoundDecisionContext,
    ) -> tuple[BoundModelDecision, Usage, str, str]:
        response_id = response.id
        model_id = response.model
        if not response_id.startswith("resp_"):
            raise _ResponseFailure(ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT)
        configured_model = str(self.profile["model"]["id"])
        if not (model_id == configured_model or model_id.startswith(f"{configured_model}-")):
            raise _ResponseFailure(
                ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT,
                response_id=response_id,
            )
        usage = _usage(response)
        if usage.input_tokens > int(
            self.profile["request"]["max_input_tokens"]
        ) or usage.output_tokens > int(self.profile["request"]["max_output_tokens"]):
            raise _ResponseFailure(
                ProviderErrorCode.USAGE_INVALID,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            )
        if response.status != "completed" or response.error is not None:
            raise _ResponseFailure(
                ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            )
        output_text = _output_text(response, usage, response_id, model_id)
        decision = self._bind_output_text(
            output_text,
            context,
            usage=usage,
            response_id=response_id,
            model_id=model_id,
        )
        return decision, usage, response_id, model_id

    def _parse_chat_response(
        self,
        response: ChatCompletion,
        context: BoundDecisionContext,
    ) -> tuple[BoundModelDecision, Usage, str, str]:
        response_id = response.id
        model_id = response.model
        if not response_id:
            raise _ResponseFailure(ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT)
        configured_model = str(self.profile["model"]["id"])
        if not (model_id == configured_model or model_id.startswith(f"{configured_model}-")):
            raise _ResponseFailure(
                ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT,
                response_id=response_id,
            )
        usage = _chat_usage(response)
        if usage.input_tokens > int(
            self.profile["request"]["max_input_tokens"]
        ) or usage.output_tokens > int(self.profile["request"]["max_output_tokens"]):
            raise _ResponseFailure(
                ProviderErrorCode.USAGE_INVALID,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            )
        if len(response.choices) != 1:
            raise _ResponseFailure(
                ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            )
        choice = response.choices[0]
        if choice.finish_reason == "content_filter" or choice.message.refusal:
            raise _ResponseFailure(
                ProviderErrorCode.POLICY_REFUSAL,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            )
        if choice.finish_reason != "stop" or choice.message.tool_calls:
            raise _ResponseFailure(
                ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            )
        output_text = choice.message.content
        if not output_text:
            raise _ResponseFailure(
                ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            )
        decision = self._bind_output_text(
            output_text,
            context,
            usage=usage,
            response_id=response_id,
            model_id=model_id,
        )
        return decision, usage, response_id, model_id

    def _bind_output_text(
        self,
        output_text: str,
        context: BoundDecisionContext,
        *,
        usage: Usage,
        response_id: str,
        model_id: str,
    ) -> BoundModelDecision:
        try:
            decision_document = json.loads(output_text)
        except json.JSONDecodeError:
            raise _ResponseFailure(
                ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            ) from None
        if not isinstance(decision_document, dict):
            raise _ResponseFailure(
                ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            )
        try:
            decision = self.binder.bind_model_decision(context, decision_document)
        except ContractValidationError as error:
            malformed_codes = {
                ContractErrorCode.SCHEMA_VALIDATION_FAILED,
                ContractErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            }
            code = (
                ProviderErrorCode.MALFORMED_STRUCTURED_OUTPUT
                if error.code in malformed_codes
                else ProviderErrorCode.BUSINESS_VALIDATION_ERROR
            )
            raise _ResponseFailure(
                code,
                usage=usage,
                response_id=response_id,
                model_id=model_id,
            ) from None
        return decision

    def _result(
        self,
        context: BoundDecisionContext,
        error_code: ProviderErrorCode | None,
        *,
        decision: BoundModelDecision | None = None,
        response_id: str | None = None,
        request_id: str | None = None,
        model_id: str | None = None,
    ) -> ProviderResult:
        try:
            summary = self.usage_ledger.cycle_summary(context.cycle_id)
        except UsageLedgerError:
            summary = UsageSummary(0, Usage(0, 0, 0), "0.000000000")
            error_code = ProviderErrorCode.PROVIDER_ERROR
            decision = None
        return ProviderResult(
            status="SUCCESS" if error_code is None else "HOLD_ONLY",
            decision=decision,
            error_code=error_code,
            model_id=model_id or str(self.profile["model"]["id"]),
            response_id=response_id,
            request_id=request_id,
            prompt_sha256=str(self.profile["prompt"]["sha256"]),
            config_sha256=self.effective.effective_sha256,
            input_sha256=context.sha256,
            usage_summary=summary,
        )


class OpenAIResponsesProvider(OpenAICompatibleProvider):
    """兼容旧调用点，并保证该实例只接受 OpenAI Responses profile。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.profile["provider"]["api"] != "responses":
            raise ValueError("OpenAIResponsesProvider requires a responses profile")


def build_provider(
    effective: EffectiveConfig,
    *,
    usage_db_path: str | Path,
    client: ProviderClient | None = None,
    environ: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> OpenAICompatibleProvider:
    policy = CostPolicy.from_profile(effective.ai_profile)
    ledger = UsageLedger(usage_db_path, policy)
    return OpenAICompatibleProvider(
        effective,
        usage_ledger=ledger,
        client=client,
        environ=environ,
        sleep=sleep,
    )
