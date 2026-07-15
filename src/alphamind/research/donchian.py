"""Point-in-time Donchian 信号纯函数。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise


class DonchianSignal(StrEnum):
    """策略层允许输出的 long/flat 信号。"""

    OPEN_LONG = "open_long"
    CLOSE_LONG = "close_long"
    HOLD = "hold"


class DonchianReason(StrEnum):
    """稳定的信号原因码，用于测试和后续审计映射。"""

    ENTRY_BREAKOUT = "entry_breakout"
    EXIT_BREAKOUT = "exit_breakout"
    NO_BREAKOUT = "no_breakout"
    WARMUP = "warmup"
    CANDLE_NOT_CLOSED = "candle_not_closed"
    DATA_GAP = "data_gap"


def _require_finite_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class Candle:
    """单根 UTC candle；价格统一使用 Decimal，避免二进制浮点漂移。"""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool = True

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")

        for name in ("open", "high", "low", "close", "volume"):
            _require_finite_decimal(name, getattr(self, name))

        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.volume < 0:
            raise ValueError("volume must not be negative")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must contain open, low and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must contain open, high and close")


@dataclass(frozen=True, slots=True)
class DonchianParameters:
    entry_window: int
    exit_window: int
    expected_interval: timedelta = timedelta(hours=4)

    def __post_init__(self) -> None:
        if self.entry_window <= 0 or self.exit_window <= 0:
            raise ValueError("Donchian windows must be positive")
        if self.expected_interval <= timedelta(0):
            raise ValueError("expected_interval must be positive")


@dataclass(frozen=True, slots=True)
class DonchianDecision:
    signal: DonchianSignal
    reason: DonchianReason
    signal_timestamp: datetime
    reference_price: Decimal
    entry_threshold: Decimal | None
    exit_threshold: Decimal | None


def evaluate_donchian(
    candles: Sequence[Candle],
    parameters: DonchianParameters,
    *,
    in_position: bool,
) -> DonchianDecision:
    """仅用当前及更早已完成 candle 计算 Donchian 信号。

    当前 candle 永远不参与 rolling high/low。函数只产生信号，不表达成交时点；
    运行 adapter 必须在后续可成交时点处理该信号。
    """

    if not candles:
        raise ValueError("candles must not be empty")

    latest = candles[-1]
    for previous, current in pairwise(candles):
        if current.timestamp <= previous.timestamp:
            raise ValueError("candle timestamps must be strictly increasing")

    required_candle_count = max(parameters.entry_window, parameters.exit_window) + 1
    active_window = candles[-required_candle_count:]

    # 只检查本次计算实际使用的窗口；窗口中的未完成 candle 一律 fail-closed。
    if any(not candle.is_closed for candle in active_window):
        return DonchianDecision(
            signal=DonchianSignal.HOLD,
            reason=DonchianReason.CANDLE_NOT_CLOSED,
            signal_timestamp=latest.timestamp,
            reference_price=latest.close,
            entry_threshold=None,
            exit_threshold=None,
        )

    # 有效窗口内的缺失 candle 不做静默填补；更早且未参与计算的数据不影响当前信号。
    if any(
        current.timestamp - previous.timestamp != parameters.expected_interval
        for previous, current in pairwise(active_window)
    ):
        return DonchianDecision(
            signal=DonchianSignal.HOLD,
            reason=DonchianReason.DATA_GAP,
            signal_timestamp=latest.timestamp,
            reference_price=latest.close,
            entry_threshold=None,
            exit_threshold=None,
        )

    history = active_window[:-1]
    if len(history) < max(parameters.entry_window, parameters.exit_window):
        return DonchianDecision(
            signal=DonchianSignal.HOLD,
            reason=DonchianReason.WARMUP,
            signal_timestamp=latest.timestamp,
            reference_price=latest.close,
            entry_threshold=None,
            exit_threshold=None,
        )

    # rolling 阈值严格滞后一根，防止当前 candle 同时抬高阈值并触发信号。
    entry_threshold = max(candle.high for candle in history[-parameters.entry_window :])
    exit_threshold = min(candle.low for candle in history[-parameters.exit_window :])

    if not in_position and latest.close > entry_threshold:
        signal = DonchianSignal.OPEN_LONG
        reason = DonchianReason.ENTRY_BREAKOUT
    elif in_position and latest.close < exit_threshold:
        signal = DonchianSignal.CLOSE_LONG
        reason = DonchianReason.EXIT_BREAKOUT
    else:
        signal = DonchianSignal.HOLD
        reason = DonchianReason.NO_BREAKOUT

    return DonchianDecision(
        signal=signal,
        reason=reason,
        signal_timestamp=latest.timestamp,
        reference_price=latest.close,
        entry_threshold=entry_threshold,
        exit_threshold=exit_threshold,
    )
