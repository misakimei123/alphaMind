"""从 Instrument Registry 生成确定性的 Freqtrade 配置片段。"""

from __future__ import annotations

import json

from alphamind.config.instruments import InstrumentRegistry, MarketKind
from alphamind.market.capabilities import MarketCapabilitySnapshot


def build_freqtrade_instrument_overlay(
    registry: InstrumentRegistry,
    market: MarketKind | str,
    market_capabilities: MarketCapabilitySnapshot | None = None,
) -> dict[str, object]:
    """生成 StaticPairList 所需的最小配置；不复制精度或杠杆能力。"""

    kind = MarketKind(market)
    if market_capabilities is not None:
        if market_capabilities.instrument_registry_sha256 != registry.source_sha256:
            raise ValueError("Market Capability does not match Instrument Registry")
        pairs = list(market_capabilities.available_pairs(kind))
    else:
        pairs = list(registry.enabled_pairs(kind))
    if not pairs:
        raise ValueError(f"Instrument Registry has no enabled {kind.value} pairs")
    return {"exchange": {"pair_whitelist": pairs}}


def render_freqtrade_instrument_overlay(
    registry: InstrumentRegistry,
    market: MarketKind | str,
    market_capabilities: MarketCapabilitySnapshot | None = None,
) -> str:
    return (
        json.dumps(
            build_freqtrade_instrument_overlay(registry, market, market_capabilities),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
