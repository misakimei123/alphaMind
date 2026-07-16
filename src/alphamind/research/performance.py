"""P1-05 统一绩效指标纯函数。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise


@dataclass(frozen=True, slots=True)
class EquityObservation:
    """一个已完成统计周期的期末权益与期间活动。"""

    timestamp: datetime
    equity: Decimal
    exposure_fraction: Decimal
    traded_notional: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        for name in ("equity", "exposure_fraction", "traded_notional"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if self.equity <= 0:
            raise ValueError("equity must be positive")
        if not Decimal("0") <= self.exposure_fraction <= Decimal("1"):
            raise ValueError("exposure_fraction must be in [0, 1]")
        if self.traded_notional < 0:
            raise ValueError("traded_notional must not be negative")


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """统一指标结果；无定义的比率使用 ``None``，禁止写入 Infinity。"""

    net_return: float
    annualized_return: float
    maximum_drawdown: float
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    profit_factor: float | None
    cvar_95: float
    turnover: float
    time_under_water_fraction: float
    max_time_under_water_periods: int
    exposure_fraction: float


def _validate_trade_pnls(trade_pnls: tuple[Decimal, ...]) -> None:
    for pnl in trade_pnls:
        if not isinstance(pnl, Decimal):
            raise TypeError("trade_pnls must contain Decimal values")
        if not pnl.is_finite():
            raise ValueError("trade_pnls must be finite")


def calculate_performance(
    initial_equity: Decimal,
    observations: tuple[EquityObservation, ...],
    trade_pnls: tuple[Decimal, ...],
    *,
    periods_per_year: int,
) -> PerformanceMetrics:
    """从同一权益路径计算 P1-05 的统一指标。

    周期收益、回撤和 Time Under Water 都从初始权益开始计算；Sharpe 使用完整
    周期收益总体标准差，Sortino 使用相对零收益的 downside deviation。Profit
    Factor 只使用已实现的净交易 PnL，不能用 candle 收益替代交易结果。
    """

    if not isinstance(initial_equity, Decimal):
        raise TypeError("initial_equity must be Decimal")
    if not initial_equity.is_finite() or initial_equity <= 0:
        raise ValueError("initial_equity must be finite and positive")
    if not observations:
        raise ValueError("observations must not be empty")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    _validate_trade_pnls(trade_pnls)

    for previous, current in pairwise(observations):
        if current.timestamp <= previous.timestamp:
            raise ValueError("observation timestamps must be strictly increasing")

    equities = [float(initial_equity), *(float(item.equity) for item in observations)]
    returns = [current / previous - 1.0 for previous, current in pairwise(equities)]
    net_return = equities[-1] / equities[0] - 1.0
    annualized_return = (equities[-1] / equities[0]) ** (periods_per_year / len(returns)) - 1.0

    peak = equities[0]
    maximum_drawdown = 0.0
    underwater_periods = 0
    current_underwater = 0
    maximum_underwater = 0
    for equity in equities[1:]:
        peak = max(peak, equity)
        drawdown = 1.0 - equity / peak
        maximum_drawdown = max(maximum_drawdown, drawdown)
        if drawdown > 0:
            underwater_periods += 1
            current_underwater += 1
            maximum_underwater = max(maximum_underwater, current_underwater)
        else:
            current_underwater = 0

    mean_return = statistics.fmean(returns)
    volatility = statistics.pstdev(returns)
    sharpe = mean_return / volatility * math.sqrt(periods_per_year) if volatility > 0 else None
    downside_deviation = math.sqrt(statistics.fmean(min(value, 0.0) ** 2 for value in returns))
    sortino = (
        mean_return / downside_deviation * math.sqrt(periods_per_year)
        if downside_deviation > 0
        else None
    )
    calmar = annualized_return / maximum_drawdown if maximum_drawdown > 0 else None

    gross_profit = sum((pnl for pnl in trade_pnls if pnl > 0), Decimal("0"))
    gross_loss = -sum((pnl for pnl in trade_pnls if pnl < 0), Decimal("0"))
    profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else None

    # CVaR 95% 使用最差 5% 周期收益；小样本至少保留最差一个观测，避免空尾部。
    tail_count = max(1, math.ceil(len(returns) * 0.05))
    cvar_95 = statistics.fmean(sorted(returns)[:tail_count])
    average_equity = statistics.fmean(equities)
    turnover = sum(float(item.traded_notional) for item in observations) / average_equity

    return PerformanceMetrics(
        net_return=net_return,
        annualized_return=annualized_return,
        maximum_drawdown=maximum_drawdown,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        profit_factor=profit_factor,
        cvar_95=cvar_95,
        turnover=turnover,
        time_under_water_fraction=underwater_periods / len(observations),
        max_time_under_water_periods=maximum_underwater,
        exposure_fraction=statistics.fmean(float(item.exposure_fraction) for item in observations),
    )
