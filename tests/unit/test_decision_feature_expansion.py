from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise

import pytest

from alphamind.candles import CompletedCandle
from alphamind.decision.features import (
    DEFAULT_CORE_FEATURE_PARAMETERS,
    PATTERN_SEMANTICS,
    _detect_pattern,
    _ema_alignment,
    _key_location,
    build_core_features,
)

AS_OF = datetime(2026, 7, 21, 12, tzinfo=UTC)
INTERVAL = timedelta(minutes=30)
CYCLE_ID = "cycle-20260721T120000Z-a1b2c3d4"
QUANTUM = Decimal("0.00000001")


def _candle(
    *,
    index: int,
    open_price: str,
    high: str,
    low: str,
    close: str,
) -> CompletedCandle:
    start = AS_OF - INTERVAL * (2 - index)
    return CompletedCandle(
        started_at_utc=start,
        completed_at_utc=start + INTERVAL,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
    )


def _series(closes: list[Decimal]) -> tuple[CompletedCandle, ...]:
    start = AS_OF - INTERVAL * len(closes)
    rows: list[CompletedCandle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        opened = previous
        rows.append(
            CompletedCandle(
                started_at_utc=start + INTERVAL * index,
                completed_at_utc=start + INTERVAL * (index + 1),
                open=opened,
                high=max(opened, close) + Decimal("1"),
                low=min(opened, close) - Decimal("1"),
                close=close,
                volume=Decimal(100 + index),
            )
        )
        previous = close
    return tuple(rows)


def _reference_rsi(closes: list[Decimal], period: int = 14) -> Decimal:
    deltas = [current - previous for previous, current in pairwise(closes)]
    gains = [max(delta, Decimal(0)) for delta in deltas]
    losses = [max(-delta, Decimal(0)) for delta in deltas]
    average_gain = sum(gains[:period]) / Decimal(period)
    average_loss = sum(losses[:period]) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        average_gain = (average_gain * Decimal(period - 1) + gain) / Decimal(period)
        average_loss = (average_loss * Decimal(period - 1) + loss) / Decimal(period)
    if average_loss == 0:
        return Decimal(100).quantize(QUANTUM)
    if average_gain == 0:
        return Decimal(0).quantize(QUANTUM)
    value = Decimal(100) - Decimal(100) / (Decimal(1) + average_gain / average_loss)
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def _reference_adx(candles: tuple[CompletedCandle, ...], period: int = 14) -> Decimal:
    transitions: list[tuple[Decimal, Decimal, Decimal]] = []
    for previous, current in pairwise(candles):
        upward = current.high - previous.high
        downward = previous.low - current.low
        transitions.append(
            (
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                ),
                upward if upward > downward and upward > 0 else Decimal(0),
                downward if downward > upward and downward > 0 else Decimal(0),
            )
        )
    smoothed = [sum(item[column] for item in transitions[:period]) for column in range(3)]

    def dx() -> Decimal:
        plus_di = Decimal(100) * smoothed[1] / smoothed[0]
        minus_di = Decimal(100) * smoothed[2] / smoothed[0]
        total = plus_di + minus_di
        return Decimal(0) if total == 0 else Decimal(100) * abs(plus_di - minus_di) / total

    dx_values = [dx()]
    for transition in transitions[period:]:
        for column in range(3):
            smoothed[column] = (
                smoothed[column] - smoothed[column] / Decimal(period) + transition[column]
            )
        dx_values.append(dx())
    adx = sum(dx_values[:period]) / Decimal(period)
    for value in dx_values[period:]:
        adx = (adx * Decimal(period - 1) + value) / Decimal(period)
    return adx.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def test_rsi_and_adx_match_independent_wilder_recalculation_and_bounds() -> None:
    closes = [Decimal(100)]
    moves = (1, 2, -1, 3, -2, 1, -3, 2)
    for index in range(1, 65):
        closes.append(closes[-1] + Decimal(moves[index % len(moves)]))
    candles = _series(closes)

    result = build_core_features(
        candles,
        cycle_id=CYCLE_ID,
        timeframe="30m",
        as_of_utc=AS_OF,
    )

    assert result.rsi == _reference_rsi(closes)
    assert result.adx == _reference_adx(candles)
    assert result.rsi is not None and Decimal(0) <= result.rsi <= Decimal(100)
    assert result.adx is not None and Decimal(0) <= result.adx <= Decimal(100)


