"""R2-06 DecisionContext 技术指标与关键位置 K 线形态纯计算。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from typing import Any

from alphamind.candles import CompletedCandle, timeframe_duration

CORE_FEATURE_VERSION = "r2-06-v2"
_OUTPUT_QUANTUM = Decimal("0.00000001")
_MAXIMUM_CANDLES = 1000
_CYCLE_ID_PATTERN = re.compile(r"^cycle-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")

PATTERN_SEMANTICS: dict[str, str] = {
    "big_bullish": "bullish_attack",
    "big_bearish": "bearish_attack",
    "hammer": "bullish_support_rejection",
    "hanging_man": "bearish_exhaustion",
    "shooting_star": "bearish_resistance_rejection",
    "inverted_hammer": "bullish_support_test",
    "bullish_engulfing": "bullish_reversal",
    "bearish_engulfing": "bearish_reversal",
    "bullish_harami": "bearish_momentum_exhaustion",
    "bearish_harami": "bullish_momentum_exhaustion",
    "doji": "indecision",
}


@dataclass(frozen=True, slots=True)
class CoreFeatureParameters:
    """冻结 R2-06 的全部数值窗口和硬规则阈值。"""

    donchian_upper_period: int = 20
    donchian_lower_period: int = 10
    atr_period: int = 20
    ema_fast_period: int = 20
    ema_slow_period: int = 50
    volume_period: int = 20
    rsi_period: int = 14
    adx_period: int = 14
    ema_strong_separation_atr: Decimal = Decimal("0.5")
    key_level_atr_tolerance: Decimal = Decimal("0.25")
    harami_previous_body_atr: Decimal = Decimal("1")
    harami_current_body_fraction: Decimal = Decimal("0.5")
    big_body_atr: Decimal = Decimal("2")
    shadow_body_ratio: Decimal = Decimal("2")
    opposite_shadow_body_ratio: Decimal = Decimal("0.1")
    doji_range_fraction: Decimal = Decimal("0.1")

    def __post_init__(self) -> None:
        periods = (
            self.donchian_upper_period,
            self.donchian_lower_period,
            self.atr_period,
            self.ema_fast_period,
            self.ema_slow_period,
            self.volume_period,
            self.rsi_period,
            self.adx_period,
        )
        if any(type(value) is not int or value < 2 or value > 500 for value in periods):
            raise ValueError("feature periods must be integers between 2 and 500")
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError("ema_fast_period must be smaller than ema_slow_period")
        positive_thresholds = (
            self.ema_strong_separation_atr,
            self.key_level_atr_tolerance,
            self.harami_previous_body_atr,
            self.harami_current_body_fraction,
            self.big_body_atr,
            self.shadow_body_ratio,
            self.opposite_shadow_body_ratio,
            self.doji_range_fraction,
        )
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            for value in positive_thresholds
        ):
            raise ValueError("feature thresholds must be finite positive Decimals")
        if self.harami_current_body_fraction >= 1 or self.doji_range_fraction >= 1:
            raise ValueError("body fractions must be smaller than one")


DEFAULT_CORE_FEATURE_PARAMETERS = CoreFeatureParameters()


@dataclass(frozen=True, slots=True)
class CoreFeatureSnapshot:
    """可写入 DecisionContext v2 的确定性结果和内部 point-in-time 证据。"""

    feature_version: str
    cycle_id: str
    timeframe: str
    completed_candle_at_utc: datetime | None
    input_sha256: str
    donchian_upper: Decimal | None
    donchian_lower: Decimal | None
    atr: Decimal | None
    ema_fast: Decimal | None
    ema_slow: Decimal | None
    volume_ratio: Decimal | None
    rsi: Decimal | None
    adx: Decimal | None
    ema_alignment: str | None
    candlestick_pattern: str | None
    pattern_semantic: str | None
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
                self.rsi,
                self.adx,
                self.ema_alignment,
            )
        )

    def to_context_features(self) -> dict[str, str | None]:
        """只输出 DecisionContext v2 受控字段；内部 reason/hash 由审计链保存。"""

        return {
            "timeframe": self.timeframe,
            "donchian_upper": _decimal_text(self.donchian_upper),
            "donchian_lower": _decimal_text(self.donchian_lower),
            "atr": _decimal_text(self.atr),
            "ema_fast": _decimal_text(self.ema_fast),
            "ema_slow": _decimal_text(self.ema_slow),
            "volume_ratio": _decimal_text(self.volume_ratio),
            "rsi": _decimal_text(self.rsi),
            "adx": _decimal_text(self.adx),
            "ema_alignment": self.ema_alignment,
            "candlestick_pattern": self.candlestick_pattern,
            "pattern_semantic": self.pattern_semantic,
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


def _parameter_document(parameters: CoreFeatureParameters) -> dict[str, int | str]:
    document: dict[str, int | str] = {}
    for key, value in asdict(parameters).items():
        if isinstance(value, Decimal):
            rendered = _decimal_text(value)
            if rendered is None:
                raise ValueError("feature parameter Decimal cannot be null")
            document[key] = rendered
        elif type(value) is int:
            document[key] = value
        else:
            raise TypeError("feature parameter type is unsupported")
    return document


def _input_sha256(
    candles: tuple[CompletedCandle, ...],
    *,
    cycle_id: str,
    timeframe: str,
    parameters: CoreFeatureParameters,
) -> str:
    document: dict[str, Any] = {
        "feature_version": CORE_FEATURE_VERSION,
        "cycle_id": cycle_id,
        "timeframe": timeframe,
        "parameters": _parameter_document(parameters),
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
    cycle_id: str,
    timeframe: str,
    candles: tuple[CompletedCandle, ...],
    parameters: CoreFeatureParameters,
    reason_code: str,
) -> CoreFeatureSnapshot:
    return CoreFeatureSnapshot(
        feature_version=CORE_FEATURE_VERSION,
        cycle_id=cycle_id,
        timeframe=timeframe,
        completed_candle_at_utc=candles[-1].completed_at_utc if candles else None,
        input_sha256=_input_sha256(
            candles,
            cycle_id=cycle_id,
            timeframe=timeframe,
            parameters=parameters,
        ),
        donchian_upper=None,
        donchian_lower=None,
        atr=None,
        ema_fast=None,
        ema_slow=None,
        volume_ratio=None,
        rsi=None,
        adx=None,
        ema_alignment=None,
        candlestick_pattern=None,
        pattern_semantic=None,
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


def _true_ranges(candles: tuple[CompletedCandle, ...]) -> tuple[Decimal, ...]:
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
    return tuple(ranges)


def _wilder_atr(candles: tuple[CompletedCandle, ...], period: int) -> Decimal | None:
    ranges = _true_ranges(candles)
    if len(ranges) < period:
        return None
    current = sum(ranges[:period]) / Decimal(period)
    for true_range in ranges[period:]:
        current = (current * Decimal(period - 1) + true_range) / Decimal(period)
    return _rounded(current)


def _wilder_rsi(closes: tuple[Decimal, ...], period: int) -> Decimal | None:
    if len(closes) < period + 1:
        return None
    deltas = tuple(current - previous for previous, current in pairwise(closes))
    gains = tuple(max(delta, Decimal(0)) for delta in deltas)
    losses = tuple(max(-delta, Decimal(0)) for delta in deltas)
    average_gain = sum(gains[:period]) / Decimal(period)
    average_loss = sum(losses[:period]) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        average_gain = (average_gain * Decimal(period - 1) + gain) / Decimal(period)
        average_loss = (average_loss * Decimal(period - 1) + loss) / Decimal(period)
    if average_gain == 0 and average_loss == 0:
        return None
    if average_loss == 0:
        return _rounded(Decimal(100))
    if average_gain == 0:
        return _rounded(Decimal(0))
    relative_strength = average_gain / average_loss
    return _rounded(Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength))


def _directional_transition(
    previous: CompletedCandle,
    current: CompletedCandle,
) -> tuple[Decimal, Decimal, Decimal]:
    upward = current.high - previous.high
    downward = previous.low - current.low
    plus_dm = upward if upward > downward and upward > 0 else Decimal(0)
    minus_dm = downward if downward > upward and downward > 0 else Decimal(0)
    true_range = max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )
    return true_range, plus_dm, minus_dm


def _dx(smoothed_tr: Decimal, smoothed_plus: Decimal, smoothed_minus: Decimal) -> Decimal | None:
    if smoothed_tr <= 0:
        return None
    plus_di = Decimal(100) * smoothed_plus / smoothed_tr
    minus_di = Decimal(100) * smoothed_minus / smoothed_tr
    denominator = plus_di + minus_di
    if denominator == 0:
        return Decimal(0)
    return Decimal(100) * abs(plus_di - minus_di) / denominator


def _wilder_adx(candles: tuple[CompletedCandle, ...], period: int) -> Decimal | None:
    # 首个 ADX 需要 14 个 transition 播种 DM/TR，再累计 14 个 DX，因此是 28 根 candle。
    if len(candles) < period * 2:
        return None
    transitions = tuple(
        _directional_transition(previous, current) for previous, current in pairwise(candles)
    )
    smoothed_tr = sum((item[0] for item in transitions[:period]), start=Decimal(0))
    smoothed_plus = sum((item[1] for item in transitions[:period]), start=Decimal(0))
    smoothed_minus = sum((item[2] for item in transitions[:period]), start=Decimal(0))
    first_dx = _dx(smoothed_tr, smoothed_plus, smoothed_minus)
    if first_dx is None:
        return None
    dx_values = [first_dx]
    for true_range, plus_dm, minus_dm in transitions[period:]:
        smoothed_tr = smoothed_tr - smoothed_tr / Decimal(period) + true_range
        smoothed_plus = smoothed_plus - smoothed_plus / Decimal(period) + plus_dm
        smoothed_minus = smoothed_minus - smoothed_minus / Decimal(period) + minus_dm
        current_dx = _dx(smoothed_tr, smoothed_plus, smoothed_minus)
        if current_dx is None:
            return None
        dx_values.append(current_dx)
    if len(dx_values) < period:
        return None
    current_adx = sum(dx_values[:period]) / Decimal(period)
    for current_dx in dx_values[period:]:
        current_adx = (current_adx * Decimal(period - 1) + current_dx) / Decimal(period)
    return _rounded(current_adx)


def _ema_alignment(
    *,
    close: Decimal,
    ema_fast: Decimal | None,
    ema_slow: Decimal | None,
    atr: Decimal | None,
    parameters: CoreFeatureParameters,
) -> str | None:
    if ema_fast is None or ema_slow is None or atr is None or atr <= 0:
        return None
    separation = abs(ema_fast - ema_slow)
    strong_threshold = parameters.ema_strong_separation_atr * atr
    if close > ema_fast > ema_slow:
        return "strong_bullish" if separation >= strong_threshold else "bullish"
    if close < ema_fast < ema_slow:
        return "strong_bearish" if separation >= strong_threshold else "bearish"
    return "mixed"


def _level_distance(close: Decimal, extreme: Decimal, level: Decimal) -> Decimal:
    return min(abs(close - level), abs(extreme - level))


def _key_location(
    candle: CompletedCandle,
    *,
    donchian_upper: Decimal | None,
    donchian_lower: Decimal | None,
    ema_slow: Decimal | None,
    atr: Decimal | None,
    parameters: CoreFeatureParameters,
) -> str | None:
    if (
        donchian_upper is None
        or donchian_lower is None
        or ema_slow is None
        or atr is None
        or atr <= 0
    ):
        return None
    tolerance = parameters.key_level_atr_tolerance * atr
    support_distances = [_level_distance(candle.close, candle.low, donchian_lower)]
    resistance_distances = [_level_distance(candle.close, candle.high, donchian_upper)]
    if candle.close > ema_slow:
        support_distances.append(_level_distance(candle.close, candle.low, ema_slow))
    elif candle.close < ema_slow:
        resistance_distances.append(_level_distance(candle.close, candle.high, ema_slow))
    support_distance = min(support_distances)
    resistance_distance = min(resistance_distances)
    support = support_distance <= tolerance
    resistance = resistance_distance <= tolerance
    if support and resistance:
        if support_distance < resistance_distance:
            return "support"
        if resistance_distance < support_distance:
            return "resistance"
        return None
    if support:
        return "support"
    if resistance:
        return "resistance"
    return None


def _body_bounds(candle: CompletedCandle) -> tuple[Decimal, Decimal]:
    return min(candle.open, candle.close), max(candle.open, candle.close)


def _detect_pattern(
    candles: tuple[CompletedCandle, ...],
    *,
    atr: Decimal | None,
    location: str | None,
    parameters: CoreFeatureParameters,
) -> str | None:
    if not candles or atr is None or atr <= 0 or location is None:
        return None
    current = candles[-1]
    current_low, current_high = _body_bounds(current)
    current_body = current_high - current_low

    if len(candles) >= 2:
        previous = candles[-2]
        previous_low, previous_high = _body_bounds(previous)
        previous_body = previous_high - previous_low
        if (
            previous.close < previous.open
            and current.close > current.open
            and current_low <= previous_low
            and current_high >= previous_high
        ):
            return "bullish_engulfing"
        if (
            previous.close > previous.open
            and current.close < current.open
            and current_low <= previous_low
            and current_high >= previous_high
        ):
            return "bearish_engulfing"
        large_previous = previous_body >= parameters.harami_previous_body_atr * atr
        small_current = current_body <= parameters.harami_current_body_fraction * previous_body
        contained = current_low >= previous_low and current_high <= previous_high
        if (
            large_previous
            and small_current
            and contained
            and previous.close < previous.open
            and current.close > current.open
        ):
            return "bullish_harami"
        if (
            large_previous
            and small_current
            and contained
            and previous.close > previous.open
            and current.close < current.open
        ):
            return "bearish_harami"

    if current_body > parameters.big_body_atr * atr:
        if current.close > current.open:
            return "big_bullish"
        if current.close < current.open:
            return "big_bearish"

    candle_range = current.high - current.low
    if current_body > 0:
        lower_shadow = current_low - current.low
        upper_shadow = current.high - current_high
        if (
            lower_shadow > parameters.shadow_body_ratio * current_body
            and upper_shadow < parameters.opposite_shadow_body_ratio * current_body
        ):
            return "hammer" if location == "support" else "hanging_man"
        if (
            upper_shadow > parameters.shadow_body_ratio * current_body
            and lower_shadow < parameters.opposite_shadow_body_ratio * current_body
        ):
            return "inverted_hammer" if location == "support" else "shooting_star"
    if candle_range > 0 and current_body < parameters.doji_range_fraction * candle_range:
        return "doji"
    return None


def build_core_features(
    candles: tuple[CompletedCandle, ...],
    *,
    cycle_id: str,
    timeframe: str,
    as_of_utc: datetime,
    parameters: CoreFeatureParameters = DEFAULT_CORE_FEATURE_PARAMETERS,
) -> CoreFeatureSnapshot:
    """从连续已完成 K 线生成 v2 特征；时间边界异常时整体 fail-closed。"""

    if not _CYCLE_ID_PATTERN.fullmatch(cycle_id):
        raise ValueError("cycle_id must match the DecisionContext cycle id contract")
    if as_of_utc.tzinfo is None or as_of_utc.utcoffset() != timedelta(0):
        raise ValueError("as_of_utc must use UTC")
    duration = timeframe_duration(timeframe)
    if not candles:
        return _unavailable_snapshot(
            cycle_id=cycle_id,
            timeframe=timeframe,
            candles=candles,
            parameters=parameters,
            reason_code="candles_missing",
        )
    if len(candles) > _MAXIMUM_CANDLES:
        return _unavailable_snapshot(
            cycle_id=cycle_id,
            timeframe=timeframe,
            candles=candles,
            parameters=parameters,
            reason_code="candle_limit_exceeded",
        )

    previous: CompletedCandle | None = None
    for candle in candles:
        if candle.completed_at_utc - candle.started_at_utc != duration:
            return _unavailable_snapshot(
                cycle_id=cycle_id,
                timeframe=timeframe,
                candles=candles,
                parameters=parameters,
                reason_code="timeframe_mismatch",
            )
        if candle.completed_at_utc > as_of_utc:
            return _unavailable_snapshot(
                cycle_id=cycle_id,
                timeframe=timeframe,
                candles=candles,
                parameters=parameters,
                reason_code="future_candle",
            )
        if previous is not None and candle.started_at_utc != previous.completed_at_utc:
            return _unavailable_snapshot(
                cycle_id=cycle_id,
                timeframe=timeframe,
                candles=candles,
                parameters=parameters,
                reason_code="candle_order_or_gap",
            )
        previous = candle

    if as_of_utc - candles[-1].completed_at_utc > duration:
        return _unavailable_snapshot(
            cycle_id=cycle_id,
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
    elif atr <= 0:
        atr = None
        reasons.append("atr_unavailable")
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

    rsi = _wilder_rsi(closes, parameters.rsi_period)
    if rsi is None:
        reasons.append(
            "rsi_warmup" if len(closes) < parameters.rsi_period + 1 else "rsi_unavailable"
        )
    adx = _wilder_adx(candles, parameters.adx_period)
    if adx is None:
        reasons.append(
            "adx_warmup" if len(candles) < parameters.adx_period * 2 else "adx_unavailable"
        )

    alignment = _ema_alignment(
        close=candles[-1].close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        atr=atr,
        parameters=parameters,
    )
    location = _key_location(
        candles[-1],
        donchian_upper=upper,
        donchian_lower=lower,
        ema_slow=ema_slow,
        atr=atr,
        parameters=parameters,
    )
    pattern = _detect_pattern(
        candles,
        atr=atr,
        location=location,
        parameters=parameters,
    )

    return CoreFeatureSnapshot(
        feature_version=CORE_FEATURE_VERSION,
        cycle_id=cycle_id,
        timeframe=timeframe,
        completed_candle_at_utc=candles[-1].completed_at_utc,
        input_sha256=_input_sha256(
            candles,
            cycle_id=cycle_id,
            timeframe=timeframe,
            parameters=parameters,
        ),
        donchian_upper=upper,
        donchian_lower=lower,
        atr=atr,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        volume_ratio=volume_ratio,
        rsi=rsi,
        adx=adx,
        ema_alignment=alignment,
        candlestick_pattern=pattern,
        pattern_semantic=PATTERN_SEMANTICS.get(pattern) if pattern is not None else None,
        reason_codes=tuple(reasons),
    )
