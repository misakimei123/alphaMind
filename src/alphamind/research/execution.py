"""P2-04 确定性成本、成交与压力场景模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

ZERO = Decimal("0")
ONE = Decimal("1")


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class FillStatus(StrEnum):
    FILLED = "filled"
    UNFILLED = "unfilled"


class FillReason(StrEnum):
    FILLED = "filled"
    MISSING_CANDLE = "missing_candle"
    DELAY_MISMATCH = "delay_mismatch"
    LIMIT_FILL_UNCONFIRMED = "limit_fill_unconfirmed"
    LIMIT_NOT_REACHED = "limit_not_reached"
    BELOW_MINIMUM_QUANTITY = "below_minimum_quantity"
    BELOW_MINIMUM_NOTIONAL = "below_minimum_notional"


@dataclass(frozen=True, slots=True)
class ExecutionBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        for name in ("open", "high", "low"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be finite and positive")
        if self.high < max(self.open, self.low) or self.low > min(self.open, self.high):
            raise ValueError("bar high/low must contain open")


@dataclass(frozen=True, slots=True)
class ExecutionOrder:
    signal_timestamp: datetime
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    limit_fill_confirmed: bool | None
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal

    def __post_init__(self) -> None:
        if self.signal_timestamp.tzinfo is None or self.signal_timestamp.utcoffset() != timedelta(
            0
        ):
            raise ValueError("signal_timestamp must be timezone-aware UTC")
        if not isinstance(self.side, OrderSide) or not isinstance(self.order_type, OrderType):
            raise TypeError("side and order_type must use their enum contracts")
        for name in (
            "quantity",
            "price_tick",
            "quantity_step",
            "minimum_quantity",
            "minimum_notional",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if self.quantity <= ZERO or self.price_tick <= ZERO or self.quantity_step <= ZERO:
            raise ValueError("quantity, price_tick and quantity_step must be positive")
        if self.minimum_quantity < ZERO or self.minimum_notional < ZERO:
            raise ValueError("minimum_quantity and minimum_notional must not be negative")
        if self.quantity % self.quantity_step != ZERO:
            raise ValueError("quantity must align to quantity_step")

        if self.order_type is OrderType.MARKET:
            if self.limit_price is not None or self.limit_fill_confirmed is not None:
                raise ValueError("market order must not contain limit fields")
        else:
            if not isinstance(self.limit_price, Decimal):
                raise TypeError("limit_price must be Decimal for limit order")
            if not self.limit_price.is_finite() or self.limit_price <= ZERO:
                raise ValueError("limit_price must be finite and positive")
            if self.limit_price % self.price_tick != ZERO:
                raise ValueError("limit_price must align to price_tick")
            if (
                self.limit_fill_confirmed is not None
                and type(self.limit_fill_confirmed) is not bool
            ):
                raise TypeError("limit_fill_confirmed must be bool or None")


@dataclass(frozen=True, slots=True)
class ExecutionCostModel:
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    half_spread_rate: Decimal
    slippage_rate_per_side: Decimal

    def __post_init__(self) -> None:
        for name in (
            "maker_fee_rate",
            "taker_fee_rate",
            "half_spread_rate",
            "slippage_rate_per_side",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} must be finite and non-negative")
        if max(self.maker_fee_rate, self.taker_fee_rate) >= ONE:
            raise ValueError("fee rates must be below 100%")
        if self.half_spread_rate + self.slippage_rate_per_side >= ONE:
            raise ValueError("spread plus slippage must be below 100%")


@dataclass(frozen=True, slots=True)
class StressScenario:
    scenario_id: str
    fee_multiplier: Decimal = ONE
    slippage_multiplier: Decimal = ONE
    parameter_multiplier: Decimal | None = None
    daily_price_shock: Decimal | None = None
    missing_candle: bool = False
    execution_delay_periods: int = 0
    disclosure: str = ""

    def __post_init__(self) -> None:
        if not self.scenario_id or not self.disclosure:
            raise ValueError("scenario_id and disclosure must not be empty")
        for name in ("fee_multiplier", "slippage_multiplier"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.parameter_multiplier is not None:
            if not isinstance(self.parameter_multiplier, Decimal):
                raise TypeError("parameter_multiplier must be Decimal")
            if not self.parameter_multiplier.is_finite() or self.parameter_multiplier <= ZERO:
                raise ValueError("parameter_multiplier must be finite and positive")
        if self.daily_price_shock is not None:
            if not isinstance(self.daily_price_shock, Decimal):
                raise TypeError("daily_price_shock must be Decimal")
            if not -ONE < self.daily_price_shock <= ZERO:
                raise ValueError("daily_price_shock must be in (-1, 0]")
        if type(self.execution_delay_periods) is not int or self.execution_delay_periods < 0:
            raise ValueError("execution_delay_periods must be a non-negative integer")
        if type(self.missing_candle) is not bool:
            raise TypeError("missing_candle must be bool")
        if self.missing_candle and self.execution_delay_periods:
            raise ValueError("missing candle and delayed execution are separate scenarios")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    scenario_id: str
    status: FillStatus
    reason: FillReason
    execution_timestamp: datetime | None
    delay_periods: int
    reference_price: Decimal | None
    gross_quote: Decimal
    fee_quote: Decimal
    spread_quote: Decimal
    slippage_quote: Decimal
    net_cash_flow_quote: Decimal


def build_p2_04_scenarios() -> tuple[StressScenario, ...]:
    """返回冻结且逐项披露的 P2-04 scenario matrix。"""

    return (
        StressScenario("baseline", disclosure="基础成本与 next-candle open 成交"),
        StressScenario(
            "fee_2x", fee_multiplier=Decimal("2"), disclosure="maker/taker fee 同时放大 2 倍"
        ),
        StressScenario(
            "slippage_3x",
            slippage_multiplier=Decimal("3"),
            disclosure="每侧 slippage 放大 3 倍，spread 不变",
        ),
        StressScenario(
            "parameter_minus_20pct",
            parameter_multiplier=Decimal("0.8"),
            disclosure="单次参数向下扰动 20%，禁止 Cartesian product",
        ),
        StressScenario(
            "parameter_minus_10pct",
            parameter_multiplier=Decimal("0.9"),
            disclosure="单次参数向下扰动 10%，禁止 Cartesian product",
        ),
        StressScenario(
            "parameter_plus_10pct",
            parameter_multiplier=Decimal("1.1"),
            disclosure="单次参数向上扰动 10%，禁止 Cartesian product",
        ),
        StressScenario(
            "parameter_plus_20pct",
            parameter_multiplier=Decimal("1.2"),
            disclosure="单次参数向上扰动 20%，禁止 Cartesian product",
        ),
        StressScenario(
            "daily_drop_10pct",
            daily_price_shock=Decimal("-0.10"),
            disclosure="单日价格冲击 -10%，不改变成交成本假设",
        ),
        StressScenario(
            "daily_drop_20pct",
            daily_price_shock=Decimal("-0.20"),
            disclosure="单日价格冲击 -20%，不改变成交成本假设",
        ),
        StressScenario(
            "missing_candle",
            missing_candle=True,
            disclosure="预期成交 candle 缺失，订单保持未成交",
        ),
        StressScenario(
            "delay_one_period",
            execution_delay_periods=1,
            disclosure="成交推迟一根完整 candle，并使用延迟 candle open",
        ),
    )


def _empty_result(
    scenario: StressScenario,
    reason: FillReason,
    *,
    actual_delay_periods: int | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        scenario_id=scenario.scenario_id,
        status=FillStatus.UNFILLED,
        reason=reason,
        execution_timestamp=None,
        delay_periods=(
            scenario.execution_delay_periods
            if actual_delay_periods is None
            else actual_delay_periods
        ),
        reference_price=None,
        gross_quote=ZERO,
        fee_quote=ZERO,
        spread_quote=ZERO,
        slippage_quote=ZERO,
        net_cash_flow_quote=ZERO,
    )


def _align_price(price: Decimal, tick: Decimal, side: OrderSide) -> Decimal:
    rounding = ROUND_CEILING if side is OrderSide.BUY else ROUND_FLOOR
    ticks = (price / tick).to_integral_value(rounding=rounding)
    return ticks * tick


def simulate_execution(
    order: ExecutionOrder,
    execution_bar: ExecutionBar | None,
    *,
    expected_interval: timedelta,
    costs: ExecutionCostModel,
    scenario: StressScenario,
) -> ExecutionResult:
    """在显式成交假设下计算单笔 fill 与完整 quote 成本。"""

    if expected_interval <= timedelta(0):
        raise ValueError("expected_interval must be positive")
    if scenario.parameter_multiplier is not None:
        raise ValueError("parameter stress must be applied before execution simulation")
    fee_rate = costs.maker_fee_rate if order.order_type is OrderType.LIMIT else costs.taker_fee_rate
    if fee_rate * scenario.fee_multiplier >= ONE:
        raise ValueError("stressed fee rate must be below 100%")
    if costs.half_spread_rate + costs.slippage_rate_per_side * scenario.slippage_multiplier >= ONE:
        raise ValueError("stressed spread plus slippage must be below 100%")
    if scenario.missing_candle or execution_bar is None:
        return _empty_result(scenario, FillReason.MISSING_CANDLE)

    elapsed = execution_bar.timestamp - order.signal_timestamp
    periods, remainder = divmod(elapsed, expected_interval)
    if remainder or periods < 1:
        raise ValueError("execution bar must follow the signal on the expected timeframe grid")
    actual_delay = periods - 1
    if actual_delay != scenario.execution_delay_periods:
        return _empty_result(
            scenario,
            FillReason.DELAY_MISMATCH,
            actual_delay_periods=actual_delay,
        )

    shock_multiplier = ONE + (scenario.daily_price_shock or ZERO)
    stressed_open = execution_bar.open * shock_multiplier
    stressed_high = execution_bar.high * shock_multiplier
    stressed_low = execution_bar.low * shock_multiplier

    if order.order_type is OrderType.LIMIT:
        assert order.limit_price is not None
        if order.limit_fill_confirmed is not True:
            return _empty_result(scenario, FillReason.LIMIT_FILL_UNCONFIRMED)
        if not stressed_low <= order.limit_price <= stressed_high:
            return _empty_result(scenario, FillReason.LIMIT_NOT_REACHED)
        reference_price = order.limit_price
    else:
        reference_price = _align_price(stressed_open, order.price_tick, order.side)

    if order.quantity < order.minimum_quantity:
        return _empty_result(scenario, FillReason.BELOW_MINIMUM_QUANTITY)
    gross_quote = order.quantity * reference_price
    if gross_quote < order.minimum_notional:
        return _empty_result(scenario, FillReason.BELOW_MINIMUM_NOTIONAL)

    fee_quote = gross_quote * fee_rate * scenario.fee_multiplier
    spread_quote = gross_quote * costs.half_spread_rate
    slippage_quote = gross_quote * costs.slippage_rate_per_side * scenario.slippage_multiplier
    total_cost = fee_quote + spread_quote + slippage_quote
    net_cash_flow = -(gross_quote + total_cost)
    if order.side is OrderSide.SELL:
        net_cash_flow = gross_quote - total_cost

    return ExecutionResult(
        scenario_id=scenario.scenario_id,
        status=FillStatus.FILLED,
        reason=FillReason.FILLED,
        execution_timestamp=execution_bar.timestamp,
        delay_periods=actual_delay,
        reference_price=reference_price,
        gross_quote=gross_quote,
        fee_quote=fee_quote,
        spread_quote=spread_quote,
        slippage_quote=slippage_quote,
        net_cash_flow_quote=net_cash_flow,
    )