def test_rsi_extremes_and_zero_movement_are_not_replaced_with_neutral_values() -> None:
    rising = _series([Decimal(100 + index) for index in range(60)])
    falling = _series([Decimal(200 - index) for index in range(60)])
    flat = _series([Decimal(100) for _ in range(60)])
    zero_range = tuple(
        CompletedCandle(
            started_at_utc=AS_OF - INTERVAL * (60 - index),
            completed_at_utc=AS_OF - INTERVAL * (59 - index),
            open=Decimal(100),
            high=Decimal(100),
            low=Decimal(100),
            close=Decimal(100),
            volume=Decimal(100),
        )
        for index in range(60)
    )

    rising_result = build_core_features(rising, cycle_id=CYCLE_ID, timeframe="30m", as_of_utc=AS_OF)
    falling_result = build_core_features(
        falling, cycle_id=CYCLE_ID, timeframe="30m", as_of_utc=AS_OF
    )
    flat_result = build_core_features(flat, cycle_id=CYCLE_ID, timeframe="30m", as_of_utc=AS_OF)
    zero_range_result = build_core_features(
        zero_range, cycle_id=CYCLE_ID, timeframe="30m", as_of_utc=AS_OF
    )

    assert (rising_result.rsi, rising_result.adx) == (Decimal("100.00000000"),) * 2
    assert (falling_result.rsi, falling_result.adx) == (Decimal("0E-8"), Decimal("100.00000000"))
    assert flat_result.rsi is None and "rsi_unavailable" in flat_result.reason_codes
    assert flat_result.adx == Decimal("0E-8")
    assert zero_range_result.adx is None and "adx_unavailable" in zero_range_result.reason_codes
    assert zero_range_result.atr is None and "atr_unavailable" in zero_range_result.reason_codes


