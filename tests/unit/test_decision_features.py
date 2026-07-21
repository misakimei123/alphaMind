from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

from alphamind.candles import CompletedCandle
from alphamind.config import load_effective_config
from alphamind.decision import DecisionContractBinder, build_core_features

PROJECT_ROOT = Path(__file__).parents[2]
AS_OF = datetime(2026, 7, 18, 12, tzinfo=UTC)
TIMEFRAME = "30m"
INTERVAL = timedelta(minutes=30)


def _candles(count: int = 60) -> tuple[CompletedCandle, ...]:
    started_at = AS_OF - INTERVAL * count
    candles: list[CompletedCandle] = []
    for index in range(count):
        price = Decimal(100 + index)
        start = started_at + INTERVAL * index
        candles.append(
            CompletedCandle(
                started_at_utc=start,
                completed_at_utc=start + INTERVAL,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=Decimal(100 + index),
            )
        )
    return tuple(candles)


def test_core_features_are_deterministic_point_in_time_and_context_compatible() -> None:
    first = build_core_features(_candles(), timeframe=TIMEFRAME, as_of_utc=AS_OF)
    second = build_core_features(_candles(), timeframe=TIMEFRAME, as_of_utc=AS_OF)

    assert first == second
    assert first.ready
    assert first.input_sha256 == "d8ac96830fb6da5fe40c68ab216eff7e4a9efe08af1080e030d9d694747e0e68"
    assert first.to_context_features() == {
        "timeframe": "30m",
        "donchian_upper": "159",
        "donchian_lower": "148",
        "atr": "2",
        "ema_fast": "149.5",
        "ema_slow": "134.5",
        "volume_ratio": "1.07070707",
    }

    effective = load_effective_config(PROJECT_ROOT, environ={})
    raw_context = yaml.safe_load(
        (PROJECT_ROOT / "tests/fixtures/contracts/decision-context.valid.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(raw_context, dict)
    context = deepcopy(raw_context)
    context["config_sha256"] = effective.effective_sha256
    context["instrument_registry_sha256"] = effective.instrument_registry.source_sha256
    context["instruments"][0]["completed_candle_at_utc"] = "2026-07-18T12:00:00Z"
    context["instruments"][0]["features"] = first.to_context_features()

    bound = DecisionContractBinder(effective).bind_context(
        context,
        now_utc=datetime(2026, 7, 18, 12, 0, 5, tzinfo=UTC),
    )

    assert bound.document["instruments"][0]["features"] == first.to_context_features()


def test_donchian_uses_previous_candles_but_other_indicators_include_latest_close() -> None:
    baseline = build_core_features(_candles(), timeframe=TIMEFRAME, as_of_utc=AS_OF)
    candles = list(_candles())
    latest = candles[-1]
    candles[-1] = CompletedCandle(
        started_at_utc=latest.started_at_utc,
        completed_at_utc=latest.completed_at_utc,
        open=Decimal("200"),
        high=Decimal("220"),
        low=Decimal("190"),
        close=Decimal("210"),
        volume=latest.volume,
    )

    changed = build_core_features(tuple(candles), timeframe=TIMEFRAME, as_of_utc=AS_OF)

    # 当前 signal candle 不能抬高/压低自身 Donchian 阈值，但应进入 ATR/EMA。
    assert changed.donchian_upper == baseline.donchian_upper
    assert changed.donchian_lower == baseline.donchian_lower
    assert changed.atr != baseline.atr
    assert changed.ema_fast != baseline.ema_fast
    assert changed.ema_slow != baseline.ema_slow


def test_warmup_zero_volume_and_missing_data_fail_closed_without_fabricated_values() -> None:
    warmup = build_core_features(_candles(10), timeframe=TIMEFRAME, as_of_utc=AS_OF)
    assert not warmup.ready
    assert warmup.ema_slow is None
    assert "ema_slow_warmup" in warmup.reason_codes

    candles = list(_candles())
    latest = candles[-1]
    candles[-1] = CompletedCandle(
        started_at_utc=latest.started_at_utc,
        completed_at_utc=latest.completed_at_utc,
        open=latest.open,
        high=latest.high,
        low=latest.low,
        close=latest.close,
        volume=Decimal(0),
    )
    no_volume = build_core_features(tuple(candles), timeframe=TIMEFRAME, as_of_utc=AS_OF)
    assert no_volume.volume_ratio is None
    assert "volume_ratio_unavailable" in no_volume.reason_codes

    missing = build_core_features((), timeframe=TIMEFRAME, as_of_utc=AS_OF)
    assert missing.to_context_features() == {
        "timeframe": "30m",
        "donchian_upper": None,
        "donchian_lower": None,
        "atr": None,
        "ema_fast": None,
        "ema_slow": None,
        "volume_ratio": None,
    }
    assert missing.reason_codes == ("candles_missing",)


def test_gap_future_stale_and_timeframe_mismatch_fail_closed_as_a_whole() -> None:
    candles = _candles()
    gap = candles[:30] + candles[31:]
    cases = {
        "candle_order_or_gap": (gap, AS_OF),
        "future_candle": (
            (
                *candles,
                CompletedCandle(
                    started_at_utc=AS_OF,
                    completed_at_utc=AS_OF + INTERVAL,
                    open=Decimal(160),
                    high=Decimal(161),
                    low=Decimal(159),
                    close=Decimal(160),
                    volume=Decimal(160),
                ),
            ),
            AS_OF,
        ),
        "completed_candle_stale": (candles, AS_OF + INTERVAL + timedelta(seconds=1)),
    }
    for reason, (rows, as_of) in cases.items():
        result = build_core_features(rows, timeframe=TIMEFRAME, as_of_utc=as_of)
        assert result.reason_codes == (reason,)
        assert all(
            value is None
            for key, value in result.to_context_features().items()
            if key != "timeframe"
        )

    mismatch = build_core_features(candles, timeframe="1h", as_of_utc=AS_OF)
    assert mismatch.reason_codes == ("timeframe_mismatch",)
