"""P1-05 现金、Buy-and-Hold、50/50 与简单均线工程基准。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from alphamind.research.performance import EquityObservation

TIMEFRAME_DELTAS = {
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


@dataclass(frozen=True, slots=True)
class PriceBar:
    """用于工程基准的最小 OHLC 输入。"""

    timestamp: datetime
    open: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        for name in ("open", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class TransactionCostModel:
    """每侧 taker fee、半点差和滑点假设。"""

    fee_rate_per_side: Decimal
    half_spread_rate: Decimal
    slippage_rate_per_side: Decimal

    def __post_init__(self) -> None:
        for name in ("fee_rate_per_side", "half_spread_rate", "slippage_rate_per_side"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.per_side_rate >= 1:
            raise ValueError("combined per-side transaction cost must be below 100%")

    @property
    def per_side_rate(self) -> Decimal:
        return self.fee_rate_per_side + self.half_spread_rate + self.slippage_rate_per_side


@dataclass(frozen=True, slots=True)
class BenchmarkCurve:
    initial_equity: Decimal
    observations: tuple[EquityObservation, ...]
    trade_pnls: tuple[Decimal, ...]


def _validate_bars(bars: tuple[PriceBar, ...], timeframe: str) -> None:
    if timeframe not in TIMEFRAME_DELTAS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    if len(bars) < 2:
        raise ValueError("at least two bars are required")
    expected_delta = TIMEFRAME_DELTAS[timeframe]
    for previous, current in pairwise(bars):
        if current.timestamp - previous.timestamp != expected_delta:
            raise ValueError("bars must be strictly increasing on the expected timeframe grid")


def _validate_initial_equity(initial_equity: Decimal) -> None:
    if not isinstance(initial_equity, Decimal):
        raise TypeError("initial_equity must be Decimal")
    if not initial_equity.is_finite() or initial_equity <= 0:
        raise ValueError("initial_equity must be finite and positive")


def build_cash_benchmark(
    bars: tuple[PriceBar, ...], *, timeframe: str, initial_equity: Decimal
) -> BenchmarkCurve:
    """构造零收益、零暴露的现金基准。"""

    _validate_bars(bars, timeframe)
    _validate_initial_equity(initial_equity)
    return BenchmarkCurve(
        initial_equity=initial_equity,
        observations=tuple(
            EquityObservation(bar.timestamp, initial_equity, Decimal("0"), Decimal("0"))
            for bar in bars
        ),
        trade_pnls=(),
    )


def build_buy_and_hold_benchmark(
    bars: tuple[PriceBar, ...],
    *,
    timeframe: str,
    initial_equity: Decimal,
    costs: TransactionCostModel,
) -> BenchmarkCurve:
    """首根 open 买入并持有，末根 close 强制平仓，完整计入双边成本。"""

    _validate_bars(bars, timeframe)
    _validate_initial_equity(initial_equity)
    rate = costs.per_side_rate
    quantity = initial_equity / (bars[0].open * (Decimal("1") + rate))
    entry_notional = quantity * bars[0].open
    observations: list[EquityObservation] = []

    for index, bar in enumerate(bars):
        traded_notional = entry_notional if index == 0 else Decimal("0")
        if index == len(bars) - 1:
            exit_notional = quantity * bar.close
            equity = exit_notional * (Decimal("1") - rate)
            traded_notional += exit_notional
        else:
            equity = quantity * bar.close
        observations.append(EquityObservation(bar.timestamp, equity, Decimal("1"), traded_notional))

    final_equity = observations[-1].equity
    return BenchmarkCurve(initial_equity, tuple(observations), (final_equity - initial_equity,))


def build_equal_weight_buy_and_hold_benchmark(
    first_bars: tuple[PriceBar, ...],
    second_bars: tuple[PriceBar, ...],
    *,
    timeframe: str,
    initial_equity: Decimal,
    costs: TransactionCostModel,
) -> BenchmarkCurve:
    """构造初始 50/50、期间不再平衡的双资产 Buy-and-Hold 基准。"""

    _validate_bars(first_bars, timeframe)
    _validate_bars(second_bars, timeframe)
    _validate_initial_equity(initial_equity)
    if tuple(bar.timestamp for bar in first_bars) != tuple(bar.timestamp for bar in second_bars):
        raise ValueError("equal-weight benchmark requires exactly aligned timestamps")

    rate = costs.per_side_rate
    allocation = initial_equity / Decimal("2")
    first_quantity = allocation / (first_bars[0].open * (Decimal("1") + rate))
    second_quantity = allocation / (second_bars[0].open * (Decimal("1") + rate))
    entry_notional = first_quantity * first_bars[0].open + second_quantity * second_bars[0].open
    observations: list[EquityObservation] = []

    for index, (first, second) in enumerate(zip(first_bars, second_bars, strict=True)):
        first_value = first_quantity * first.close
        second_value = second_quantity * second.close
        traded_notional = entry_notional if index == 0 else Decimal("0")
        if index == len(first_bars) - 1:
            exit_notional = first_value + second_value
            equity = exit_notional * (Decimal("1") - rate)
            traded_notional += exit_notional
        else:
            equity = first_value + second_value
        observations.append(
            EquityObservation(first.timestamp, equity, Decimal("1"), traded_notional)
        )

    final_equity = observations[-1].equity
    return BenchmarkCurve(initial_equity, tuple(observations), (final_equity - initial_equity,))


def build_simple_moving_average_benchmark(
    bars: tuple[PriceBar, ...],
    *,
    timeframe: str,
    initial_equity: Decimal,
    window: int,
    costs: TransactionCostModel,
) -> BenchmarkCurve:
    """构造 close>SMA(window) 的 long/flat 工程基准。

    当前 candle close 只生成目标仓位，最早在下一根 candle open 执行；末根 close
    强制平仓用于统一结算。该基准只验证研究链路，不参与 Donchian 参数选择。
    """

    _validate_bars(bars, timeframe)
    _validate_initial_equity(initial_equity)
    if window <= 1:
        raise ValueError("moving-average window must be greater than one")

    rate = costs.per_side_rate
    cash = initial_equity
    quantity = Decimal("0")
    entry_equity: Decimal | None = None
    pending_target: bool | None = None
    closes: list[Decimal] = []
    observations: list[EquityObservation] = []
    trade_pnls: list[Decimal] = []

    for index, bar in enumerate(bars):
        traded_notional = Decimal("0")
        if pending_target is True and quantity == 0:
            entry_equity = cash
            quantity = cash / (bar.open * (Decimal("1") + rate))
            traded_notional = quantity * bar.open
            cash = Decimal("0")
        elif pending_target is False and quantity > 0:
            exit_notional = quantity * bar.open
            cash = exit_notional * (Decimal("1") - rate)
            traded_notional = exit_notional
            assert entry_equity is not None
            trade_pnls.append(cash - entry_equity)
            quantity = Decimal("0")
            entry_equity = None

        exposed_during_period = quantity > 0
        if index == len(bars) - 1 and quantity > 0:
            exit_notional = quantity * bar.close
            cash = exit_notional * (Decimal("1") - rate)
            traded_notional += exit_notional
            assert entry_equity is not None
            trade_pnls.append(cash - entry_equity)
            quantity = Decimal("0")
            entry_equity = None
        equity = cash + quantity * bar.close
        observations.append(
            EquityObservation(
                bar.timestamp,
                equity,
                Decimal("1") if exposed_during_period else Decimal("0"),
                traded_notional,
            )
        )

        # 信号严格在本 candle 完成后产生，只能影响下一根 open。
        closes.append(bar.close)
        if len(closes) >= window:
            moving_average = sum(closes[-window:], Decimal("0")) / Decimal(window)
            pending_target = bar.close > moving_average

    return BenchmarkCurve(initial_equity, tuple(observations), tuple(trade_pnls))
