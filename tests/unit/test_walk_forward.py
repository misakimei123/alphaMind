from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alphamind.research.walk_forward import (
    BacktestResult,
    BacktestSettings,
    CostAssumptions,
    DonchianTrial,
    MarketBar,
    MarketConstraints,
    WalkForwardFold,
    bootstrap_mean_confidence_interval,
    deflated_sharpe_probability,
    profit_concentration,
    run_portfolio_backtest,
    validate_expanding_folds,
)

START = datetime(2023, 12, 31, 16, tzinfo=UTC)
INTERVAL = timedelta(hours=4)
TRIAL = DonchianTrial(1, "baseline", 2, 2, 2, Decimal("2.0"))
ZERO_COST = CostAssumptions(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
CONSTRAINT = MarketConstraints(
    price_tick=Decimal("0.01"),
    quantity_step=Decimal("0.001"),
    minimum_quantity=Decimal("0.001"),
    minimum_notional=Decimal("1"),
)
SETTINGS = BacktestSettings(
    initial_equity=Decimal("10000"),
    risk_fraction=Decimal("0.0025"),
    symbol_exposure_fraction=Decimal("0.40"),
    directional_exposure_fraction=Decimal("0.70"),
    volatility_cap_fraction=Decimal("0.40"),
    maximum_unit_loss_fraction=Decimal("0.50"),
    event_cluster_hours=72,
)


def _bar(
    index: int, close: str, *, open_price: str | None = None, low: str | None = None
) -> MarketBar:
    close_value = Decimal(close)
    open_value = Decimal(open_price or close)
    low_value = Decimal(low) if low is not None else min(open_value, close_value) - Decimal("1")
    high_value = max(open_value, close_value) + Decimal("1")
    return MarketBar(
        timestamp=START + INTERVAL * index,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=Decimal("10"),
    )


def _bars(*, stop_on_entry_bar: bool = False) -> tuple[MarketBar, ...]:
    values = [
        _bar(0, "100"),
        _bar(1, "100"),
        _bar(2, "101"),
        _bar(3, "110"),
        _bar(4, "111", open_price="111", low="90" if stop_on_entry_bar else "109"),
        _bar(5, "112"),
        _bar(6, "111"),
        _bar(7, "115"),
    ]
    return tuple(values)


def _run(*, stop_on_entry_bar: bool = False) -> BacktestResult:
    bars = _bars(stop_on_entry_bar=stop_on_entry_bar)
    return run_portfolio_backtest(
        {"BTC/USDT": bars},
        evaluation_start=bars[2].timestamp,
        evaluation_end_exclusive=bars[-1].timestamp + INTERVAL,
        expected_interval=INTERVAL,
        trial=TRIAL,
        costs=ZERO_COST,
        constraints={"BTC/USDT": CONSTRAINT},
        settings=SETTINGS,
    )


def test_expanding_folds_reject_random_or_overlapping_splits() -> None:
    first = WalkForwardFold(
        "WF-01",
        datetime(2022, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 7, 1, tzinfo=UTC),
    )
    second = WalkForwardFold(
        "WF-02",
        datetime(2022, 1, 1, tzinfo=UTC),
        datetime(2024, 7, 1, tzinfo=UTC),
        datetime(2024, 7, 1, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
    )
    validate_expanding_folds((first, second))

    random_split = WalkForwardFold(
        "WF-02",
        datetime(2022, 7, 1, tzinfo=UTC),
        datetime(2024, 7, 1, tzinfo=UTC),
        datetime(2024, 7, 1, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="share one train start"):
        validate_expanding_folds((first, random_split))


def test_signal_candle_never_fills_and_fold_starts_flat() -> None:
    result = _run()

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_signal_timestamp == START + INTERVAL * 3
    assert trade.entry_timestamp == START + INTERVAL * 4
    assert trade.entry_price == Decimal("111")
    assert trade.exit_reason == "fold_end"
    assert result.independent_event_count == 1


def test_intrabar_stop_uses_conservative_gap_or_stop_fill() -> None:
    result = _run(stop_on_entry_bar=True)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_timestamp == trade.exit_timestamp == START + INTERVAL * 4
    assert trade.exit_reason == "initial_stop"
    assert trade.exit_price < trade.entry_price
    assert trade.net_pnl_quote < 0


def test_market_data_gaps_and_pair_misalignment_fail_closed() -> None:
    bars = _bars()
    with pytest.raises(ValueError, match="timeframe grid"):
        run_portfolio_backtest(
            {"BTC/USDT": bars[:3] + bars[4:]},
            evaluation_start=bars[4].timestamp,
            evaluation_end_exclusive=bars[-1].timestamp + INTERVAL,
            expected_interval=INTERVAL,
            trial=TRIAL,
            costs=ZERO_COST,
            constraints={"BTC/USDT": CONSTRAINT},
            settings=SETTINGS,
        )

    shifted = tuple(
        MarketBar(
            item.timestamp + INTERVAL,
            item.open,
            item.high,
            item.low,
            item.close,
            item.volume,
        )
        for item in bars
    )
    with pytest.raises(ValueError, match="aligned timestamps"):
        run_portfolio_backtest(
            {"BTC/USDT": bars, "ETH/USDT": shifted},
            evaluation_start=bars[2].timestamp,
            evaluation_end_exclusive=bars[-1].timestamp + INTERVAL,
            expected_interval=INTERVAL,
            trial=TRIAL,
            costs=ZERO_COST,
            constraints={"BTC/USDT": CONSTRAINT, "ETH/USDT": CONSTRAINT},
            settings=SETTINGS,
        )


def test_bootstrap_is_deterministic_and_deflated_sharpe_is_probability() -> None:
    values = tuple(Decimal(item) for item in ("-1", "0.5", "1", "2", "3"))
    first = bootstrap_mean_confidence_interval(
        values,
        confidence_level=Decimal("0.95"),
        resamples=500,
        seed=20260716,
    )
    second = bootstrap_mean_confidence_interval(
        values,
        confidence_level=Decimal("0.95"),
        resamples=500,
        seed=20260716,
    )
    assert first == second
    assert first is not None and first[0] <= sum(values) / len(values) <= first[1]

    correction = deflated_sharpe_probability(
        (0.01, -0.005, 0.02, -0.002, 0.015, 0.003),
        (-0.02, -0.01, 0.0, 0.01, 0.02),
    )
    assert correction is not None
    probability, expected_maximum = correction
    assert 0 <= probability <= 1
    assert expected_maximum > 0


def test_profit_concentration_reports_top_five_and_pair_split() -> None:
    result = _run()
    concentration = profit_concentration(result.trades)

    top_five = concentration["top_5_trades"]
    assert isinstance(top_five, tuple) and len(top_five) == 1
    assert concentration["pair_net_pnl_quote"] == {"BTC/USDT": result.trades[0].net_pnl_quote}