@pytest.mark.parametrize(
    ("close", "fast", "slow", "atr", "expected"),
    [
        ("110", "108", "100", "10", "strong_bullish"),
        ("105", "103", "100", "10", "bullish"),
        ("90", "92", "100", "10", "strong_bearish"),
        ("95", "97", "100", "10", "bearish"),
        ("101", "99", "100", "10", "mixed"),
        ("100", "100", "100", "0", None),
    ],
)
def test_ema_alignment_uses_frozen_half_atr_threshold(
    close: str,
    fast: str,
    slow: str,
    atr: str,
    expected: str | None,
) -> None:
    assert (
        _ema_alignment(
            close=Decimal(close),
            ema_fast=Decimal(fast),
            ema_slow=Decimal(slow),
            atr=Decimal(atr),
            parameters=DEFAULT_CORE_FEATURE_PARAMETERS,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("previous", "current", "location", "expected"),
    [
        (None, ("100", "122", "99", "121"), "support", "big_bullish"),
        (None, ("121", "122", "99", "100"), "resistance", "big_bearish"),
        (None, ("100", "102.1", "95", "102"), "support", "hammer"),
        (None, ("100", "102.1", "95", "102"), "resistance", "hanging_man"),
        (None, ("100", "107", "99.9", "102"), "resistance", "shooting_star"),
        (None, ("100", "107", "99.9", "102"), "support", "inverted_hammer"),
        (("105", "106", "99", "100"), ("99", "107", "98", "106"), "support", "bullish_engulfing"),
        (
            ("100", "106", "99", "105"),
            ("106", "107", "98", "99"),
            "resistance",
            "bearish_engulfing",
        ),
        (("120", "121", "99", "100"), ("105", "111", "104", "110"), "support", "bullish_harami"),
        (("100", "121", "99", "120"), ("115", "116", "109", "110"), "resistance", "bearish_harami"),
        (None, ("100", "105", "95", "100"), "support", "doji"),
    ],
)
def test_all_frozen_patterns_have_deterministic_priority_and_semantics(
    previous: tuple[str, str, str, str] | None,
    current: tuple[str, str, str, str],
    location: str,
    expected: str,
) -> None:
    rows = []
    if previous is not None:
        rows.append(
            _candle(
                index=0,
                open_price=previous[0],
                high=previous[1],
                low=previous[2],
                close=previous[3],
            )
        )
    rows.append(
        _candle(
            index=1,
            open_price=current[0],
            high=current[1],
            low=current[2],
            close=current[3],
        )
    )

    pattern = _detect_pattern(
        tuple(rows),
        atr=Decimal("10"),
        location=location,
        parameters=DEFAULT_CORE_FEATURE_PARAMETERS,
    )

    assert pattern == expected
    assert PATTERN_SEMANTICS[pattern] in PATTERN_SEMANTICS.values()


def test_key_location_filter_removes_noise_and_resolves_direction_without_bias() -> None:
    resistance = _candle(index=1, open_price="109", high="110.1", low="108", close="109.8")
    support = _candle(index=1, open_price="90", high="91", low="89.9", close="90.1")
    noise = _candle(index=1, open_price="100", high="101", low="99", close="100")

    assert (
        _key_location(
            resistance,
            donchian_upper=Decimal("110"),
            donchian_lower=Decimal("80"),
            ema_slow=Decimal("90"),
            atr=Decimal("1"),
            parameters=DEFAULT_CORE_FEATURE_PARAMETERS,
        )
        == "resistance"
    )
    assert (
        _key_location(
            support,
            donchian_upper=Decimal("120"),
            donchian_lower=Decimal("80"),
            ema_slow=Decimal("90"),
            atr=Decimal("1"),
            parameters=DEFAULT_CORE_FEATURE_PARAMETERS,
        )
        == "support"
    )
    assert (
        _key_location(
            noise,
            donchian_upper=Decimal("120"),
            donchian_lower=Decimal("80"),
            ema_slow=Decimal("90"),
            atr=Decimal("1"),
            parameters=DEFAULT_CORE_FEATURE_PARAMETERS,
        )
        is None
    )
    assert (
        _detect_pattern(
            (noise,),
            atr=Decimal("1"),
            location=None,
            parameters=DEFAULT_CORE_FEATURE_PARAMETERS,
        )
        is None
    )


def test_zero_range_and_equal_distance_conflict_emit_no_pattern() -> None:
    flat = _candle(index=1, open_price="100", high="100", low="100", close="100")
    assert (
        _detect_pattern(
            (flat,),
            atr=Decimal("1"),
            location="support",
            parameters=DEFAULT_CORE_FEATURE_PARAMETERS,
        )
        is None
    )
    assert (
        _key_location(
            flat,
            donchian_upper=Decimal("100"),
            donchian_lower=Decimal("100"),
            ema_slow=Decimal("90"),
            atr=Decimal("1"),
            parameters=DEFAULT_CORE_FEATURE_PARAMETERS,
        )
        is None
    )


def test_prefix_result_is_stable_and_future_candle_is_rejected() -> None:
    closes = [Decimal(100 + index) for index in range(65)]
    full = _series(closes)
    prefix = full[:60]
    first = build_core_features(
        prefix, cycle_id=CYCLE_ID, timeframe="30m", as_of_utc=prefix[-1].completed_at_utc
    )
    copied_from_longer_input = tuple(full)[:60]
    second = build_core_features(
        copied_from_longer_input,
        cycle_id=CYCLE_ID,
        timeframe="30m",
        as_of_utc=copied_from_longer_input[-1].completed_at_utc,
    )
    with_future = build_core_features(
        full,
        cycle_id=CYCLE_ID,
        timeframe="30m",
        as_of_utc=prefix[-1].completed_at_utc,
    )

    assert first.to_context_features() == second.to_context_features()
    assert first.input_sha256 == second.input_sha256
    assert with_future.reason_codes == ("future_candle",)


def test_cycle_id_is_part_of_feature_evidence_and_must_match_contract() -> None:
    candles = _series([Decimal(100 + index) for index in range(60)])
    first = build_core_features(candles, cycle_id=CYCLE_ID, timeframe="30m", as_of_utc=AS_OF)
    second = build_core_features(
        candles,
        cycle_id="cycle-20260721T120000Z-deadbeef",
        timeframe="30m",
        as_of_utc=AS_OF,
    )
    assert first.input_sha256 != second.input_sha256
    with pytest.raises(ValueError, match="cycle_id"):
        build_core_features(candles, cycle_id="invalid", timeframe="30m", as_of_utc=AS_OF)
