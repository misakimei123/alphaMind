"""Instrument Registry 与 Bybit 公共市场规则的确定性能力快照。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from alphamind.config.instruments import Instrument, InstrumentRegistry, MarketKind
from alphamind.market.bybit import BybitFetchResult

JsonObject = dict[str, Any]
ZERO = Decimal("0")
UNAVAILABLE_REASONS = frozenset(
    {
        "REGISTRY_DISABLED",
        "NOT_RETURNED_AS_TRADING",
        "STATUS_NOT_TRADING",
        "INVALID_MARKET_RECORD",
        "WRONG_CONTRACT_TYPE",
    }
)


class CapabilityError(ValueError):
    """市场能力快照或源记录不满足安全合同。"""


def _decimal(value: object, *, location: str) -> Decimal:
    if not isinstance(value, str):
        raise CapabilityError(f"{location} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise CapabilityError(f"{location} must be a decimal string") from error
    if not parsed.is_finite() or parsed <= ZERO:
        raise CapabilityError(f"{location} must be finite and positive")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return format(normalized, "f")


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp(value: object, *, location: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CapabilityError(f"{location} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise CapabilityError(f"{location} must be a UTC timestamp") from error
    return parsed


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketCapability:
    market: MarketKind
    pair: str | None
    enabled_by_registry: bool
    available: bool
    unavailable_reason: str | None
    exchange_symbol: str | None
    status: str | None
    contract_type: str | None
    price_tick: Decimal | None
    quantity_step: Decimal | None
    minimum_quantity: Decimal | None
    minimum_notional: Decimal | None
    maximum_limit_quantity: Decimal | None
    maximum_market_quantity: Decimal | None
    funding_interval_minutes: int | None
    configured_max_leverage: Decimal | None
    exchange_max_leverage: Decimal | None
    effective_max_leverage: Decimal | None

    def to_dict(self) -> JsonObject:
        return {
            "market": self.market.value,
            "pair": self.pair,
            "enabled_by_registry": self.enabled_by_registry,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "exchange_symbol": self.exchange_symbol,
            "status": self.status,
            "contract_type": self.contract_type,
            "price_tick": _decimal_text(self.price_tick),
            "quantity_step": _decimal_text(self.quantity_step),
            "minimum_quantity": _decimal_text(self.minimum_quantity),
            "minimum_notional": _decimal_text(self.minimum_notional),
            "maximum_limit_quantity": _decimal_text(self.maximum_limit_quantity),
            "maximum_market_quantity": _decimal_text(self.maximum_market_quantity),
            "funding_interval_minutes": self.funding_interval_minutes,
            "configured_max_leverage": _decimal_text(self.configured_max_leverage),
            "exchange_max_leverage": _decimal_text(self.exchange_max_leverage),
            "effective_max_leverage": _decimal_text(self.effective_max_leverage),
        }


@dataclass(frozen=True, slots=True)
class InstrumentCapability:
    instrument_id: str
    enabled: bool
    spot: MarketCapability
    futures: MarketCapability

    def to_dict(self) -> JsonObject:
        return {
            "instrument_id": self.instrument_id,
            "enabled": self.enabled,
            "spot": self.spot.to_dict(),
            "futures": self.futures.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class MarketCapabilitySnapshot:
    snapshot_id: str
    fetched_at_utc: datetime
    exchange: str
    environment: str
    base_url: str
    endpoint: str
    instrument_registry_sha256: str
    global_max_leverage: Decimal
    response_sha256: str
    page_sha256: Mapping[str, tuple[str, ...]]
    server_time_milliseconds: tuple[int, ...]
    instruments: tuple[InstrumentCapability, ...]
    source_sha256: str

    def available_pairs(self, market: MarketKind | str) -> tuple[str, ...]:
        kind = MarketKind(market)
        capabilities = (
            (item.spot if kind is MarketKind.SPOT else item.futures) for item in self.instruments
        )
        return tuple(item.pair for item in capabilities if item.available and item.pair is not None)

    def capability_for_pair(
        self,
        pair: str,
        market: MarketKind | str,
    ) -> MarketCapability | None:
        kind = MarketKind(market)
        return next(
            (
                capability
                for item in self.instruments
                if (capability := item.spot if kind is MarketKind.SPOT else item.futures).pair
                == pair
            ),
            None,
        )

    def to_dict(self) -> JsonObject:
        available_spot = len(self.available_pairs(MarketKind.SPOT))
        available_futures = len(self.available_pairs(MarketKind.FUTURES))
        unavailable = [
            f"{item.instrument_id}:{capability.market.value}"
            for item in self.instruments
            for capability in (item.spot, item.futures)
            if capability.enabled_by_registry and not capability.available
        ]
        return {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "fetched_at_utc": _utc_text(self.fetched_at_utc),
            "exchange": self.exchange,
            "environment": self.environment,
            "base_url": self.base_url,
            "endpoint": self.endpoint,
            "instrument_registry_sha256": self.instrument_registry_sha256,
            "global_max_leverage": _decimal_text(self.global_max_leverage),
            "source": {
                "response_sha256": self.response_sha256,
                "page_sha256": {
                    category: list(hashes) for category, hashes in sorted(self.page_sha256.items())
                },
                "server_time_milliseconds": list(self.server_time_milliseconds),
            },
            "summary": {
                "instrument_count": len(self.instruments),
                "available_spot_count": available_spot,
                "available_futures_count": available_futures,
                "unavailable_enabled_markets": unavailable,
            },
            "instruments": [item.to_dict() for item in self.instruments],
        }


def _unavailable(
    instrument: Instrument,
    market: MarketKind,
    reason: str,
    *,
    record: Mapping[str, Any] | None = None,
) -> MarketCapability:
    selected = instrument.spot if market is MarketKind.SPOT else instrument.futures
    return MarketCapability(
        market=market,
        pair=selected.pair,
        enabled_by_registry=instrument.market_enabled(market),
        available=False,
        unavailable_reason=reason,
        exchange_symbol=record.get("symbol")
        if record and isinstance(record.get("symbol"), str)
        else None,
        status=record.get("status") if record and isinstance(record.get("status"), str) else None,
        contract_type=(
            record.get("contractType")
            if record and isinstance(record.get("contractType"), str)
            else None
        ),
        price_tick=None,
        quantity_step=None,
        minimum_quantity=None,
        minimum_notional=None,
        maximum_limit_quantity=None,
        maximum_market_quantity=None,
        funding_interval_minutes=None,
        configured_max_leverage=(
            instrument.futures.max_leverage if market is MarketKind.FUTURES else None
        ),
        exchange_max_leverage=None,
        effective_max_leverage=None,
    )


def _record_mapping(record: Mapping[str, Any], key: str, *, location: str) -> Mapping[str, Any]:
    value = record.get(key)
    if not isinstance(value, Mapping):
        raise CapabilityError(f"{location}.{key} must be an object")
    return value


def _build_available_market(
    instrument: Instrument,
    market: MarketKind,
    record: Mapping[str, Any],
    global_max_leverage: Decimal,
) -> MarketCapability:
    selected = instrument.spot if market is MarketKind.SPOT else instrument.futures
    pair = selected.pair
    assert pair is not None
    expected_symbol = pair.replace("/", "").replace(":USDT", "")
    if record.get("status") != "Trading":
        raise CapabilityError("STATUS_NOT_TRADING")
    if (
        record.get("symbol") != expected_symbol
        or record.get("baseCoin") != instrument.instrument_id
        or record.get("quoteCoin") != "USDT"
    ):
        raise CapabilityError("INVALID_MARKET_RECORD")
    if market is MarketKind.FUTURES and (
        record.get("contractType") != "LinearPerpetual"
        or record.get("settleCoin") != "USDT"
        or str(record.get("deliveryTime")) != "0"
    ):
        raise CapabilityError("WRONG_CONTRACT_TYPE")

    price_filter = _record_mapping(record, "priceFilter", location=expected_symbol)
    lot_filter = _record_mapping(record, "lotSizeFilter", location=expected_symbol)
    price_tick = _decimal(price_filter.get("tickSize"), location=f"{expected_symbol}.tickSize")
    if market is MarketKind.SPOT:
        quantity_step = _decimal(
            lot_filter.get("basePrecision"), location=f"{expected_symbol}.basePrecision"
        )
        minimum_quantity = _decimal(
            lot_filter.get("minOrderQty"), location=f"{expected_symbol}.minOrderQty"
        )
        minimum_notional = _decimal(
            lot_filter.get("minOrderAmt"), location=f"{expected_symbol}.minOrderAmt"
        )
        maximum_limit_quantity = _decimal(
            lot_filter.get("maxLimitOrderQty"),
            location=f"{expected_symbol}.maxLimitOrderQty",
        )
        maximum_market_quantity = _decimal(
            lot_filter.get("maxMarketOrderQty"),
            location=f"{expected_symbol}.maxMarketOrderQty",
        )
        funding_interval = None
        configured_max = None
        exchange_max = None
        effective_max = None
    else:
        quantity_step = _decimal(lot_filter.get("qtyStep"), location=f"{expected_symbol}.qtyStep")
        minimum_quantity = _decimal(
            lot_filter.get("minOrderQty"), location=f"{expected_symbol}.minOrderQty"
        )
        minimum_notional = _decimal(
            lot_filter.get("minNotionalValue"),
            location=f"{expected_symbol}.minNotionalValue",
        )
        maximum_limit_quantity = _decimal(
            lot_filter.get("maxOrderQty"), location=f"{expected_symbol}.maxOrderQty"
        )
        maximum_market_quantity = _decimal(
            lot_filter.get("maxMktOrderQty"), location=f"{expected_symbol}.maxMktOrderQty"
        )
        leverage_filter = _record_mapping(record, "leverageFilter", location=expected_symbol)
        exchange_max = _decimal(
            leverage_filter.get("maxLeverage"),
            location=f"{expected_symbol}.maxLeverage",
        )
        configured_max = instrument.futures.max_leverage
        assert configured_max is not None
        effective_max = min(global_max_leverage, configured_max, exchange_max)
        funding_interval = record.get("fundingInterval")
        if type(funding_interval) is not int or funding_interval <= 0:
            raise CapabilityError(f"{expected_symbol}.fundingInterval must be positive integer")

    return MarketCapability(
        market=market,
        pair=pair,
        enabled_by_registry=True,
        available=True,
        unavailable_reason=None,
        exchange_symbol=expected_symbol,
        status="Trading",
        contract_type=("LinearPerpetual" if market is MarketKind.FUTURES else None),
        price_tick=price_tick,
        quantity_step=quantity_step,
        minimum_quantity=minimum_quantity,
        minimum_notional=minimum_notional,
        maximum_limit_quantity=maximum_limit_quantity,
        maximum_market_quantity=maximum_market_quantity,
        funding_interval_minutes=funding_interval,
        configured_max_leverage=configured_max,
        exchange_max_leverage=exchange_max,
        effective_max_leverage=effective_max,
    )


def _market_capability(
    instrument: Instrument,
    market: MarketKind,
    records: Mapping[str, Mapping[str, Any]],
    duplicates: frozenset[str],
    global_max_leverage: Decimal,
) -> MarketCapability:
    if not instrument.market_enabled(market):
        return _unavailable(instrument, market, "REGISTRY_DISABLED")
    selected = instrument.spot if market is MarketKind.SPOT else instrument.futures
    assert selected.pair is not None
    symbol = selected.pair.replace("/", "").replace(":USDT", "")
    record = records.get(symbol)
    if record is None:
        return _unavailable(instrument, market, "NOT_RETURNED_AS_TRADING")
    if symbol in duplicates:
        return _unavailable(instrument, market, "INVALID_MARKET_RECORD", record=record)
    try:
        return _build_available_market(instrument, market, record, global_max_leverage)
    except CapabilityError as error:
        reason = str(error)
        if reason not in UNAVAILABLE_REASONS:
            reason = "INVALID_MARKET_RECORD"
        return _unavailable(instrument, market, reason, record=record)


def _record_index(records: tuple[JsonObject, ...]) -> tuple[dict[str, JsonObject], frozenset[str]]:
    indexed: dict[str, JsonObject] = {}
    duplicates: set[str] = set()
    for record in records:
        symbol = record.get("symbol")
        if not isinstance(symbol, str):
            continue
        if symbol in indexed:
            duplicates.add(symbol)
        else:
            indexed[symbol] = record
    return indexed, frozenset(duplicates)


def build_market_capability_snapshot(
    registry: InstrumentRegistry,
    fetched: BybitFetchResult,
    *,
    global_max_leverage: Decimal,
    environment: str = "mainnet",
) -> MarketCapabilitySnapshot:
    """单个市场记录失败只禁用该市场；请求级失败由客户端整体 fail-closed。"""

    if not isinstance(global_max_leverage, Decimal) or not global_max_leverage.is_finite():
        raise CapabilityError("global_max_leverage must be a finite Decimal")
    if global_max_leverage <= ZERO:
        raise CapabilityError("global_max_leverage must be positive")
    if fetched.base_url != "https://api.bybit.com" or environment != "mainnet":
        raise CapabilityError("R1-03 checked-in snapshot must use Bybit mainnet public data")

    spot_records, spot_duplicates = _record_index(fetched.records.get("spot", ()))
    linear_records, linear_duplicates = _record_index(fetched.records.get("linear", ()))
    instruments: list[InstrumentCapability] = []
    for instrument in registry.instruments:
        instruments.append(
            InstrumentCapability(
                instrument_id=instrument.instrument_id,
                enabled=instrument.enabled,
                spot=_market_capability(
                    instrument,
                    MarketKind.SPOT,
                    spot_records,
                    spot_duplicates,
                    global_max_leverage,
                ),
                futures=_market_capability(
                    instrument,
                    MarketKind.FUTURES,
                    linear_records,
                    linear_duplicates,
                    global_max_leverage,
                ),
            )
        )

    flattened_hashes = [
        digest
        for category in sorted(fetched.page_sha256)
        for digest in fetched.page_sha256[category]
    ]
    response_sha256 = _canonical_sha256(flattened_hashes)
    provisional = MarketCapabilitySnapshot(
        snapshot_id="",
        fetched_at_utc=fetched.fetched_at_utc,
        exchange="bybit",
        environment=environment,
        base_url=fetched.base_url,
        endpoint=fetched.endpoint,
        instrument_registry_sha256=registry.source_sha256,
        global_max_leverage=global_max_leverage,
        response_sha256=response_sha256,
        page_sha256=fetched.page_sha256,
        server_time_milliseconds=fetched.server_time_milliseconds,
        instruments=tuple(instruments),
        source_sha256="",
    )
    document = provisional.to_dict()
    document.pop("snapshot_id")
    identity = _canonical_sha256(document)
    snapshot_id = (
        f"capability-{fetched.fetched_at_utc.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{identity[:12]}"
    )
    snapshot = MarketCapabilitySnapshot(
        snapshot_id=snapshot_id,
        fetched_at_utc=provisional.fetched_at_utc,
        exchange=provisional.exchange,
        environment=provisional.environment,
        base_url=provisional.base_url,
        endpoint=provisional.endpoint,
        instrument_registry_sha256=provisional.instrument_registry_sha256,
        global_max_leverage=provisional.global_max_leverage,
        response_sha256=provisional.response_sha256,
        page_sha256=provisional.page_sha256,
        server_time_milliseconds=provisional.server_time_milliseconds,
        instruments=provisional.instruments,
        source_sha256="",
    )
    final_document = snapshot.to_dict()
    return MarketCapabilitySnapshot(
        snapshot_id=snapshot.snapshot_id,
        fetched_at_utc=snapshot.fetched_at_utc,
        exchange=snapshot.exchange,
        environment=snapshot.environment,
        base_url=snapshot.base_url,
        endpoint=snapshot.endpoint,
        instrument_registry_sha256=snapshot.instrument_registry_sha256,
        global_max_leverage=snapshot.global_max_leverage,
        response_sha256=snapshot.response_sha256,
        page_sha256=snapshot.page_sha256,
        server_time_milliseconds=snapshot.server_time_milliseconds,
        instruments=snapshot.instruments,
        source_sha256=_canonical_sha256(final_document),
    )


def _optional_decimal(value: object, *, location: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, location=location)


def _parse_market(document: Mapping[str, Any], *, location: str) -> MarketCapability:
    expected = {
        "market",
        "pair",
        "enabled_by_registry",
        "available",
        "unavailable_reason",
        "exchange_symbol",
        "status",
        "contract_type",
        "price_tick",
        "quantity_step",
        "minimum_quantity",
        "minimum_notional",
        "maximum_limit_quantity",
        "maximum_market_quantity",
        "funding_interval_minutes",
        "configured_max_leverage",
        "exchange_max_leverage",
        "effective_max_leverage",
    }
    if set(document) != expected:
        raise CapabilityError(f"{location} fields do not match capability contract")
    try:
        market = MarketKind(document["market"])
    except (TypeError, ValueError):
        raise CapabilityError(f"{location}.market is invalid") from None
    pair = document["pair"]
    if pair is not None and (
        not isinstance(pair, str)
        or re.fullmatch(r"[A-Z][A-Z0-9]{1,15}/USDT(?::USDT)?", pair) is None
    ):
        raise CapabilityError(f"{location}.pair is invalid")
    if type(document["enabled_by_registry"]) is not bool or type(document["available"]) is not bool:
        raise CapabilityError(f"{location} flags must be boolean")
    reason = document["unavailable_reason"]
    if reason is not None and reason not in UNAVAILABLE_REASONS:
        raise CapabilityError(f"{location}.unavailable_reason is invalid")
    for key in ("exchange_symbol", "status", "contract_type"):
        value = document[key]
        if value is not None and not isinstance(value, str):
            raise CapabilityError(f"{location}.{key} is invalid")
    capability = MarketCapability(
        market=market,
        pair=pair,
        enabled_by_registry=document["enabled_by_registry"],
        available=document["available"],
        unavailable_reason=reason,
        exchange_symbol=document["exchange_symbol"],
        status=document["status"],
        contract_type=document["contract_type"],
        price_tick=_optional_decimal(document["price_tick"], location=f"{location}.price_tick"),
        quantity_step=_optional_decimal(
            document["quantity_step"], location=f"{location}.quantity_step"
        ),
        minimum_quantity=_optional_decimal(
            document["minimum_quantity"], location=f"{location}.minimum_quantity"
        ),
        minimum_notional=_optional_decimal(
            document["minimum_notional"], location=f"{location}.minimum_notional"
        ),
        maximum_limit_quantity=_optional_decimal(
            document["maximum_limit_quantity"],
            location=f"{location}.maximum_limit_quantity",
        ),
        maximum_market_quantity=_optional_decimal(
            document["maximum_market_quantity"],
            location=f"{location}.maximum_market_quantity",
        ),
        funding_interval_minutes=document["funding_interval_minutes"],
        configured_max_leverage=_optional_decimal(
            document["configured_max_leverage"],
            location=f"{location}.configured_max_leverage",
        ),
        exchange_max_leverage=_optional_decimal(
            document["exchange_max_leverage"],
            location=f"{location}.exchange_max_leverage",
        ),
        effective_max_leverage=_optional_decimal(
            document["effective_max_leverage"],
            location=f"{location}.effective_max_leverage",
        ),
    )
    if capability.pair is not None:
        expected_pair_pattern = (
            r"[A-Z][A-Z0-9]{1,15}/USDT"
            if market is MarketKind.SPOT
            else r"[A-Z][A-Z0-9]{1,15}/USDT:USDT"
        )
        if re.fullmatch(expected_pair_pattern, capability.pair) is None:
            raise CapabilityError(f"{location}.pair does not match market type")
    numeric = (
        capability.price_tick,
        capability.quantity_step,
        capability.minimum_quantity,
        capability.minimum_notional,
        capability.maximum_limit_quantity,
        capability.maximum_market_quantity,
    )
    if capability.available:
        if capability.pair is None:
            raise CapabilityError(f"{location} available capability has no pair")
        expected_symbol = capability.pair.replace("/", "").replace(":USDT", "")
        if (
            reason is not None
            or capability.exchange_symbol != expected_symbol
            or capability.status != "Trading"
            or any(value is None for value in numeric)
        ):
            raise CapabilityError(f"{location} available capability is incomplete")
        if market is MarketKind.FUTURES:
            if (
                type(capability.funding_interval_minutes) is not int
                or capability.funding_interval_minutes <= 0
                or capability.configured_max_leverage is None
                or capability.exchange_max_leverage is None
                or capability.effective_max_leverage is None
                or capability.contract_type != "LinearPerpetual"
            ):
                raise CapabilityError(f"{location} futures capability is incomplete")
        elif any(
            value is not None
            for value in (
                capability.funding_interval_minutes,
                capability.configured_max_leverage,
                capability.exchange_max_leverage,
                capability.effective_max_leverage,
                capability.contract_type,
            )
        ):
            raise CapabilityError(f"{location} spot futures-only fields must be null")
    elif reason is None or any(value is not None for value in numeric):
        raise CapabilityError(f"{location} unavailable capability is inconsistent")
    return capability


def parse_market_capability_snapshot(
    document: Mapping[str, Any],
    *,
    registry: InstrumentRegistry | None = None,
    source_sha256: str | None = None,
) -> MarketCapabilitySnapshot:
    required = {
        "schema_version",
        "snapshot_id",
        "fetched_at_utc",
        "exchange",
        "environment",
        "base_url",
        "endpoint",
        "instrument_registry_sha256",
        "global_max_leverage",
        "source",
        "summary",
        "instruments",
    }
    if set(document) != required or document.get("schema_version") != 1:
        raise CapabilityError("market capability snapshot fields do not match schema v1")
    snapshot_id = document["snapshot_id"]
    if (
        not isinstance(snapshot_id, str)
        or re.fullmatch(r"capability-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", snapshot_id) is None
    ):
        raise CapabilityError("market capability snapshot_id is invalid")
    fetched_at = _timestamp(document["fetched_at_utc"], location="fetched_at_utc")
    registry_sha = document["instrument_registry_sha256"]
    if not isinstance(registry_sha, str) or re.fullmatch(r"[a-f0-9]{64}", registry_sha) is None:
        raise CapabilityError("instrument_registry_sha256 is invalid")
    if registry is not None and registry_sha != registry.source_sha256:
        raise CapabilityError("market capability snapshot does not match Instrument Registry")
    source = document["source"]
    if not isinstance(source, Mapping) or set(source) != {
        "response_sha256",
        "page_sha256",
        "server_time_milliseconds",
    }:
        raise CapabilityError("market capability source evidence is invalid")
    page_sha = source["page_sha256"]
    if not isinstance(page_sha, Mapping) or set(page_sha) != {"spot", "linear"}:
        raise CapabilityError("market capability page hashes are invalid")
    parsed_page_sha: dict[str, tuple[str, ...]] = {}
    for category in ("spot", "linear"):
        values = page_sha[category]
        if (
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in values
            )
        ):
            raise CapabilityError("market capability page hashes are invalid")
        parsed_page_sha[category] = tuple(values)
    server_times = source["server_time_milliseconds"]
    if (
        not isinstance(server_times, list)
        or not server_times
        or any(type(value) is not int or value <= 0 for value in server_times)
    ):
        raise CapabilityError("market capability server times are invalid")
    raw_instruments = document["instruments"]
    if not isinstance(raw_instruments, list) or not 1 <= len(raw_instruments) <= 50:
        raise CapabilityError("market capability instruments are invalid")
    instruments: list[InstrumentCapability] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_instruments):
        if not isinstance(raw_item, Mapping) or set(raw_item) != {
            "instrument_id",
            "enabled",
            "spot",
            "futures",
        }:
            raise CapabilityError("market capability instrument fields are invalid")
        instrument_id = raw_item["instrument_id"]
        if not isinstance(instrument_id, str) or instrument_id in seen_ids:
            raise CapabilityError("market capability instrument ids must be unique")
        seen_ids.add(instrument_id)
        spot = raw_item["spot"]
        futures = raw_item["futures"]
        if not isinstance(spot, Mapping) or not isinstance(futures, Mapping):
            raise CapabilityError("market capabilities must be objects")
        if type(raw_item["enabled"]) is not bool:
            raise CapabilityError("market capability instrument enabled flag must be boolean")
        instruments.append(
            InstrumentCapability(
                instrument_id=instrument_id,
                enabled=raw_item["enabled"],
                spot=_parse_market(spot, location=f"instruments[{index}].spot"),
                futures=_parse_market(futures, location=f"instruments[{index}].futures"),
            )
        )
    if (
        document["exchange"] != "bybit"
        or document["environment"] != "mainnet"
        or document["base_url"] != "https://api.bybit.com"
        or document["endpoint"] != "/v5/market/instruments-info"
    ):
        raise CapabilityError("market capability source identity is invalid")
    response_sha = source["response_sha256"]
    if not isinstance(response_sha, str) or re.fullmatch(r"[a-f0-9]{64}", response_sha) is None:
        raise CapabilityError("market capability response_sha256 is invalid")
    flattened_hashes = [
        digest for category in sorted(parsed_page_sha) for digest in parsed_page_sha[category]
    ]
    if response_sha != _canonical_sha256(flattened_hashes):
        raise CapabilityError("market capability response evidence hash is inconsistent")
    global_max_leverage = _decimal(document["global_max_leverage"], location="global_max_leverage")
    snapshot = MarketCapabilitySnapshot(
        snapshot_id=snapshot_id,
        fetched_at_utc=fetched_at,
        exchange=str(document["exchange"]),
        environment=str(document["environment"]),
        base_url=str(document["base_url"]),
        endpoint=str(document["endpoint"]),
        instrument_registry_sha256=registry_sha,
        global_max_leverage=global_max_leverage,
        response_sha256=response_sha,
        page_sha256=parsed_page_sha,
        server_time_milliseconds=tuple(server_times),
        instruments=tuple(instruments),
        source_sha256=source_sha256 or _canonical_sha256(document),
    )
    for item in snapshot.instruments:
        capability = item.futures
        if capability.available:
            assert capability.configured_max_leverage is not None
            assert capability.exchange_max_leverage is not None
            if capability.effective_max_leverage != min(
                global_max_leverage,
                capability.configured_max_leverage,
                capability.exchange_max_leverage,
            ):
                raise CapabilityError("effective futures leverage formula is invalid")
    if registry is not None:
        expected_ids = tuple(item.instrument_id for item in registry.instruments)
        if tuple(item.instrument_id for item in snapshot.instruments) != expected_ids:
            raise CapabilityError("market capability instruments do not match Instrument Registry")
        for item, configured in zip(snapshot.instruments, registry.instruments, strict=True):
            if item.enabled != configured.enabled:
                raise CapabilityError(
                    "market capability enablement does not match Instrument Registry"
                )
            for capability, market in (
                (item.spot, MarketKind.SPOT),
                (item.futures, MarketKind.FUTURES),
            ):
                selected = configured.spot if market is MarketKind.SPOT else configured.futures
                if (
                    capability.market is not market
                    or capability.pair != selected.pair
                    or capability.enabled_by_registry != configured.market_enabled(market)
                ):
                    raise CapabilityError(
                        "market capability market mapping does not match Instrument Registry"
                    )
                if market is MarketKind.FUTURES and (
                    capability.configured_max_leverage != configured.futures.max_leverage
                ):
                    raise CapabilityError(
                        "market capability leverage does not match Instrument Registry"
                    )
    expected_document = snapshot.to_dict()
    expected_document.pop("snapshot_id")
    expected_identity = _canonical_sha256(expected_document)[:12]
    if not snapshot_id.endswith(expected_identity):
        raise CapabilityError("market capability snapshot content hash does not match snapshot_id")
    if dict(document) != snapshot.to_dict():
        raise CapabilityError("market capability snapshot contains inconsistent derived fields")
    return snapshot


def load_market_capability_snapshot(
    path: str | Path,
    *,
    registry: InstrumentRegistry | None = None,
    now_utc: datetime | None = None,
    maximum_age: timedelta | None = None,
) -> MarketCapabilitySnapshot:
    snapshot_path = Path(path)
    try:
        payload = snapshot_path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CapabilityError("market capability snapshot could not be read") from None
    if not isinstance(document, Mapping):
        raise CapabilityError("market capability snapshot must be an object")
    snapshot = parse_market_capability_snapshot(
        document,
        registry=registry,
        source_sha256=hashlib.sha256(payload).hexdigest(),
    )
    if (now_utc is None) is not (maximum_age is None):
        raise ValueError("now_utc and maximum_age must be provided together")
    if now_utc is not None and maximum_age is not None:
        if now_utc.tzinfo is None or now_utc.utcoffset() is None or maximum_age <= timedelta(0):
            raise ValueError("freshness inputs are invalid")
        age = now_utc.astimezone(UTC) - snapshot.fetched_at_utc
        if age < timedelta(0) or age > maximum_age:
            raise CapabilityError("market capability snapshot is stale or from the future")
    return snapshot
