from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alphamind.research.execution import (
    ExecutionBar,
    ExecutionCostModel,
    ExecutionOrder,
    FillReason,
    FillStatus,
    OrderSide,
    OrderType,
    StressScenario,
    build_p2_04_scenarios,
    simulate_execution,
)

SIGNAL_TIME = datetime(2026, 1, 1, tzinfo=UTC)
INTERVAL = timedelta(hours=4)
ZERO_COST = ExecutionCostModel(
    maker_fee_rate=Decimal("0"),
    taker_fee_rate=Decimal("0"),
    half_spread_rate=Decimal("0"),
    slippage_rate_per_side=Decimal("0"),
)
BASE_COST = ExecutionCostModel(
    maker_fee_rate=Decimal("0.001"),
    taker_fee_rate=Decimal("0.002"),
    half_spread_rate=Decimal("0.00025"),
    slippage_rate_per_side=Decimal("0.0005"),
)
BASELINE = build_p2_04_scenarios()[0]


def bar(*, periods_after_signal: int = 1, open_price: str = "100") -> ExecutionBar:
    open_value = Decimal(open_price)
    return ExecutionBar(
        timestamp=SIGNAL_TIME + INTERVAL * periods_after_signal,
        open=open_value,
        high=open_value + Decimal("5"),
        low=open_value - Decimal("5"),
    )


def order(**overrides: object) -> ExecutionOrder:
    baseline: dict[str, object] = {
        "signal_timestamp": SIGNAL_TIME,
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("2"),
        "limit_price": None,
        "limit_fill_confirmed": None,
        "price_tick": Decimal("0.01"),
        "quantity_step": Decimal("0.001"),
        "minimum_quantity": Decimal("0.001"),
        "minimum_notional": Decimal("5"),
    }
    baseline.update(overrides)
    return ExecutionOrder(**baseline)  # type: ignore[arg-type]


def execute(
    execution_order: ExecutionOrder,
    execution_bar: ExecutionBar | None,
    *,
    costs: ExecutionCostModel = BASE_COST,
    scenario: StressScenario = BASELINE,
):
    return simulate_execution(
        execution_order,
        execution_bar,
        expected_interval=INTERVAL,
        costs=costs,
        scenario=scenario,
    )


def test_zero_cost_next_candle_open_matches_hand_calculation() -> None:
    result = execute(order(), bar(), costs=ZERO_COST)

    assert result.status is FillStatus.FILLED
    assert result.execution_timestamp == SIGNAL_TIME + INTERVAL
    assert result.reference_price == Decimal("100")
    assert result.gross_quote == Decimal("200")
    assert result.fee_quote == result.spread_quote == result.slippage_quote == Decimal("0")
    assert result.net_cash_flow_quote == Decimal("-200")


def test_cost_increases_cannot_improve_buy_or_sell_cash_flow() -> None:
    buy_zero = execute(order(), bar(), costs=ZERO_COST)
    buy_cost = execute(order(), bar())
    sell_zero = execute(order(side=OrderSide.SELL), bar(), costs=ZERO_COST)
    sell_cost = execute(order(side=OrderSide.SELL), bar())

    assert buy_cost.net_cash_flow_quote < buy_zero.net_cash_flow_quote
    assert sell_cost.net_cash_flow_quote < sell_zero.net_cash_flow_quote

    fee_stress = next(item for item in build_p2_04_scenarios() if item.scenario_id == "fee_2x")
    slippage_stress = next(
        item for item in build_p2_04_scenarios() if item.scenario_id == "slippage_3x"
    )
    assert execute(order(), bar(), scenario=fee_stress).net_cash_flow_quote < (
        buy_cost.net_cash_flow_quote
    )
    assert execute(order(), bar(), scenario=slippage_stress).net_cash_flow_quote < (
        buy_cost.net_cash_flow_quote
    )


def test_limit_touch_requires_explicit_fill_confirmation() -> None:
    unconfirmed = order(
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
        limit_fill_confirmed=None,
    )
    confirmed = replace(unconfirmed, limit_fill_confirmed=True)

    assert execute(unconfirmed, bar()).reason is FillReason.LIMIT_FILL_UNCONFIRMED
    assert execute(confirmed, bar()).status is FillStatus.FILLED
    not_reached = execute(replace(confirmed, limit_price=Decimal("90")), bar())
    assert not_reached.status is FillStatus.UNFILLED
    assert not_reached.reason is FillReason.LIMIT_NOT_REACHED

    with pytest.raises(TypeError, match="limit_fill_confirmed"):
        replace(unconfirmed, limit_fill_confirmed=1)  # type: ignore[arg-type]


