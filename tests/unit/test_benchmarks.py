from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alphamind.research.benchmarks import (
    PriceBar,
    TransactionCostModel,
    build_buy_and_hold_benchmark,
    build_cash_benchmark,
    build_equal_weight_buy_and_hold_benchmark,
    build_simple_moving_average_benchmark,
)
from alphamind.research.performance import EquityObservation, calculate_performance


def bars(*prices: tuple[str, str], timeframe: str = "1d") -> tuple[PriceBar, ...]:
    delta = timedelta(hours=4) if timeframe == "4h" else timedelta(days=1)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(
        PriceBar(start + index * delta, Decimal(open_price), Decimal(close_price))
        for index, (open_price, close_price) in enumerate(prices)
    )


ZERO_COST = TransactionCostModel(Decimal("0"), Decimal("0"), Decimal("0"))


def test_performance_metrics_match_hand_calculated_return_drawdown_and_tail() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observations = (
        EquityObservation(start, Decimal("110"), Decimal("1"), Decimal("100")),
        EquityObservation(start + timedelta(days=1), Decimal("88"), Decimal("1"), Decimal("0")),
        EquityObservation(start + timedelta(days=2), Decimal("99"), Decimal("0"), Decimal("88")),
    )

    metrics = calculate_performance(
        Decimal("100"), observations, (Decimal("10"), Decimal("-5")), periods_per_year=365
    )

    assert metrics.net_return == pytest.approx(-0.01)
    assert metrics.annualized_return == pytest.approx(-0.7055926310900846)
    assert metrics.maximum_drawdown == pytest.approx(0.20)
    assert metrics.sharpe == pytest.approx(1.0781560101235834)
    assert metrics.sortino == pytest.approx(1.3787826756478576)
    assert metrics.calmar == pytest.approx(-3.527963155450423)
    assert metrics.profit_factor == pytest.approx(2.0)
    assert metrics.cvar_95 == pytest.approx(-0.20)
    assert metrics.time_under_water_fraction == pytest.approx(2 / 3)
    assert metrics.max_time_under_water_periods == 2
    assert metrics.exposure_fraction == pytest.approx(2 / 3)
    assert metrics.turnover == pytest.approx(188 / 99.25)


def test_cash_benchmark_has_only_zero_defined_metrics() -> None:
    curve = build_cash_benchmark(
        bars(("100", "101"), ("101", "99")), timeframe="1d", initial_equity=Decimal("100")
    )

    metrics = calculate_performance(
        curve.initial_equity, curve.observations, curve.trade_pnls, periods_per_year=365
    )

    assert metrics.net_return == 0
    assert metrics.maximum_drawdown == 0
    assert metrics.turnover == 0
    assert metrics.exposure_fraction == 0
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.calmar is None
    assert metrics.profit_factor is None


def test_buy_and_hold_costs_cannot_improve_net_return() -> None:
    market = bars(("100", "110"), ("110", "121"))
    no_cost = build_buy_and_hold_benchmark(
        market, timeframe="1d", initial_equity=Decimal("100"), costs=ZERO_COST
    )
    with_cost = build_buy_and_hold_benchmark(
        market,
        timeframe="1d",
        initial_equity=Decimal("100"),
        costs=TransactionCostModel(Decimal("0.001"), Decimal("0.00025"), Decimal("0.0005")),
    )

    assert no_cost.observations[-1].equity == Decimal("121")
    assert with_cost.observations[-1].equity < no_cost.observations[-1].equity
    assert with_cost.observations[0].traded_notional > 0
    assert with_cost.observations[-1].traded_notional > 0


def test_equal_weight_benchmark_does_not_rebalance() -> None:
    first = bars(("100", "100"), ("100", "200"))
    second = bars(("100", "100"), ("100", "50"))

    curve = build_equal_weight_buy_and_hold_benchmark(
        first,
        second,
        timeframe="1d",
        initial_equity=Decimal("100"),
        costs=ZERO_COST,
    )

    assert curve.observations[-1].equity == Decimal("125.0")
    assert curve.observations[0].traded_notional == Decimal("100")


def test_moving_average_signal_executes_only_at_next_candle_open() -> None:
    market = bars(("100", "100"), ("100", "110"), ("200", "200"))

    curve = build_simple_moving_average_benchmark(
        market,
        timeframe="1d",
        initial_equity=Decimal("100"),
        window=2,
        costs=ZERO_COST,
    )

    # 第二根 close 才产生 long 目标；第三根 open=200 买入并在末根 close 强制平仓。
    assert curve.observations[-1].equity == Decimal("100")
    assert [item.exposure_fraction for item in curve.observations] == [
        Decimal("0"),
        Decimal("0"),
        Decimal("1"),
    ]
    assert curve.observations[-1].traded_notional == Decimal("200")
    assert curve.trade_pnls == (Decimal("0"),)


def test_benchmarks_reject_gaps_and_misaligned_portfolio_inputs() -> None:
    valid = bars(("100", "100"), ("100", "101"))
    gap = (
        valid[0],
        PriceBar(valid[1].timestamp + timedelta(days=1), Decimal("100"), Decimal("101")),
    )
    with pytest.raises(ValueError, match="expected timeframe grid"):
        build_cash_benchmark(gap, timeframe="1d", initial_equity=Decimal("100"))

    shifted = tuple(
        PriceBar(item.timestamp + timedelta(hours=4), item.open, item.close) for item in valid
    )
    with pytest.raises(ValueError, match="aligned timestamps"):
        build_equal_weight_buy_and_hold_benchmark(
            valid,
            shifted,
            timeframe="1d",
            initial_equity=Decimal("100"),
            costs=ZERO_COST,
        )
