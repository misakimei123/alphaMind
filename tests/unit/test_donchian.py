from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alphamind.research.donchian import (
    Candle,
    DonchianDecision,
    DonchianParameters,
    DonchianReason,
    DonchianSignal,
    evaluate_donchian,
)


def candle(
    offset: int,
    *,
    high: str,
    low: str,
    close: str,
    is_closed: bool = True,
    interval: timedelta = timedelta(hours=4),
) -> Candle:
    close_value = Decimal(close)
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + interval * offset,
        open=close_value,
        high=Decimal(high),
        low=Decimal(low),
        close=close_value,
        volume=Decimal("1"),
        is_closed=is_closed,
    )


PARAMETERS = DonchianParameters(entry_window=3, exit_window=2)


def test_entry_threshold_excludes_signal_candle() -> None:
    candles = [
        candle(0, high="100", low="90", close="95"),
        candle(1, high="102", low="91", close="98"),
        candle(2, high="101", low="93", close="99"),
        candle(3, high="110", low="99", close="105"),
    ]

    decision = evaluate_donchian(candles, PARAMETERS, in_position=False)

    assert decision.signal is DonchianSignal.OPEN_LONG
    assert decision.reason is DonchianReason.ENTRY_BREAKOUT
    assert decision.entry_threshold == Decimal("102")
    assert decision.reference_price == Decimal("105")
    assert decision.signal_timestamp == candles[-1].timestamp


def test_signal_decision_does_not_claim_a_fill() -> None:
    decision_fields = {field.name for field in fields(DonchianDecision)}

    assert "fill_price" not in decision_fields
    assert "fill_timestamp" not in decision_fields


def test_equal_to_channel_boundary_is_not_a_breakout() -> None:
    candles = [
        candle(0, high="100", low="90", close="95"),
        candle(1, high="102", low="91", close="98"),
        candle(2, high="101", low="93", close="99"),
        candle(3, high="105", low="99", close="102"),
    ]

    decision = evaluate_donchian(candles, PARAMETERS, in_position=False)

    assert decision.signal is DonchianSignal.HOLD
    assert decision.reason is DonchianReason.NO_BREAKOUT


def test_exit_uses_lagged_low_channel() -> None:
    candles = [
        candle(0, high="110", low="100", close="105"),
        candle(1, high="109", low="98", close="101"),
        candle(2, high="107", low="96", close="99"),
        candle(3, high="100", low="90", close="95"),
    ]

    decision = evaluate_donchian(candles, PARAMETERS, in_position=True)

    assert decision.signal is DonchianSignal.CLOSE_LONG
    assert decision.reason is DonchianReason.EXIT_BREAKOUT
    assert decision.exit_threshold == Decimal("96")


def test_unclosed_candle_fails_closed() -> None:
    candles = [
        candle(0, high="100", low="90", close="95"),
        candle(1, high="102", low="91", close="98"),
        candle(2, high="101", low="93", close="99"),
        candle(3, high="110", low="99", close="105", is_closed=False),
    ]

    decision = evaluate_donchian(candles, PARAMETERS, in_position=False)

    assert decision.signal is DonchianSignal.HOLD
    assert decision.reason is DonchianReason.CANDLE_NOT_CLOSED


def test_warmup_and_data_gap_fail_closed() -> None:
    warmup = evaluate_donchian(
        [
            candle(0, high="100", low="90", close="95"),
            candle(1, high="102", low="91", close="98"),
        ],
        PARAMETERS,
        in_position=False,
    )
    gap = evaluate_donchian(
        [
            candle(0, high="100", low="90", close="95"),
            candle(1, high="102", low="91", close="98"),
            candle(2, high="101", low="93", close="99"),
            candle(4, high="110", low="99", close="105"),
        ],
        PARAMETERS,
        in_position=False,
    )

    assert warmup.reason is DonchianReason.WARMUP
    assert gap.reason is DonchianReason.DATA_GAP


def test_warmup_reason_precedes_short_window_quality_reasons() -> None:
    decision = evaluate_donchian(
        [
            candle(0, high="100", low="90", close="95"),
            candle(2, high="102", low="91", close="98", is_closed=False),
        ],
        PARAMETERS,
        in_position=False,
    )

    assert decision.signal is DonchianSignal.HOLD
    assert decision.reason is DonchianReason.WARMUP
    assert decision.entry_threshold is None
    assert decision.exit_threshold is None


def test_gap_before_active_window_does_not_block_current_signal() -> None:
    candles = [
        candle(0, high="90", low="80", close="85"),
        candle(2, high="100", low="90", close="95"),
        candle(3, high="102", low="91", close="98"),
        candle(4, high="101", low="93", close="99"),
        candle(5, high="110", low="99", close="105"),
    ]

    decision = evaluate_donchian(candles, PARAMETERS, in_position=False)

    assert decision.signal is DonchianSignal.OPEN_LONG
    assert decision.reason is DonchianReason.ENTRY_BREAKOUT


def test_invalid_candle_and_timestamp_order_are_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        candle(0, high="NaN", low="90", close="95")

    candles = [
        candle(0, high="100", low="90", close="95"),
        candle(0, high="102", low="91", close="98"),
    ]
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_donchian(candles, PARAMETERS, in_position=False)

    with pytest.raises(TypeError, match="in_position must be bool"):
        evaluate_donchian(candles[:1], PARAMETERS, in_position=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("interval", [timedelta(hours=4), timedelta(days=1)])
def test_utc_signal_timestamp_is_stable_for_primary_and_robustness_timeframes(
    interval: timedelta,
) -> None:
    parameters = DonchianParameters(entry_window=3, exit_window=2, expected_interval=interval)
    candles = [
        candle(0, high="100", low="90", close="95", interval=interval),
        candle(1, high="102", low="91", close="98", interval=interval),
        candle(2, high="101", low="93", close="99", interval=interval),
        candle(3, high="110", low="99", close="105", interval=interval),
    ]

    decisions = {
        pair: evaluate_donchian(candles, parameters, in_position=False)
        for pair in ("BTC/USDT", "ETH/USDT")
    }

    assert decisions["BTC/USDT"] == decisions["ETH/USDT"]
    assert decisions["BTC/USDT"].signal_timestamp == datetime(2026, 1, 1, tzinfo=UTC) + (
        interval * 3
    )
    assert decisions["BTC/USDT"].signal_timestamp.utcoffset() == timedelta(0)
