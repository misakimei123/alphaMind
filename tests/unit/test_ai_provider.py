from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import openai
import pytest
import yaml
from openai.types.responses import Response

import alphamind.ai.provider as provider_module
from alphamind.ai import (
    BudgetExceededError,
    CostPolicy,
    OpenAIResponsesProvider,
    ProviderErrorCode,
    Usage,
    UsageLedger,
    UsageLedgerError,
)
from alphamind.config import EffectiveConfig, load_effective_config
from alphamind.decision import BoundDecisionContext, DecisionContractBinder

PROJECT_ROOT = Path(__file__).parents[2]
CONTRACT_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "contracts"
OPENAI_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "openai"
NOW = datetime(2026, 7, 18, 12, 0, 5, tzinfo=UTC)


def _yaml(name: str) -> dict[str, object]:
    document = yaml.safe_load((CONTRACT_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _effective_context() -> tuple[EffectiveConfig, BoundDecisionContext]:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    context = _yaml("decision-context.valid.yaml")
    context["config_sha256"] = effective.effective_sha256
    context["instrument_registry_sha256"] = effective.instrument_registry.source_sha256
    bound = DecisionContractBinder(effective).bind_context(context, now_utc=NOW)
    return effective, bound


def _success_document() -> dict[str, object]:
    document = json.loads((OPENAI_FIXTURES / "responses-success.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _response(document: dict[str, object] | None = None) -> Response:
    response = Response.model_validate(document or _success_document())
    response._request_id = "req_fixture_001"  # type: ignore[attr-defined]
    return response


class FakeResponses:
    def __init__(
        self,
        responses: list[Response | openai.APIError],
    ) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> Response:
        self.requests.append(dict(kwargs))
        next_response = self.responses.pop(0)
        if isinstance(next_response, openai.APIError):
            raise next_response
        return next_response


class FakeClient:
    def __init__(self, responses: list[Response | openai.APIError]) -> None:
        self.responses = FakeResponses(responses)

    @property
    def requests(self) -> list[dict[str, object]]:
        return self.responses.requests


def _status_error(
    error_type: type[openai.APIStatusError],
    status_code: int,
    *,
    request_id: str,
    body: object | None = None,
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(
        status_code,
        request=request,
        headers={"x-request-id": request_id},
    )
    return error_type("fixture provider error", response=response, body=body)


def _timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.com/v1/responses")
    )


def _provider(
    tmp_path: Path,
    client: FakeClient,
    *,
    effective: EffectiveConfig | None = None,
    environ: dict[str, str] | None = None,
    sleeps: list[float] | None = None,
) -> OpenAIResponsesProvider:
    selected = effective or _effective_context()[0]
    policy = CostPolicy.from_profile(selected.ai_profile)
    ledger = UsageLedger(tmp_path / "ai-usage.sqlite", policy)
    sleep_values = sleeps if sleeps is not None else []
    return OpenAIResponsesProvider(
        selected,
        usage_ledger=ledger,
        client=client,
        environ=({"OPENAI_API_KEY": "test-key-never-logged"} if environ is None else environ),
        sleep=sleep_values.append,
    )


def _walk(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        found.append(cast(dict[str, object], value))
        for child in value.values():
            found.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk(child))
    return found


def test_request_uses_strict_responses_contract_without_tools_or_storage(tmp_path: Path) -> None:
    effective, context = _effective_context()
    client = FakeClient([_response()])
    provider = _provider(tmp_path, client, effective=effective)

    payload = provider.request_payload(context)
    result = provider.decide(context, now_utc=NOW)

    assert result.status == "SUCCESS"
    assert result.error_code is None
    assert result.decision is not None
    assert result.decision.document["actions"][0]["action"] == "HOLD"
    assert result.usage_summary.accounted_cost_usd == "0.003550000"
    assert result.usage_summary.usage == Usage(1000, 200, 100)
    assert payload["model"] == "gpt-5.6-terra"
    assert payload["tools"] == []
    assert payload["tool_choice"] == "none"
    assert payload["store"] is False
    assert payload["background"] is False
    response_format = payload["text"]["format"]
    assert response_format["type"] == "json_schema"
    assert response_format["strict"] is True
    assert all("uniqueItems" not in item for item in _walk(response_format["schema"]))
    object_schemas = [
        item for item in _walk(response_format["schema"]) if item.get("type") == "object"
    ]
    assert object_schemas
    assert all(item["additionalProperties"] is False for item in object_schemas)
    assert all(set(item["required"]) == set(item["properties"]) for item in object_schemas)
    assert client.requests[0] == payload
    assert "test-key-never-logged" not in json.dumps(payload)


def test_rate_limit_and_server_errors_retry_once_then_succeed(tmp_path: Path) -> None:
    _, context = _effective_context()
    sleeps: list[float] = []
    client = FakeClient(
        [
            _status_error(
                openai.RateLimitError,
                429,
                request_id="req_rate",
            ),
            _response(),
        ]
    )
    provider = _provider(tmp_path, client, sleeps=sleeps)

    result = provider.decide(context, now_utc=NOW)

    assert result.status == "SUCCESS"
    assert result.usage_summary.attempts == 2
    assert result.usage_summary.accounted_cost_usd == "0.003550000"
    assert sleeps == [2.0]
    assert len(client.requests) == 2


def test_default_sdk_client_disables_nested_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    effective, context = _effective_context()
    client = FakeClient([_response()])
    captured: dict[str, object] = {}

    def fake_openai(**kwargs: object) -> FakeClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(provider_module, "OpenAI", fake_openai)
    provider = OpenAIResponsesProvider(
        effective,
        usage_ledger=UsageLedger(
            tmp_path / "ai-usage.sqlite",
            CostPolicy.from_profile(effective.ai_profile),
        ),
        environ={"OPENAI_API_KEY": "test-key-never-logged"},
        sleep=lambda _: None,
    )

    result = provider.decide(context, now_utc=NOW)

    assert result.status == "SUCCESS"
    assert captured == {
        "api_key": "test-key-never-logged",
        "base_url": "https://api.openai.com/v1",
        "max_retries": 0,
        "timeout": 90.0,
    }


def test_sdk_output_text_accepts_reasoning_item_before_message(tmp_path: Path) -> None:
    _, context = _effective_context()
    document = _success_document()
    output = cast(list[dict[str, object]], document["output"])
    output.insert(
        0,
        {
            "id": "rs_fixture_001",
            "type": "reasoning",
            "summary": [],
            "status": "completed",
        },
    )

    result = _provider(tmp_path, FakeClient([_response(document)])).decide(context, now_utc=NOW)

    assert result.status == "SUCCESS"
    assert result.decision is not None
    assert result.decision.document["decision_summary"] == "Hold pending stronger evidence."


def test_malformed_structured_output_retries_after_recording_usage(tmp_path: Path) -> None:
    _, context = _effective_context()
    malformed = _success_document()
    malformed_output = cast(list[dict[str, object]], malformed["output"])
    malformed_content = cast(list[dict[str, object]], malformed_output[0]["content"])
    malformed_content[0]["text"] = "{not-json"
    sleeps: list[float] = []
    provider = _provider(
        tmp_path,
        FakeClient([_response(malformed), _response()]),
        sleeps=sleeps,
    )

    result = provider.decide(context, now_utc=NOW)

    assert result.status == "SUCCESS"
    assert result.usage_summary.attempts == 2
    assert result.usage_summary.usage == Usage(2000, 400, 200)
    assert result.usage_summary.accounted_cost_usd == "0.007100000"
    assert sleeps == [2.0]


def test_business_validation_authentication_and_refusal_never_retry(
    tmp_path: Path,
) -> None:
    _, context = _effective_context()
    invalid = _success_document()
    invalid_output = cast(list[dict[str, object]], invalid["output"])
    invalid_content = cast(list[dict[str, object]], invalid_output[0]["content"])
    decision = json.loads(cast(str, invalid_content[0]["text"]))
    decision["cycle_id"] = "cycle-20260718T120000Z-deadbeef"
    invalid_content[0]["text"] = json.dumps(decision)
    cases: list[tuple[Response | openai.APIError, ProviderErrorCode]] = [
        (_response(invalid), ProviderErrorCode.BUSINESS_VALIDATION_ERROR),
        (
            _status_error(
                openai.AuthenticationError,
                401,
                request_id="req_auth",
            ),
            ProviderErrorCode.AUTHENTICATION_ERROR,
        ),
    ]
    refusal = _success_document()
    refusal_output = cast(list[dict[str, object]], refusal["output"])
    refusal_output[0]["content"] = [{"type": "refusal", "refusal": "policy refusal"}]
    cases.append((_response(refusal), ProviderErrorCode.POLICY_REFUSAL))

    for index, (response, expected) in enumerate(cases):
        case_root = tmp_path / str(index)
        client = FakeClient([response])
        result = _provider(case_root, client).decide(context, now_utc=NOW)
        assert result.status == "HOLD_ONLY"
        assert result.error_code is expected
        assert result.usage_summary.attempts == 1
        assert len(client.requests) == 1


def test_timeout_is_conservatively_charged_and_retry_is_stopped_by_cycle_cap(
    tmp_path: Path,
) -> None:
    _, context = _effective_context()
    sleeps: list[float] = []
    client = FakeClient([_timeout_error()])
    result = _provider(tmp_path, client, sleeps=sleeps).decide(context, now_utc=NOW)

    assert result.status == "HOLD_ONLY"
    assert result.error_code is ProviderErrorCode.BUDGET_EXCEEDED
    assert result.usage_summary.attempts == 1
    assert result.usage_summary.accounted_cost_usd == "0.105000000"
    assert sleeps == [2.0]


def test_missing_key_and_stale_context_fail_before_transport_or_budget_reservation(
    tmp_path: Path,
) -> None:
    _, context = _effective_context()
    client = FakeClient([])
    missing = _provider(tmp_path / "missing", client, environ={}).decide(context, now_utc=NOW)
    stale = _provider(tmp_path / "stale", client).decide(
        context,
        now_utc=datetime(2026, 7, 18, 12, 3, tzinfo=UTC),
    )

    assert missing.error_code is ProviderErrorCode.API_KEY_MISSING
    assert stale.error_code is ProviderErrorCode.BUSINESS_VALIDATION_ERROR
    assert missing.usage_summary.attempts == stale.usage_summary.attempts == 0
    assert client.requests == []


def test_failed_attempt_settlement_error_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, context = _effective_context()
    client = FakeClient([_status_error(openai.RateLimitError, 429, request_id="req_rate")])
    provider = _provider(tmp_path, client)

    def fail_settlement(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise UsageLedgerError("injected ledger failure")

    monkeypatch.setattr(provider.usage_ledger, "settle", fail_settlement)
    result = provider.decide(context, now_utc=NOW)

    assert result.status == "HOLD_ONLY"
    assert result.error_code is ProviderErrorCode.PROVIDER_ERROR
    assert len(client.requests) == 1


def test_usage_above_configured_token_limits_fails_closed_without_retry(tmp_path: Path) -> None:
    _, context = _effective_context()
    invalid_usage = _success_document()
    usage = cast(dict[str, object], invalid_usage["usage"])
    usage["output_tokens"] = 3001
    result = _provider(tmp_path, FakeClient([_response(invalid_usage)])).decide(
        context, now_utc=NOW
    )

    assert result.status == "HOLD_ONLY"
    assert result.error_code is ProviderErrorCode.USAGE_INVALID
    assert result.usage_summary.attempts == 1
    assert result.usage_summary.accounted_cost_usd == "0.047065000"


def test_usage_ledger_enforces_daily_reservation_across_instances(tmp_path: Path) -> None:
    effective, _ = _effective_context()
    base = CostPolicy.from_profile(effective.ai_profile)
    policy = replace(
        base,
        maximum_cost_per_utc_day_nano_usd=base.maximum_attempt_cost_nano_usd,
    )
    path = tmp_path / "shared.sqlite"
    first = UsageLedger(path, policy)
    second = UsageLedger(path, policy)
    first.reserve(
        cycle_id="cycle-20260718T120000Z-00000001",
        attempt_number=1,
        attempted_at_utc=NOW,
        prompt_sha256="a" * 64,
        config_sha256="b" * 64,
        input_sha256="c" * 64,
    )

    with pytest.raises(BudgetExceededError, match="UTC-day"):
        second.reserve(
            cycle_id="cycle-20260718T123000Z-00000002",
            attempt_number=1,
            attempted_at_utc=NOW,
            prompt_sha256="a" * 64,
            config_sha256="b" * 64,
            input_sha256="d" * 64,
        )


def test_usage_database_contains_hashes_and_usage_but_no_prompt_context_or_key(
    tmp_path: Path,
) -> None:
    _, context = _effective_context()
    provider = _provider(tmp_path, FakeClient([_response()]))
    result = provider.decide(context, now_utc=NOW)
    assert result.status == "SUCCESS"

    connection = sqlite3.connect(tmp_path / "ai-usage.sqlite")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_attempts)").fetchall()}
    row = connection.execute(
        "SELECT prompt_sha256, config_sha256, input_sha256, input_tokens, "
        "cached_input_tokens, output_tokens FROM ai_attempts"
    ).fetchone()
    connection.close()

    assert {"raw_prompt", "raw_context", "api_key", "response_body"}.isdisjoint(columns)
    assert row is not None
    assert row[:3] == (
        result.prompt_sha256,
        result.config_sha256,
        result.input_sha256,
    )
    database_bytes = (tmp_path / "ai-usage.sqlite").read_bytes()
    assert b"test-key-never-logged" not in database_bytes
    assert b"alphaMind AI Trade Decision Prompt" not in database_bytes


def test_provider_result_is_detached_from_transport_response(tmp_path: Path) -> None:
    _, context = _effective_context()
    response_document = _success_document()
    result = _provider(tmp_path, FakeClient([_response(response_document)])).decide(
        context, now_utc=NOW
    )
    assert result.decision is not None
    exposed = result.decision.document
    exposed["decision_summary"] = "mutated"
    assert result.decision.document["decision_summary"] == "Hold pending stronger evidence."


def test_cost_policy_uses_cached_discount_and_exact_nano_usd_accounting() -> None:
    effective, _ = _effective_context()
    policy = CostPolicy.from_profile(deepcopy(effective.ai_profile))

    assert policy.usage_cost_nano_usd(Usage(1000, 200, 100)) == 3_550_000
    assert policy.maximum_attempt_cost_nano_usd == 105_000_000