def test_missing_and_delayed_candles_are_separate_explicit_scenarios() -> None:
    missing = next(item for item in build_p2_04_scenarios() if item.scenario_id == "missing_candle")
    delayed = next(
        item for item in build_p2_04_scenarios() if item.scenario_id == "delay_one_period"
    )

    missing_result = execute(order(), None, scenario=missing)
    assert missing_result.status is FillStatus.UNFILLED
    assert missing_result.reason is FillReason.MISSING_CANDLE

    mismatch = execute(order(), bar(periods_after_signal=2))
    assert mismatch.reason is FillReason.DELAY_MISMATCH
    assert mismatch.delay_periods == 1
    delayed_result = execute(order(), bar(periods_after_signal=2), scenario=delayed)
    assert delayed_result.status is FillStatus.FILLED
    assert delayed_result.delay_periods == 1


def test_signal_candle_cannot_be_used_as_execution_candle() -> None:
    with pytest.raises(ValueError, match="must follow the signal"):
        execute(order(), bar(periods_after_signal=0))


def test_minimums_and_precision_fail_closed() -> None:
    below_quantity = execute(
        order(quantity=Decimal("0.001"), minimum_quantity=Decimal("0.002")), bar()
    )
    below_notional = execute(order(quantity=Decimal("0.01"), minimum_notional=Decimal("2")), bar())

    assert below_quantity.reason is FillReason.BELOW_MINIMUM_QUANTITY
    assert below_notional.reason is FillReason.BELOW_MINIMUM_NOTIONAL
    with pytest.raises(ValueError, match="quantity_step"):
        order(quantity=Decimal("1.0005"))
    with pytest.raises(ValueError, match="price_tick"):
        order(
            order_type=OrderType.LIMIT,
            limit_price=Decimal("100.001"),
            limit_fill_confirmed=True,
        )


def test_maker_and_taker_fee_rates_are_kept_separate() -> None:
    market = execute(order(), bar())
    limit = execute(
        order(
            order_type=OrderType.LIMIT,
            limit_price=Decimal("100"),
            limit_fill_confirmed=True,
        ),
        bar(),
    )

    assert market.fee_quote == Decimal("0.400")
    assert limit.fee_quote == Decimal("0.200")


def test_daily_price_shocks_are_applied_without_changing_cost_multipliers() -> None:
    drop_10 = next(
        item for item in build_p2_04_scenarios() if item.scenario_id == "daily_drop_10pct"
    )
    drop_20 = next(
        item for item in build_p2_04_scenarios() if item.scenario_id == "daily_drop_20pct"
    )

    assert execute(order(), bar(), scenario=drop_10, costs=ZERO_COST).reference_price == Decimal(
        "90.00"
    )
    assert execute(order(), bar(), scenario=drop_20, costs=ZERO_COST).reference_price == Decimal(
        "80.00"
    )
    assert drop_10.fee_multiplier == drop_20.fee_multiplier == Decimal("1")


def test_scenario_matrix_discloses_every_preregistered_assumption_separately() -> None:
    scenarios = build_p2_04_scenarios()
    by_id = {item.scenario_id: item for item in scenarios}

    assert len(by_id) == len(scenarios) == 11
    assert all(item.disclosure for item in scenarios)
    assert by_id["fee_2x"].fee_multiplier == Decimal("2")
    assert by_id["slippage_3x"].slippage_multiplier == Decimal("3")
    assert {
        item.parameter_multiplier for item in scenarios if item.parameter_multiplier is not None
    } == {Decimal("0.8"), Decimal("0.9"), Decimal("1.1"), Decimal("1.2")}
    assert {item.daily_price_shock for item in scenarios if item.daily_price_shock is not None} == {
        Decimal("-0.10"),
        Decimal("-0.20"),
    }


def test_parameter_and_extreme_cost_scenarios_cannot_be_silently_misapplied() -> None:
    parameter = next(
        item for item in build_p2_04_scenarios() if item.scenario_id == "parameter_minus_20pct"
    )
    with pytest.raises(ValueError, match="must be applied before execution"):
        execute(order(), bar(), scenario=parameter)

    extreme_fee = StressScenario(
        "extreme_fee",
        fee_multiplier=Decimal("1000"),
        disclosure="invalid external stress fixture",
    )
    with pytest.raises(ValueError, match="fee rate"):
        execute(order(), bar(), scenario=extreme_fee)
