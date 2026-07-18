from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from alphamind.config import (
    InstrumentRegistryError,
    MarketKind,
    load_effective_config,
    load_instrument_registry,
    parse_instrument_registry,
)
from alphamind.config.freqtrade import (
    build_freqtrade_instrument_overlay,
    render_freqtrade_instrument_overlay,
)
from scripts.render_instrument_configs import main as render_main

PROJECT_ROOT = Path(__file__).parents[2]
REGISTRY_PATH = PROJECT_ROOT / "configs" / "alphamind" / "instruments.example.yaml"


def _document() -> dict[str, object]:
    document = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _xrp() -> dict[str, object]:
    return {
        "id": "XRP",
        "enabled": True,
        "spot": {"enabled": True, "pair": "XRP/USDT"},
        "futures": {
            "enabled": True,
            "pair": "XRP/USDT:USDT",
            "allow_long": True,
            "allow_short": False,
            "max_leverage": "1.5",
        },
    }


def test_registry_exposes_typed_spot_and_futures_queries() -> None:
    registry = load_instrument_registry(REGISTRY_PATH)

    assert registry.exchange == "bybit"
    assert registry.quote_currency == "USDT"
    assert len(registry.source_sha256) == 64
    assert registry.enabled_pairs(MarketKind.SPOT) == (
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "HYPE/USDT",
    )
    assert registry.enabled_pairs(MarketKind.FUTURES) == (
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "HYPE/USDT:USDT",
    )
    hype = registry.get("HYPE")
    assert hype is not None
    assert hype.futures.max_leverage == Decimal("1")
    assert registry.instrument_for_pair("SOL/USDT", MarketKind.SPOT).instrument_id == "SOL"


def test_effective_config_and_generated_freqtrade_pairlist_share_one_registry() -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    registry = effective.instrument_registry
    capability = effective.market_capability_snapshot
    generated = build_freqtrade_instrument_overlay(registry, MarketKind.SPOT, capability)
    generated_futures = build_freqtrade_instrument_overlay(
        registry,
        MarketKind.FUTURES,
        capability,
    )

    assert generated == {
        "exchange": {"pair_whitelist": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "HYPE/USDT"]}
    }
    assert generated_futures == {
        "exchange": {
            "pair_whitelist": [
                "BTC/USDT:USDT",
                "ETH/USDT:USDT",
                "SOL/USDT:USDT",
                "HYPE/USDT:USDT",
            ]
        }
    }
    checked_in_spot = (
        PROJECT_ROOT / "configs" / "freqtrade" / "spot-instruments.generated.json"
    ).read_text(encoding="utf-8")
    checked_in_futures = (
        PROJECT_ROOT / "configs" / "freqtrade" / "futures-instruments.generated.json"
    ).read_text(encoding="utf-8")
    assert checked_in_spot == render_freqtrade_instrument_overlay(
        registry,
        MarketKind.SPOT,
        capability,
    )
    assert checked_in_futures == render_freqtrade_instrument_overlay(
        registry,
        MarketKind.FUTURES,
        capability,
    )
    assert render_main(["--project-root", str(PROJECT_ROOT), "--check"]) == 0


def test_adding_or_disabling_instrument_requires_only_registry_data_changes() -> None:
    document = _document()
    rows = document["instruments"]
    assert isinstance(rows, list)
    rows.append(_xrp())

    extended = parse_instrument_registry(document)

    assert extended.enabled_pairs(MarketKind.SPOT)[-1] == "XRP/USDT"
    assert extended.enabled_pairs(MarketKind.FUTURES)[-1] == "XRP/USDT:USDT"
    assert (
        build_freqtrade_instrument_overlay(extended, MarketKind.SPOT)["exchange"]["pair_whitelist"][
            -1
        ]
        == "XRP/USDT"
    )

    disabled_document = deepcopy(document)
    disabled_rows = disabled_document["instruments"]
    assert isinstance(disabled_rows, list)
    sol = next(row for row in disabled_rows if row["id"] == "SOL")
    sol["enabled"] = False
    disabled = parse_instrument_registry(disabled_document)

    assert "SOL/USDT" not in disabled.enabled_pairs(MarketKind.SPOT)
    assert "SOL/USDT:USDT" not in disabled.enabled_pairs(MarketKind.FUTURES)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda rows: rows[1].update({"id": "BTC"}),
            "instrument ids must be unique",
        ),
        (
            lambda rows: rows[2]["spot"].update({"pair": "XRP/USDT"}),
            "must match instrument id",
        ),
        (
            lambda rows: rows[2]["futures"].update({"allow_long": False, "allow_short": False}),
            "must allow at least one direction",
        ),
    ],
)
def test_registry_business_contract_fails_closed(mutate: object, message: str) -> None:
    document = _document()
    rows = document["instruments"]
    assert isinstance(rows, list)
    assert callable(mutate)
    mutate(rows)

    with pytest.raises(InstrumentRegistryError, match=message):
        parse_instrument_registry(document)
