"""R2-01 NewsItem、DecisionContext、ModelDecision 与 TradeAction 运行时绑定。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from referencing import Registry, Resource

from alphamind.config import EffectiveConfig, MarketKind
from alphamind.decision.features import PATTERN_SEMANTICS

JsonObject = dict[str, Any]
SUPPORTED_SCHEMA_VERSIONS: Mapping[str, int] = {
    "news-item.schema.yaml": 1,
    "decision-context.schema.yaml": 2,
    "model-decision.schema.yaml": 1,
    "trade-action.schema.yaml": 2,
}


class ContractErrorCode(StrEnum):
    ACTION_NOT_ALLOWED = "action_not_allowed"
    CONFIG_MISMATCH = "config_mismatch"
    CYCLE_MISMATCH = "cycle_mismatch"
    DUPLICATE_VALUE = "duplicate_value"
    INVALID_ARITHMETIC = "invalid_arithmetic"
    INVALID_REFERENCE = "invalid_reference"
    INVALID_TIMESTAMP_ORDER = "invalid_timestamp_order"
    MARKET_UNAVAILABLE = "market_unavailable"
    MAX_ACTIONS_EXCEEDED = "max_actions_exceeded"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    STALE_CONTEXT = "stale_context"
    STALE_NEWS = "stale_news"
    UNKNOWN_INSTRUMENT = "unknown_instrument"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"


class ContractValidationError(ValueError):
    """不回显不可信输入值的确定性合同错误。"""

    def __init__(self, code: ContractErrorCode, location: str) -> None:
        self.code = code
        self.location = location
        super().__init__(f"{code.value} at {location}")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "location": self.location}


@dataclass(frozen=True, slots=True)
class BoundNewsItem:
    _document_json: str
    sha256: str
    news_id: str
    published_at_utc: datetime
    fetched_at_utc: datetime
    assets: tuple[str, ...]

    @property
    def document(self) -> JsonObject:
        return _document_from_json(self._document_json)


@dataclass(frozen=True, slots=True)
class BoundDecisionContext:
    _document_json: str
    sha256: str
    cycle_id: str
    as_of_utc: datetime
    instrument_ids: tuple[str, ...]
    news_ids: tuple[str, ...]

    @property
    def document(self) -> JsonObject:
        return _document_from_json(self._document_json)


@dataclass(frozen=True, slots=True)
class BoundModelDecision:
    _document_json: str
    sha256: str
    context_sha256: str
    cycle_id: str
    action_ids: tuple[str, ...]

    @property
    def document(self) -> JsonObject:
        return _document_from_json(self._document_json)


@dataclass(frozen=True, slots=True)
class BoundDecisionChain:
    context: BoundDecisionContext
    decision: BoundModelDecision


def _canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _object(value: object, location: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ContractValidationError(ContractErrorCode.SCHEMA_VALIDATION_FAILED, location)
    return value


def _document_from_json(payload: str) -> JsonObject:
    try:
        document: object = json.loads(payload)
    except json.JSONDecodeError:
        raise ContractValidationError(
            ContractErrorCode.SCHEMA_VALIDATION_FAILED, "bound.document"
        ) from None
    return _object(document, "bound.document")


def _list(value: object, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractValidationError(ContractErrorCode.SCHEMA_VALIDATION_FAILED, location)
    return value


def _timestamp(value: object, location: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ContractValidationError(ContractErrorCode.SCHEMA_VALIDATION_FAILED, location)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractValidationError(
            ContractErrorCode.SCHEMA_VALIDATION_FAILED, location
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractValidationError(ContractErrorCode.SCHEMA_VALIDATION_FAILED, location)
    return parsed.astimezone(UTC)


def _decimal(value: object, location: str) -> Decimal:
    if not isinstance(value, str):
        raise ContractValidationError(ContractErrorCode.SCHEMA_VALIDATION_FAILED, location)
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ContractValidationError(
            ContractErrorCode.SCHEMA_VALIDATION_FAILED, location
        ) from None
    if not parsed.is_finite():
        raise ContractValidationError(ContractErrorCode.INVALID_ARITHMETIC, location)
    return parsed


def _ensure_unique(values: Iterable[str], location: str) -> tuple[str, ...]:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ContractValidationError(ContractErrorCode.DUPLICATE_VALUE, location)
    return materialized


def _schema_path(error: jsonschema.ValidationError) -> str:
    path = ".".join(str(item) for item in error.absolute_path)
    return path if path else "document"


class DecisionContractBinder:
    """只有经过本对象绑定的 Action 才能交给未来审批层。"""

    def __init__(self, effective: EffectiveConfig) -> None:
        self.effective = effective
        self.registry = effective.instrument_registry
        self.capability = effective.market_capability_snapshot
        self.max_actions = int(effective.runtime["scheduler"]["max_actions_per_cycle"])
        self.context_timeout = timedelta(
            seconds=int(effective.runtime["scheduler"]["cycle_timeout_seconds"])
        )
        self.news_lookback = timedelta(
            hours=int(effective.runtime["scheduler"]["news_lookback_hours"])
        )
        self.sources = {
            str(source["source_id"]): source
            for source in effective.news_sources["sources"]
            if source["enabled"]
        }
        self._validators = self._load_validators(effective.project_root)

    @staticmethod
    def _load_validators(project_root: Path) -> dict[str, jsonschema.Draft202012Validator]:
        schema_root = project_root / "data" / "schemas"
        schemas: dict[str, JsonObject] = {}
        resources: list[tuple[str, Resource[Any]]] = []
        for path in sorted(schema_root.glob("*.schema.yaml")):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError):
                raise ContractValidationError(
                    ContractErrorCode.SCHEMA_VALIDATION_FAILED, f"schema.{path.name}"
                ) from None
            schema = _object(document, f"schema.{path.name}")
            schema_id = schema.get("$id")
            if not isinstance(schema_id, str):
                raise ContractValidationError(
                    ContractErrorCode.SCHEMA_VALIDATION_FAILED, f"schema.{path.name}"
                )
            schemas[path.name] = schema
            resources.append((schema_id, Resource.from_contents(schema)))
        registry = Registry().with_resources(resources)
        validators: dict[str, jsonschema.Draft202012Validator] = {}
        for name in SUPPORTED_SCHEMA_VERSIONS:
            selected_schema = schemas.get(name)
            if selected_schema is None:
                raise ContractValidationError(
                    ContractErrorCode.SCHEMA_VALIDATION_FAILED, f"schema.{name}"
                )
            validators[name] = jsonschema.Draft202012Validator(
                selected_schema,
                registry=registry,
                format_checker=jsonschema.FormatChecker(),
            )
        return validators

    def _validate_schema(self, name: str, document: JsonObject, location: str) -> None:
        self._validate_version(name, document, location)
        error = next(iter(self._validators[name].iter_errors(document)), None)
        if error is not None:
            path = _schema_path(error)
            raise ContractValidationError(
                ContractErrorCode.SCHEMA_VALIDATION_FAILED,
                location if path == "document" else f"{location}.{path}",
            )

    @staticmethod
    def _validate_version(name: str, document: JsonObject, location: str) -> None:
        expected_version = SUPPORTED_SCHEMA_VERSIONS[name]
        if (
            type(document.get("schema_version")) is not int
            or document.get("schema_version") != expected_version
        ):
            raise ContractValidationError(
                ContractErrorCode.UNSUPPORTED_SCHEMA_VERSION,
                f"{location}.schema_version",
            )

    def bind_context(
        self,
        raw_context: Mapping[str, Any],
        *,
        now_utc: datetime,
    ) -> BoundDecisionContext:
        if now_utc.tzinfo is None or now_utc.utcoffset() != timedelta(0):
            raise ValueError("now_utc must use UTC")
        context = deepcopy(dict(raw_context))
        self._validate_version("decision-context.schema.yaml", context, "context")
        raw_news_items = context.get("news_items")
        if isinstance(raw_news_items, list):
            for index, raw_news in enumerate(raw_news_items):
                if isinstance(raw_news, dict):
                    self._validate_version(
                        "news-item.schema.yaml", raw_news, f"context.news_items.{index}"
                    )
        self._validate_schema("decision-context.schema.yaml", context, "context")
        if context["environment"] != self.effective.runtime["environment"]:
            raise ContractValidationError(ContractErrorCode.CONFIG_MISMATCH, "context.environment")
        if context["config_sha256"] != self.effective.effective_sha256:
            raise ContractValidationError(
                ContractErrorCode.CONFIG_MISMATCH, "context.config_sha256"
            )
        if context["instrument_registry_sha256"] != self.registry.source_sha256:
            raise ContractValidationError(
                ContractErrorCode.CONFIG_MISMATCH,
                "context.instrument_registry_sha256",
            )

        as_of = _timestamp(context["as_of_utc"], "context.as_of_utc")
        generated = _timestamp(context["generated_at_utc"], "context.generated_at_utc")
        if as_of > generated or generated > now_utc + timedelta(seconds=5):
            raise ContractValidationError(
                ContractErrorCode.INVALID_TIMESTAMP_ORDER, "context.generated_at_utc"
            )
        if now_utc - generated > self.context_timeout:
            raise ContractValidationError(
                ContractErrorCode.STALE_CONTEXT, "context.generated_at_utc"
            )

        account = _object(context["account"], "context.account")
        futures_margin_used = _decimal(
            account["futures_margin_used"], "context.account.futures_margin_used"
        )
        if futures_margin_used > _decimal(account["nav"], "context.account.nav"):
            raise ContractValidationError(
                ContractErrorCode.INVALID_ARITHMETIC,
                "context.account.futures_margin_used",
            )
        allowed_actions = frozenset(str(item) for item in context["allowed_actions"])
        if account["risk_state"] != "ENTRY_ALLOWED" and allowed_actions & {"OPEN", "ADD"}:
            raise ContractValidationError(
                ContractErrorCode.ACTION_NOT_ALLOWED, "context.allowed_actions"
            )

        instrument_rows = _list(context["instruments"], "context.instruments")
        instrument_ids = _ensure_unique(
            (str(_object(row, "context.instruments")["instrument_id"]) for row in instrument_rows),
            "context.instruments.instrument_id",
        )
        context_markets: set[tuple[str, str]] = set()
        position_ids: list[str] = []
        for index, raw_row in enumerate(instrument_rows):
            row = _object(raw_row, f"context.instruments.{index}")
            instrument_id = str(row["instrument_id"])
            configured = self.registry.get(instrument_id)
            if configured is None or not configured.enabled:
                raise ContractValidationError(
                    ContractErrorCode.UNKNOWN_INSTRUMENT,
                    f"context.instruments.{index}.instrument_id",
                )
            observed = _timestamp(
                row["observed_at_utc"], f"context.instruments.{index}.observed_at_utc"
            )
            completed_candle = _timestamp(
                row["completed_candle_at_utc"],
                f"context.instruments.{index}.completed_candle_at_utc",
            )
            if completed_candle > observed or observed > as_of:
                raise ContractValidationError(
                    ContractErrorCode.INVALID_TIMESTAMP_ORDER,
                    f"context.instruments.{index}.observed_at_utc",
                )
            if row["spot"] is None and row["futures"] is None:
                raise ContractValidationError(
                    ContractErrorCode.MARKET_UNAVAILABLE, f"context.instruments.{index}"
                )
            if row["spot"] is not None:
                spot = _object(row["spot"], f"context.instruments.{index}.spot")
                capability = self.capability.capability_for_pair(str(spot["pair"]), MarketKind.SPOT)
                if (
                    not configured.spot.enabled
                    or configured.spot.pair != spot["pair"]
                    or capability is None
                    or not capability.available
                ):
                    raise ContractValidationError(
                        ContractErrorCode.MARKET_UNAVAILABLE,
                        f"context.instruments.{index}.spot.pair",
                    )
                context_markets.add((instrument_id, "spot"))
            if row["futures"] is not None:
                futures = _object(row["futures"], f"context.instruments.{index}.futures")
                capability = self.capability.capability_for_pair(
                    str(futures["pair"]), MarketKind.FUTURES
                )
                if (
                    not configured.futures.enabled
                    or configured.futures.pair != futures["pair"]
                    or capability is None
                    or not capability.available
                ):
                    raise ContractValidationError(
                        ContractErrorCode.MARKET_UNAVAILABLE,
                        f"context.instruments.{index}.futures.pair",
                    )
                context_markets.add((instrument_id, "linear_perpetual"))
                position = futures["position"]
                if position is not None:
                    position_ids.append(
                        str(
                            _object(position, f"context.instruments.{index}.futures.position")[
                                "position_id"
                            ]
                        )
                    )
            features = _object(row["features"], f"context.instruments.{index}.features")
            for indicator_name in ("rsi", "adx"):
                raw_indicator = features[indicator_name]
                if raw_indicator is None:
                    continue
                indicator = _decimal(
                    raw_indicator,
                    f"context.instruments.{index}.features.{indicator_name}",
                )
                if indicator < 0 or indicator > 100:
                    raise ContractValidationError(
                        ContractErrorCode.INVALID_ARITHMETIC,
                        f"context.instruments.{index}.features.{indicator_name}",
                    )
            pattern = features["candlestick_pattern"]
            semantic = features["pattern_semantic"]
            expected_semantic = PATTERN_SEMANTICS.get(str(pattern)) if pattern is not None else None
            if semantic != expected_semantic:
                raise ContractValidationError(
                    ContractErrorCode.INVALID_REFERENCE,
                    f"context.instruments.{index}.features.pattern_semantic",
                )
        _ensure_unique(position_ids, "context.instruments.futures.position.position_id")

        order_ids: list[str] = []
        for index, raw_order in enumerate(_list(context["open_orders"], "context.open_orders")):
            order = _object(raw_order, f"context.open_orders.{index}")
            order_ids.append(str(order["order_id"]))
            if (str(order["instrument_id"]), str(order["market"])) not in context_markets:
                raise ContractValidationError(
                    ContractErrorCode.INVALID_REFERENCE,
                    f"context.open_orders.{index}.instrument_id",
                )
            filled_quantity = _decimal(
                order["filled_quantity"], f"context.open_orders.{index}.filled_quantity"
            )
            if filled_quantity > _decimal(
                order["quantity"], f"context.open_orders.{index}.quantity"
            ):
                raise ContractValidationError(
                    ContractErrorCode.INVALID_ARITHMETIC,
                    f"context.open_orders.{index}.filled_quantity",
                )
        _ensure_unique(order_ids, "context.open_orders.order_id")

        pending_action_ids: list[str] = []
        pending_proposal_ids: list[str] = []
        for index, raw_pending in enumerate(
            _list(context["pending_approvals"], "context.pending_approvals")
        ):
            pending = _object(raw_pending, f"context.pending_approvals.{index}")
            pending_action_ids.append(str(pending["action_id"]))
            pending_proposal_ids.append(str(pending["proposal_id"]))
            if (
                _timestamp(
                    pending["expires_at_utc"], f"context.pending_approvals.{index}.expires_at_utc"
                )
                <= as_of
            ):
                raise ContractValidationError(
                    ContractErrorCode.INVALID_TIMESTAMP_ORDER,
                    f"context.pending_approvals.{index}.expires_at_utc",
                )
        _ensure_unique(pending_action_ids, "context.pending_approvals.action_id")
        _ensure_unique(pending_proposal_ids, "context.pending_approvals.proposal_id")

        fill_ids: list[str] = []
        for index, raw_fill in enumerate(_list(context["recent_fills"], "context.recent_fills")):
            fill = _object(raw_fill, f"context.recent_fills.{index}")
            fill_ids.append(str(fill["fill_id"]))
            if (str(fill["instrument_id"]), str(fill["market"])) not in context_markets:
                raise ContractValidationError(
                    ContractErrorCode.INVALID_REFERENCE,
                    f"context.recent_fills.{index}.instrument_id",
                )
            if (
                _timestamp(fill["occurred_at_utc"], f"context.recent_fills.{index}.occurred_at_utc")
                > as_of
            ):
                raise ContractValidationError(
                    ContractErrorCode.INVALID_TIMESTAMP_ORDER,
                    f"context.recent_fills.{index}.occurred_at_utc",
                )
        _ensure_unique(fill_ids, "context.recent_fills.fill_id")

        news_rows = _list(context["news_items"], "context.news_items")
        news_ids: list[str] = []
        canonical_urls: list[str] = []
        title_hashes: list[str] = []
        content_hashes: list[str] = []
        local_assets = set(instrument_ids)
        for index, raw_news in enumerate(news_rows):
            news = _object(raw_news, f"context.news_items.{index}")
            bound_news = self.bind_news_item(
                news,
                as_of_utc=as_of,
                local_instrument_ids=local_assets,
                location=f"context.news_items.{index}",
            )
            news_ids.append(bound_news.news_id)
            canonical_urls.append(str(news["canonical_url"]))
            title_hashes.append(str(news["title_sha256"]))
            content_hashes.append(str(news["content_sha256"]))
        _ensure_unique(news_ids, "context.news_items.news_id")
        _ensure_unique(canonical_urls, "context.news_items.canonical_url")
        _ensure_unique(title_hashes, "context.news_items.title_sha256")
        _ensure_unique(content_hashes, "context.news_items.content_sha256")

        previous = context["previous_cycle"]
        if previous is not None:
            previous_cycle = _object(previous, "context.previous_cycle")
            if previous_cycle["cycle_id"] == context["cycle_id"]:
                raise ContractValidationError(
                    ContractErrorCode.CYCLE_MISMATCH, "context.previous_cycle.cycle_id"
                )
            if (
                _timestamp(
                    previous_cycle["completed_at_utc"], "context.previous_cycle.completed_at_utc"
                )
                > as_of
            ):
                raise ContractValidationError(
                    ContractErrorCode.INVALID_TIMESTAMP_ORDER,
                    "context.previous_cycle.completed_at_utc",
                )
            if len(previous_cycle["action_ids"]) != len(previous_cycle["user_decisions"]):
                raise ContractValidationError(
                    ContractErrorCode.INVALID_REFERENCE,
                    "context.previous_cycle.user_decisions",
                )

        canonical_json = _canonical_json(context)
        return BoundDecisionContext(
            _document_json=canonical_json,
            sha256=_canonical_sha256(canonical_json),
            cycle_id=str(context["cycle_id"]),
            as_of_utc=as_of,
            instrument_ids=instrument_ids,
            news_ids=tuple(news_ids),
        )

    def bind_news_item(
        self,
        raw_news: Mapping[str, Any],
        *,
        as_of_utc: datetime,
        local_instrument_ids: Iterable[str] | None = None,
        location: str = "news",
    ) -> BoundNewsItem:
        if as_of_utc.tzinfo is None or as_of_utc.utcoffset() != timedelta(0):
            raise ValueError("as_of_utc must use UTC")
        news = deepcopy(dict(raw_news))
        self._validate_schema("news-item.schema.yaml", news, location)
        source = _object(news["source"], f"{location}.source")
        configured_source = self.sources.get(str(source["source_id"]))
        if configured_source is None:
            raise ContractValidationError(
                ContractErrorCode.INVALID_REFERENCE, f"{location}.source.source_id"
            )
        source_bindings = {
            "display_name": "display_name",
            "source_type": "source_type",
            "trust_tier": "trust_tier",
        }
        for news_field, configured_field in source_bindings.items():
            if source[news_field] != configured_source[configured_field]:
                raise ContractValidationError(
                    ContractErrorCode.CONFIG_MISMATCH,
                    f"{location}.source.{news_field}",
                )
        if news["language"] != configured_source["language"]:
            raise ContractValidationError(ContractErrorCode.CONFIG_MISMATCH, f"{location}.language")
        if news["category"] not in configured_source["categories"]:
            raise ContractValidationError(ContractErrorCode.CONFIG_MISMATCH, f"{location}.category")
        published = _timestamp(news["published_at_utc"], f"{location}.published_at_utc")
        fetched = _timestamp(news["fetched_at_utc"], f"{location}.fetched_at_utc")
        if published > fetched or fetched > as_of_utc:
            raise ContractValidationError(
                ContractErrorCode.INVALID_TIMESTAMP_ORDER, f"{location}.fetched_at_utc"
            )
        if as_of_utc - published > self.news_lookback:
            raise ContractValidationError(
                ContractErrorCode.STALE_NEWS, f"{location}.published_at_utc"
            )
        allowed_assets = (
            set(local_instrument_ids)
            if local_instrument_ids is not None
            else {item.instrument_id for item in self.registry.instruments}
        )
        assets = tuple(str(asset) for asset in news["assets"])
        if any(asset != "MARKET" and asset not in allowed_assets for asset in assets):
            raise ContractValidationError(ContractErrorCode.INVALID_REFERENCE, f"{location}.assets")
        canonical_json = _canonical_json(news)
        return BoundNewsItem(
            _document_json=canonical_json,
            sha256=_canonical_sha256(canonical_json),
            news_id=str(news["news_id"]),
            published_at_utc=published,
            fetched_at_utc=fetched,
            assets=assets,
        )

    def bind_model_decision(
        self,
        context: BoundDecisionContext,
        raw_decision: Mapping[str, Any],
    ) -> BoundModelDecision:
        decision = deepcopy(dict(raw_decision))
        self._validate_version("model-decision.schema.yaml", decision, "decision")
        raw_actions = decision.get("actions")
        if isinstance(raw_actions, list):
            for index, raw_action in enumerate(raw_actions):
                if isinstance(raw_action, dict):
                    self._validate_version(
                        "trade-action.schema.yaml", raw_action, f"decision.actions.{index}"
                    )
        self._validate_schema("model-decision.schema.yaml", decision, "decision")
        if decision["cycle_id"] != context.cycle_id:
            raise ContractValidationError(ContractErrorCode.CYCLE_MISMATCH, "decision.cycle_id")
        actions = _list(decision["actions"], "decision.actions")
        if len(actions) > self.max_actions:
            raise ContractValidationError(
                ContractErrorCode.MAX_ACTIONS_EXCEEDED, "decision.actions"
            )
        action_ids = _ensure_unique(
            (
                str(_object(action, f"decision.actions.{index}")["action_id"])
                for index, action in enumerate(actions)
            ),
            "decision.actions.action_id",
        )
        context_document = context.document
        pending_action_ids = {
            str(item["action_id"]) for item in context_document["pending_approvals"]
        }
        local_news = set(context.news_ids)
        local_instruments = set(context.instrument_ids)
        allowed_actions = set(context_document["allowed_actions"])
        context_rows = {
            str(item["instrument_id"]): item for item in context_document["instruments"]
        }
        order_rows = {str(item["order_id"]): item for item in context_document["open_orders"]}

        for index, raw_action in enumerate(actions):
            action = _object(raw_action, f"decision.actions.{index}")
            self._validate_schema("trade-action.schema.yaml", action, f"decision.actions.{index}")
            if action["cycle_id"] != context.cycle_id:
                raise ContractValidationError(
                    ContractErrorCode.CYCLE_MISMATCH,
                    f"decision.actions.{index}.cycle_id",
                )
            if action["action_id"] in pending_action_ids:
                raise ContractValidationError(
                    ContractErrorCode.DUPLICATE_VALUE,
                    f"decision.actions.{index}.action_id",
                )
            instrument_id = str(action["instrument_id"])
            if instrument_id not in local_instruments:
                raise ContractValidationError(
                    ContractErrorCode.UNKNOWN_INSTRUMENT,
                    f"decision.actions.{index}.instrument_id",
                )
            if action["action"] not in allowed_actions:
                raise ContractValidationError(
                    ContractErrorCode.ACTION_NOT_ALLOWED,
                    f"decision.actions.{index}.action",
                )
            instrument_row = context_rows[instrument_id]
            market_key = "spot" if action["market"] == "spot" else "futures"
            if instrument_row[market_key] is None:
                raise ContractValidationError(
                    ContractErrorCode.MARKET_UNAVAILABLE,
                    f"decision.actions.{index}.market",
                )
            if any(str(news_id) not in local_news for news_id in action["news_refs"]):
                raise ContractValidationError(
                    ContractErrorCode.INVALID_REFERENCE,
                    f"decision.actions.{index}.news_refs",
                )
            if action["action"] == "CANCEL_ORDER":
                target_id = str(action["target_reference_id"])
                target = order_rows.get(target_id)
                if (
                    target is None
                    or target["instrument_id"] != instrument_id
                    or target["market"] != action["market"]
                ):
                    raise ContractValidationError(
                        ContractErrorCode.INVALID_REFERENCE,
                        f"decision.actions.{index}.target_reference_id",
                    )

        canonical_json = _canonical_json(decision)
        return BoundModelDecision(
            _document_json=canonical_json,
            sha256=_canonical_sha256(canonical_json),
            context_sha256=context.sha256,
            cycle_id=context.cycle_id,
            action_ids=action_ids,
        )

    def bind_chain(
        self,
        raw_context: Mapping[str, Any],
        raw_decision: Mapping[str, Any],
        *,
        now_utc: datetime,
    ) -> BoundDecisionChain:
        context = self.bind_context(raw_context, now_utc=now_utc)
        decision = self.bind_model_decision(context, raw_decision)
        return BoundDecisionChain(context, decision)
