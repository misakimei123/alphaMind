"""R2-05 DecisionContext 核心技术特征纯计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from alphamind.candles import CompletedCandle, timeframe_duration

CORE_FEATURE_VERSION = "r2-05-v1"
_OUTPUT_QUANTUM = Decimal("0.00000001")
_MAXIMUM_CANDLES = 1000


@dataclass(frozen=True, slots=True)
class CoreFeatureParameters:
    """冻结 R2-05 核心特征窗口；R2-06 不得隐式修改这些参数。"""

    donchian_upper_period: int = 20
    donchian_lower_period: int = 10
    atr_period: int = 20
    ema_fast_period: int = 20
    ema_slow_period: int = 50
    volume_period: int = 20

    def __post_init__(self) -> None:
        values = tuple(asdict(self).values())
        if any(type(value) is not int or value < 2 or value > 500 for value in values):
            raise ValueError("feature periods must be integers between 2 and 500")
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError("ema_fast_period must be smaller than ema_slow_period")


DEFAULT_CORE_FEATURE_PARAMETERS = CoreFeatureParameters()


@dataclass(frozen=True, slots=True)
class CoreFeatureSnapshot:
    """可直接写入 DecisionContext.features 的确定性结果和内部证据。"""

    feature_version: str
    timeframe: str
    completed_candle_at_utc: datetime | None
    input_sha256: str
    donchian_upper: Decimal | None
    donchian_lower: Decimal | None
    atr: Decimal | None
    ema_fast: Decimal | None
    ema_slow: Decimal | None
    volume_ratio: Decimal | None
    reason_codes: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.reason_codes and all(
            value is not None
            for value in (
                self.donchian_upper,
                self.donchian_lower,
                self.atr,
                self.ema_fast,
                self.ema_slow,
                self.volume_ratio,
            )
        )

    def to_context_features(self) -> dict[str, str | None]:
        """只输出 schema v1 已冻结字段，内部 reason/hash 不泄漏到模型输入。"""

        return {
            "timeframe": self.timeframe,
            "donchian_upper": _decimal_text(self.donchian_upper),
            "donchian_lower": _decimal_text(self.donchian_lower),
            "atr": _decimal_text(self.atr),
            "ema_fast": _decimal_text(self.ema_fast),
            "ema_slow": _decimal_text(self.ema_slow),
            "volume_ratio": _decimal_text(self.volume_ratio),
        }


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(_OUTPUT_QUANTUM, rounding=ROUND_HALF_EVEN)


def _timestamp_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _input_sha256(
    candles: tuple[CompletedCandle, ...],
    *,
    timeframe: str,
    parameters: CoreFeatureParameters,
) -> str:
    document: dict[str, Any] = {
        "feature_version": CORE_FEATURE_VERSION,
        "timeframe": timeframe,
        "parameters": asdict(parameters),
        "candles": [
            {
                "started_at_utc": _timestamp_text(candle.started_at_utc),
                "completed_at_utc": _timestamp_text(candle.completed_at_utc),
                "open": _decimal_text(candle.open),
                "high": _decimal_text(candle.high),
                "low": _decimal_text(candle.low),
                "close": _decimal_text(candle.close),
                "volume": _decimal_text(candle.volume),
            }
            for candle in candles
        ],
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _unavailable_snapshot(
    *,
    timeframe: str,
    candles: tuple[CompletedCandle, ...],
    parameters: CoreFeatureParameters,
    reason_code: str,
) -> CoreFeatureSnapshot:
    return CoreFeatureSnapshot(
        feature_version=CORE_FEATURE_VERSION,
        timeframe=timeframe,
        completed_candle_at_utc=candles[-1].completed_at_utc if candles else None,
        input_sha256=_input_sha256(candles, timeframe=timeframe, parameters=parameters),
        donchian_upper=None,
        donchian_lower=None,
        atr=None,
        ema_fast=None,
        ema_slow=None,
        volume_ratio=None,
        reason_codes=(reason_code,),
    )


def _ema(closes: tuple[Decimal, ...], period: int) -> Decimal | None:
    if len(closes) < period:
        return None
    current = sum(closes[:period]) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    for close in closes[period:]:
        current = (close - current) * alpha + current
    return _rounded(current)


def _wilder_atr(candles: tuple[CompletedCandle, ...], period: int) -> Decimal | None:
    if len(candles) < period:
        return None
    ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for candle in candles:
        candidates = [candle.high - candle.low]
        if previous_close is not None:
            candidates.extend(
                (
                    abs(candle.high - previous_close),
                    abs(candle.low - previous_close),
                )
            )
        ranges.append(max(candidates))
        previous_close = candle.close
    current = sum(ranges[:period]) / Decimal(period)
    for true_range in ranges[period:]:
        current = (current * Decimal(period - 1) + true_range) / Decimal(period)
    return _rounded(current)


def build_core_features(
    candles: tuple[CompletedCandle, ...],
    *,
    timeframe: str,
    as_of_utc: datetime,
    parameters: CoreFeatureParameters = DEFAULT_CORE_FEATURE_PARAMETERS,
) -> CoreFeatureSnapshot:
    """从严格连续的已完成 K 线生成核心特征；任何时间边界异常均整体 fail-closed。"""

    if as_of_utc.tzinfo is None or as_of_utc.utcoffset() != timedelta(0):
        raise ValueError("as_of_utc must use UTC")
    duration = timeframe_duration(timeframe)
    if not candles:
        return _unavailable_snapshot(
            timeframe=timeframe,
            candles=candles,
            parameters=parameters,
            reason_code="candles_missing",
        )
    if len(candles) > _MAXIMUM_CANDLES:
        return _unavailable_snapshot(
            timeframe=timeframe,
            candles=candles,
            parameters=parameters,
            reason_code="candle_limit_exceeded",
        )

    previous: CompletedCandle | None = None
    for candle in candles:
        if candle.completed_at_utc - candle.started_at_utc != duration:
            return _unavailable_snapshot(
                timeframe=timeframe,
                candles=candles,
                parameters=parameters,
                reason_code="timeframe_mismatch",
            )
        if candle.completed_at_utc > as_of_utc:
            return _unavailable_snapshot(
                timeframe=timeframe,
                candles=candles,
                parameters=parameters,
                reason_code="future_candle",
            )
        if previous is not None and candle.started_at_utc != previous.completed_at_utc:
            return _unavailable_snapshot(
                timeframe=timeframe,
                candles=candles,
                parameters=parameters,
                reason_code="candle_order_or_gap",
            )
        previous = candle

    if as_of_utc - candles[-1].completed_at_utc > duration:
        return _unavailable_snapshot(
            timeframe=timeframe,
            candles=candles,
            parameters=parameters,
            reason_code="completed_candle_stale",
        )

    closes = tuple(candle.close for candle in candles)
    reasons: list[str] = []
    upper = None
    if len(candles) > parameters.donchian_upper_period:
        upper = max(candle.high for candle in candles[-parameters.donchian_upper_period - 1 : -1])
    else:
        reasons.append("donchian_upper_warmup")
    lower = None
    if len(candles) > parameters.donchian_lower_period:
        lower = min(candle.low for candle in candles[-parameters.donchian_lower_period - 1 : -1])
    else:
        reasons.append("donchian_lower_warmup")

    atr = _wilder_atr(candles, parameters.atr_period)
    if atr is None:
        reasons.append("atr_warmup")
    ema_fast = _ema(closes, parameters.ema_fast_period)
    if ema_fast is None:
        reasons.append("ema_fast_warmup")
    ema_slow = _ema(closes, parameters.ema_slow_period)
    if ema_slow is None:
        reasons.append("ema_slow_warmup")

    volume_ratio = None
    if len(candles) <= parameters.volume_period:
        reasons.append("volume_ratio_warmup")
    else:
        baseline = sum(
            candle.volume for candle in candles[-parameters.volume_period - 1 : -1]
        ) / Decimal(parameters.volume_period)
        if baseline <= 0 or candles[-1].volume <= 0:
            reasons.append("volume_ratio_unavailable")
        else:
            volume_ratio = _rounded(candles[-1].volume / baseline)

    return CoreFeatureSnapshot(
        feature_version=CORE_FEATURE_VERSION,
        timeframe=timeframe,
        completed_candle_at_utc=candles[-1].completed_at_utc,
        input_sha256=_input_sha256(candles, timeframe=timeframe, parameters=parameters),
        donchian_upper=upper,
        donchian_lower=lower,
        atr=atr,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        volume_ratio=volume_ratio,
        reason_codes=tuple(reasons),
    )
