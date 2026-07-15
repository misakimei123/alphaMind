from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alphamind.research.donchian import (
    Candle,
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
) -> Candle:
    close_value = Decimal(close)
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=4 * offset),
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
