"""交易所无关的已完成 K 线合同。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

_TIMEFRAME_PATTERN = re.compile(r"^([1-9][0-9]*)([mhdw])$")


def timeframe_duration(timeframe: str) -> timedelta:
    """把固定长度 timeframe 转为 timedelta；月线因长度不固定而拒绝。"""

    matched = _TIMEFRAME_PATTERN.fullmatch(timeframe)
    if matched is None:
        raise ValueError("timeframe must use a fixed m/h/d/w duration")
    amount = int(matched.group(1))
    units = {
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
    }
    return units[matched.group(2)]


@dataclass(frozen=True, slots=True)
class CompletedCandle:
    """只表达已经闭合且通过基本 OHLCV 关系校验的 candle。"""

    started_at_utc: datetime
    completed_at_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        for field_name in ("started_at_utc", "completed_at_utc"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError(f"{field_name} must use UTC")
        if self.completed_at_utc <= self.started_at_utc:
            raise ValueError("completed_at_utc must be after started_at_utc")
        prices = (self.open, self.high, self.low, self.close)
        if any(not value.is_finite() or value <= 0 for value in prices):
            raise ValueError("OHLC prices must be finite and positive")
        if not self.volume.is_finite() or self.volume < 0:
            raise ValueError("volume must be finite and nonnegative")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must cover open, low and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must cover open, high and close")
