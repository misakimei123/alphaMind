"""配置化交易标的 Registry 及其运行时查询接口。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

JsonObject = dict[str, Any]
PAIR_PATTERN = re.compile(r"^(?P<base>[A-Z][A-Z0-9]{1,15})/USDT(?P<settle>:USDT)?$")


class InstrumentRegistryError(ValueError):
    """Registry 结构或业务约束无效。"""


class MarketKind(StrEnum):
    SPOT = "spot"
    FUTURES = "futures"


@dataclass(frozen=True, slots=True)
class SpotMarket:
    enabled: bool
    pair: str | None


@dataclass(frozen=True, slots=True)
class FuturesMarket:
    enabled: bool
    pair: str | None
    allow_long: bool
    allow_short: bool
    max_leverage: Decimal | None


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    enabled: bool
    spot: SpotMarket
    futures: FuturesMarket

    def market_enabled(self, market: MarketKind) -> bool:
        selected = self.spot if market is MarketKind.SPOT else self.futures
        return self.enabled and selected.enabled

    def pair(self, market: MarketKind) -> str | None:
        selected = self.spot if market is MarketKind.SPOT else self.futures
        return selected.pair if self.enabled and selected.enabled else None


@dataclass(frozen=True, slots=True)
class InstrumentRegistry:
    schema_version: int
    exchange: str
    quote_currency: str
    instruments: tuple[Instrument, ...]
    source_sha256: str

    def enabled_instruments(self, market: MarketKind | str) -> tuple[Instrument, ...]:
        kind = MarketKind(market)
        return tuple(item for item in self.instruments if item.market_enabled(kind))

    def enabled_pairs(self, market: MarketKind | str) -> tuple[str, ...]:
        kind = MarketKind(market)
        return tuple(
            pair for item in self.enabled_instruments(kind) if (pair := item.pair(kind)) is not None
        )

    def get(self, instrument_id: str) -> Instrument | None:
        return next(
            (item for item in self.instruments if item.instrument_id == instrument_id),
            None,
        )

    def instrument_for_pair(
        self,
        pair: str,
        market: MarketKind | str,
    ) -> Instrument | None:
        kind = MarketKind(market)
        return next(
            (item for item in self.enabled_instruments(kind) if item.pair(kind) == pair),
            None,
        )


def _require_mapping(value: object, *, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InstrumentRegistryError(f"{location} must be an object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    location: str,
) -> None:
    if set(value) != expected:
        raise InstrumentRegistryError(f"{location} fields do not match the registry contract")


def _require_bool(value: object, *, location: str) -> bool:
    if type(value) is not bool:
        raise InstrumentRegistryError(f"{location} must be boolean")
    return value


def _optional_pair(
    value: object,
    *,
    location: str,
    instrument_id: str,
    market: MarketKind,
    enabled: bool,
) -> str | None:
    if not enabled:
        if value is not None:
            raise InstrumentRegistryError(f"{location} must be null when disabled")
        return None
    if not isinstance(value, str):
        raise InstrumentRegistryError(f"{location} must be a pair string when enabled")
    matched = PAIR_PATTERN.fullmatch(value)
    expected_settle = market is MarketKind.FUTURES
    if (
        matched is None
        or matched.group("base") != instrument_id
        or bool(matched.group("settle")) is not expected_settle
    ):
        raise InstrumentRegistryError(
            f"{location} must match instrument id and the {market.value} USDT format"
        )
    return value


def _optional_leverage(value: object, *, location: str, enabled: bool) -> Decimal | None:
    if not enabled:
        if value is not None:
            raise InstrumentRegistryError(f"{location} must be null when disabled")
        return None
    if not isinstance(value, str):
        raise InstrumentRegistryError(f"{location} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise InstrumentRegistryError(f"{location} must be a decimal string") from error
    if not parsed.is_finite() or parsed <= 0:
        raise InstrumentRegistryError(f"{location} must be finite and positive")
    return parsed


def _canonical_sha256(document: Mapping[str, Any]) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_instrument_registry(
    document: Mapping[str, Any],
    *,
    source_sha256: str | None = None,
) -> InstrumentRegistry:
    """把已读取文档转成不可变 Registry，并复核跨记录业务约束。"""

    root = _require_mapping(document, location="instrument registry")
    _require_exact_keys(
        root,
        {"schema_version", "exchange", "quote_currency", "instruments"},
        location="instrument registry",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise InstrumentRegistryError("instrument registry schema_version must be 1")
    if root["exchange"] != "bybit":
        raise InstrumentRegistryError("instrument registry exchange must be bybit")
    if root["quote_currency"] != "USDT":
        raise InstrumentRegistryError("instrument registry quote_currency must be USDT")
    raw_instruments = root["instruments"]
    if not isinstance(raw_instruments, list) or not 1 <= len(raw_instruments) <= 50:
        raise InstrumentRegistryError("instrument registry must contain 1 to 50 instruments")

    instruments: list[Instrument] = []
    ids: set[str] = set()
    pairs: set[str] = set()
    for index, raw_item in enumerate(raw_instruments):
        location = f"instruments[{index}]"
        item = _require_mapping(raw_item, location=location)
        _require_exact_keys(item, {"id", "enabled", "spot", "futures"}, location=location)
        instrument_id = item["id"]
        if not isinstance(instrument_id, str) or not re.fullmatch(
            r"[A-Z][A-Z0-9]{1,15}", instrument_id
        ):
            raise InstrumentRegistryError(f"{location}.id is invalid")
        if instrument_id in ids:
            raise InstrumentRegistryError("instrument ids must be unique")
        ids.add(instrument_id)
        enabled = _require_bool(item["enabled"], location=f"{location}.enabled")

        raw_spot = _require_mapping(item["spot"], location=f"{location}.spot")
        _require_exact_keys(raw_spot, {"enabled", "pair"}, location=f"{location}.spot")
        spot_enabled = _require_bool(raw_spot["enabled"], location=f"{location}.spot.enabled")
        spot_pair = _optional_pair(
            raw_spot["pair"],
            location=f"{location}.spot.pair",
            instrument_id=instrument_id,
            market=MarketKind.SPOT,
            enabled=spot_enabled,
        )

        raw_futures = _require_mapping(item["futures"], location=f"{location}.futures")
        _require_exact_keys(
            raw_futures,
            {"enabled", "pair", "allow_long", "allow_short", "max_leverage"},
            location=f"{location}.futures",
        )
        futures_enabled = _require_bool(
            raw_futures["enabled"], location=f"{location}.futures.enabled"
        )
        futures_pair = _optional_pair(
            raw_futures["pair"],
            location=f"{location}.futures.pair",
            instrument_id=instrument_id,
            market=MarketKind.FUTURES,
            enabled=futures_enabled,
        )
        allow_long = _require_bool(
            raw_futures["allow_long"], location=f"{location}.futures.allow_long"
        )
        allow_short = _require_bool(
            raw_futures["allow_short"], location=f"{location}.futures.allow_short"
        )
        max_leverage = _optional_leverage(
            raw_futures["max_leverage"],
            location=f"{location}.futures.max_leverage",
            enabled=futures_enabled,
        )
        if futures_enabled and not (allow_long or allow_short):
            raise InstrumentRegistryError(
                f"{location}.futures must allow at least one direction when enabled"
            )
        if not futures_enabled and (allow_long or allow_short):
            raise InstrumentRegistryError(
                f"{location}.futures directions must be false when disabled"
            )
        if enabled and not (spot_enabled or futures_enabled):
            raise InstrumentRegistryError(
                f"enabled instrument {instrument_id} has no enabled market"
            )
        for pair in (spot_pair, futures_pair):
            if pair is not None and pair in pairs:
                raise InstrumentRegistryError("enabled instrument pairs must be unique")
            if pair is not None:
                pairs.add(pair)

        instruments.append(
            Instrument(
                instrument_id=instrument_id,
                enabled=enabled,
                spot=SpotMarket(spot_enabled, spot_pair),
                futures=FuturesMarket(
                    futures_enabled,
                    futures_pair,
                    allow_long,
                    allow_short,
                    max_leverage,
                ),
            )
        )

    digest = source_sha256 or _canonical_sha256(root)
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise InstrumentRegistryError("instrument registry source_sha256 is invalid")
    return InstrumentRegistry(
        schema_version=1,
        exchange="bybit",
        quote_currency="USDT",
        instruments=tuple(instruments),
        source_sha256=digest,
    )


def load_instrument_registry(path: str | Path) -> InstrumentRegistry:
    """从 YAML 文件加载 Registry；错误不会回显文档中的原始值。"""

    registry_path = Path(path)
    try:
        payload = registry_path.read_bytes()
        document = yaml.safe_load(payload.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise InstrumentRegistryError(
            "instrument registry could not be read as UTF-8 YAML"
        ) from None
    if not isinstance(document, Mapping):
        raise InstrumentRegistryError("instrument registry must be a YAML object")
    return parse_instrument_registry(
        document,
        source_sha256=hashlib.sha256(payload).hexdigest(),
    )
