"""现货 long/flat 的风险预算仓位纯函数。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

ZERO = Decimal("0")


class RejectionReason(StrEnum):
    """运行时可预期的拒绝原因。输入合同错误会直接抛出异常。"""

    NO_CAPACITY = "no_capacity"
    BELOW_MINIMUM_QUANTITY = "below_minimum_quantity"
    BELOW_MINIMUM_NOTIONAL = "below_minimum_notional"


class LimitingCap(StrEnum):
    """最终限制仓位的首个约束。"""

    RISK_BUDGET = "risk_budget"
    VOLATILITY = "volatility"
    SYMBOL_EXPOSURE = "symbol_exposure"
    DIRECTIONAL_EXPOSURE = "directional_exposure"
    AVAILABLE_BALANCE = "available_balance"


@dataclass(frozen=True, slots=True)
class PositionSizeRequest:
    """一次仓位审批所需的完整、可重放输入。

    ``*_quantity`` 使用当前交易对的 base asset 单位；``*_quote`` 必须使用与
    ``entry_price`` 相同的 quote currency，因而可安全汇总不同 base asset 的方向暴露。
    """

    nav: Decimal
    risk_fraction: Decimal
    entry_price: Decimal
    stop_price: Decimal
    minimum_stop_distance: Decimal
    fee_buffer_per_unit: Decimal
    slippage_buffer_per_unit: Decimal
    gap_buffer_per_unit: Decimal
    volatility_cap_quantity: Decimal
    symbol_exposure_limit_quote: Decimal
    current_symbol_exposure_quote: Decimal
    pending_symbol_entry_exposure_quote: Decimal
    directional_exposure_limit_quote: Decimal
    current_directional_exposure_quote: Decimal
    pending_directional_entry_exposure_quote: Decimal
    available_balance_cap_quantity: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal


@dataclass(frozen=True, slots=True)
class PositionSizeDecision:
    approved_quantity: Decimal
    risk_cash: Decimal
    estimated_unit_loss: Decimal
    limiting_cap: LimitingCap
    rejection_reason: RejectionReason | None

    @property
    def approved(self) -> bool:
        return self.rejection_reason is None and self.approved_quantity > ZERO


def _validate_request(request: PositionSizeRequest) -> None:
    for field_name in request.__dataclass_fields__:
        value = getattr(request, field_name)
        if not isinstance(value, Decimal):
            raise TypeError(f"{field_name} must be Decimal")
        if not value.is_finite():
            raise ValueError(f"{field_name} must be finite")

    if request.nav <= ZERO:
        raise ValueError("nav must be positive")
    if not ZERO < request.risk_fraction <= Decimal("1"):
        raise ValueError("risk_fraction must be in (0, 1]")
    if request.entry_price <= ZERO or request.stop_price <= ZERO:
        raise ValueError("entry_price and stop_price must be positive")
    if request.stop_price >= request.entry_price:
        raise ValueError("long position stop_price must be below entry_price")
    if request.minimum_stop_distance <= ZERO:
        raise ValueError("minimum_stop_distance must be positive")
    if request.quantity_step <= ZERO:
        raise ValueError("quantity_step must be positive")

    non_negative_fields = (
        "fee_buffer_per_unit",
        "slippage_buffer_per_unit",
        "gap_buffer_per_unit",
        "volatility_cap_quantity",
        "symbol_exposure_limit_quote",
        "current_symbol_exposure_quote",
        "pending_symbol_entry_exposure_quote",
        "directional_exposure_limit_quote",
        "current_directional_exposure_quote",
        "pending_directional_entry_exposure_quote",
        "available_balance_cap_quantity",
        "minimum_quantity",
        "minimum_notional",
    )
    if any(getattr(request, name) < ZERO for name in non_negative_fields):
        raise ValueError("quantities, limits and buffers must not be negative")


def _floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    # 交易数量只能向下对齐交易所 step，禁止舍入造成实际风险扩大。
    steps = (quantity / step).to_integral_value(rounding=ROUND_FLOOR)
    return steps * step


def calculate_position_size(request: PositionSizeRequest) -> PositionSizeDecision:
    """按风险预算与暴露上限计算可批准的 base asset 数量。"""

    _validate_request(request)

    stop_distance = request.entry_price - request.stop_price
    if stop_distance < request.minimum_stop_distance:
        raise ValueError("stop distance is below the configured minimum")

    risk_cash = request.nav * request.risk_fraction
    estimated_unit_loss = (
        stop_distance
        + request.fee_buffer_per_unit
        + request.slippage_buffer_per_unit
        + request.gap_buffer_per_unit
    )
    quantity_by_risk = risk_cash / estimated_unit_loss

    # 跨标的暴露统一以 quote currency 计量，禁止直接相加 BTC、ETH 等不同 base 数量。
    symbol_capacity_quote = max(
        request.symbol_exposure_limit_quote
        - request.current_symbol_exposure_quote
        - request.pending_symbol_entry_exposure_quote,
        ZERO,
    )
    directional_capacity_quote = max(
        request.directional_exposure_limit_quote
        - request.current_directional_exposure_quote
        - request.pending_directional_entry_exposure_quote,
        ZERO,
    )
    symbol_capacity = symbol_capacity_quote / request.entry_price
    directional_capacity = directional_capacity_quote / request.entry_price

    caps = {
        LimitingCap.RISK_BUDGET: quantity_by_risk,
        LimitingCap.VOLATILITY: request.volatility_cap_quantity,
        LimitingCap.SYMBOL_EXPOSURE: symbol_capacity,
        LimitingCap.DIRECTIONAL_EXPOSURE: directional_capacity,
        LimitingCap.AVAILABLE_BALANCE: request.available_balance_cap_quantity,
    }
    raw_quantity = min(caps.values())
    limiting_cap = next(name for name, quantity in caps.items() if quantity == raw_quantity)
    approved_quantity = _floor_to_step(raw_quantity, request.quantity_step)

    if approved_quantity <= ZERO:
        rejection_reason = RejectionReason.NO_CAPACITY
    elif approved_quantity < request.minimum_quantity:
        rejection_reason = RejectionReason.BELOW_MINIMUM_QUANTITY
    elif approved_quantity * request.entry_price < request.minimum_notional:
        rejection_reason = RejectionReason.BELOW_MINIMUM_NOTIONAL
    else:
        rejection_reason = None

    if rejection_reason is not None:
        approved_quantity = ZERO

    return PositionSizeDecision(
        approved_quantity=approved_quantity,
        risk_cash=risk_cash,
        estimated_unit_loss=estimated_unit_loss,
        limiting_cap=limiting_cap,
        rejection_reason=rejection_reason,
    )
