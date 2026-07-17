from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from alphamind.risk.freqtrade_adapter import (
    calculate_initial_stop_price,
    calculate_runtime_entry_approval,
    fixed_stoploss_ratio,
    load_freqtrade_risk_config,
)
from alphamind.risk.position_sizing import RiskContextSource, calculate_position_size
from alphamind.risk.watchdog import SnapshotReadResult

PROJECT_ROOT = Path(__file__).parents[2]
RISK_CONFIG_PATH = PROJECT_ROOT / "configs/common/freqtrade-risk-adapter.toml"


def snapshot_result(
    *,
    entry_allowed: bool = True,
    pending_exposure: str = "20",
) -> SnapshotReadResult:
    snapshot: dict[str, object] = {
        "snapshot_id": "risk-20260717T120000Z-0123456789ab",
        "expires_at_utc": "2026-07-17T12:01:00Z",
        "accounting": {
            "nav": "500",
            "positions": [
                {
                    "pair": "BTC/USDT",
                    "marked_value": "100",
                }
            ],
        },
        "exposure": {
            "open_exposure_quote": "100",
            "pending_entry_exposure_quote": pending_exposure,
            "available_balance_quote": "300",
        },
        "thresholds": {"trade_risk_fraction": "0.0025"},
    }
    return SnapshotReadResult(
        snapshot=snapshot,
        entry_allowed=entry_allowed,
        close_only=not entry_allowed,
        kill_switch=False,
        safe_exit_allowed=True,
        reason_codes=("risk_checks_passed" if entry_allowed else "snapshot_stale",),
    )


def test_runtime_config_matches_frozen_research_and_exchange_inputs() -> None:
    config = load_freqtrade_risk_config(RISK_CONFIG_PATH)
    walk_forward = (PROJECT_ROOT / "configs/research/walk-forward-v1.toml").read_text(
        encoding="utf-8"
    )
    execution = (PROJECT_ROOT / "configs/research/execution-model-v1.toml").read_text(
        encoding="utf-8"
    )
    metadata = json.loads(
        (
            PROJECT_ROOT / "data/manifests/source/"
            "bybit-spot-ohlcv-20260716T070451Z-ef232b839406.exchange-metadata.json"
        ).read_text(encoding="utf-8")
    )

    assert config.atr_period == 20
    assert config.stop_multiple == Decimal("2.0")
    assert config.maximum_holding_time_enabled is False
    for value in (
        'symbol_exposure_fraction = "0.40"',
        'directional_exposure_fraction = "0.70"',
        'volatility_cap_fraction = "0.40"',
        'maximum_unit_loss_fraction = "0.50"',
        'gap_buffer_rate = "0.002"',
    ):
        assert value in walk_forward
    for value in (
        'maker_fee_rate = "0.001"',
        'half_spread_rate = "0.00025"',
        'slippage_rate_per_side = "0.0005"',
    ):
        assert value in execution
    market_by_pair = {market["symbol"]: market for market in metadata["markets"]}
    for pair, constraint in config.pairs.items():
        market = market_by_pair[pair]
        assert constraint.price_tick == Decimal(str(market["precision"]["price"]))
        assert constraint.quantity_step == Decimal(str(market["precision"]["amount"]))
        assert constraint.minimum_quantity == Decimal(str(market["limits"]["amount"]["min"]))
        assert constraint.minimum_notional == Decimal(str(market["limits"]["cost"]["min"]))


def test_runtime_snapshot_and_backtest_contexts_use_identical_position_formula() -> None:
    config = load_freqtrade_risk_config(RISK_CONFIG_PATH)
    approval = calculate_runtime_entry_approval(
        snapshot_result(),
        config,
        pair="ETH/USDT",
        current_rate=Decimal("100"),
        signal_atr=Decimal("2.5"),
        min_stake=Decimal("5"),
        max_stake=Decimal("300"),
    )

    assert approval is not None
    backtest_context = replace(
        approval.position_context,
        source=RiskContextSource.BACKTEST,
        snapshot_id=None,
    )
    assert calculate_position_size(backtest_context) == approval.position_decision
    assert approval.approved_stake == approval.approved_quantity * approval.reference_rate


def test_missing_or_overcommitted_snapshot_rejects_runtime_stake() -> None:
    config = load_freqtrade_risk_config(RISK_CONFIG_PATH)
    common = {
        "config": config,
        "pair": "ETH/USDT",
        "current_rate": Decimal("100"),
        "signal_atr": Decimal("2.5"),
        "min_stake": Decimal("5"),
        "max_stake": Decimal("300"),
    }

    stale = calculate_runtime_entry_approval(snapshot_result(entry_allowed=False), **common)
    overcommitted = calculate_runtime_entry_approval(
        snapshot_result(pending_exposure="350"), **common
    )

    assert stale is None
    assert overcommitted is None


def test_initial_stop_is_fixed_to_actual_fill_and_never_widens() -> None:
    config = load_freqtrade_risk_config(RISK_CONFIG_PATH)
    stop = calculate_initial_stop_price(
        config,
        pair="BTC/USDT",
        average_entry_rate=Decimal("100.07"),
        signal_atr=Decimal("2.5"),
    )

    assert stop == Decimal("95.0")
    assert fixed_stoploss_ratio(initial_stop_price=stop, current_rate=Decimal("100")) == Decimal(
        "0.05"
    )
    assert fixed_stoploss_ratio(initial_stop_price=stop, current_rate=Decimal("110")) == (
        Decimal("15") / Decimal("110")
    )
    assert fixed_stoploss_ratio(initial_stop_price=stop, current_rate=Decimal("94")) == Decimal("0")
