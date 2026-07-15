"""项目级绝对损失门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from alphamind.config.risk_limits import RiskLimitsConfig

ZERO = Decimal("0")


class AbsoluteLossReason(StrEnum):
    BELOW_LIMIT = "below_absolute_loss_limit"
    LIMIT_REACHED = "absolute_loss_limit_reached"
    CURRENCY_MISMATCH = "accounting_currency_mismatch"


class AbsoluteLossBoundary(StrEnum):
    """当前实际生效的更严格停止边界。"""

    FRACTION = "fraction"
    FIXED = "fixed"
    BOTH = "both"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AccountPnlObservation:
    """从已批准项目基线起、剔除外部现金流后的累计 PnL。"""

    accounting_currency: str
    cashflow_adjusted_capital_baseline: Decimal
    cashflow_adjusted_cumulative_pnl: Decimal


@dataclass(frozen=True, slots=True)
class AbsoluteLossDecision:
    entry_allowed: bool
    kill_switch: bool
    absolute_loss: Decimal
    effective_loss_limit: Decimal
    remaining_loss_capacity: Decimal
    limiting_boundary: AbsoluteLossBoundary
    reason: AbsoluteLossReason


def evaluate_absolute_loss(
    observation: AccountPnlObservation,
    config: RiskLimitsConfig,
) -> AbsoluteLossDecision:
    """达到配置的绝对损失边界时停止新入场并触发 Kill Switch。"""

    baseline = observation.cashflow_adjusted_capital_baseline
    pnl = observation.cashflow_adjusted_cumulative_pnl
    for name, value in (
        ("cashflow_adjusted_capital_baseline", baseline),
        ("cashflow_adjusted_cumulative_pnl", pnl),
    ):
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be Decimal")
        if not value.is_finite():
            raise ValueError(f"{name} must be finite")
    if baseline <= ZERO:
        raise ValueError("cashflow_adjusted_capital_baseline must be positive")

    absolute_loss = max(-pnl, ZERO)
    if observation.accounting_currency != config.accounting_currency:
        # 币种无法安全比较时 fail-closed，禁止把不同资产的名义数字直接相减。
        return AbsoluteLossDecision(
            entry_allowed=False,
            kill_switch=True,
            absolute_loss=absolute_loss,
            effective_loss_limit=ZERO,
            remaining_loss_capacity=ZERO,
            limiting_boundary=AbsoluteLossBoundary.UNKNOWN,
            reason=AbsoluteLossReason.CURRENCY_MISMATCH,
        )

    fraction_loss_limit = baseline * config.maximum_absolute_loss_fraction
    fixed_loss_limit = config.maximum_absolute_loss
    effective_loss_limit = min(fraction_loss_limit, fixed_loss_limit)
    if fraction_loss_limit < fixed_loss_limit:
        limiting_boundary = AbsoluteLossBoundary.FRACTION
    elif fixed_loss_limit < fraction_loss_limit:
        limiting_boundary = AbsoluteLossBoundary.FIXED
    else:
        limiting_boundary = AbsoluteLossBoundary.BOTH

    remaining_loss_capacity = max(effective_loss_limit - absolute_loss, ZERO)
    limit_reached = absolute_loss >= effective_loss_limit
    return AbsoluteLossDecision(
        entry_allowed=not limit_reached,
        kill_switch=limit_reached,
        absolute_loss=absolute_loss,
        effective_loss_limit=effective_loss_limit,
        remaining_loss_capacity=remaining_loss_capacity,
        limiting_boundary=limiting_boundary,
        reason=(
            AbsoluteLossReason.LIMIT_REACHED if limit_reached else AbsoluteLossReason.BELOW_LIMIT
        ),
    )
