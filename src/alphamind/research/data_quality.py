"""P1-04 开发池 OHLCV 确定性质量规则。"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from numbers import Real
from typing import Literal, TypedDict

Severity = Literal["ERROR", "WARN"]

TIMEFRAME_DELTAS = {
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}

# 跳变仅用于人工诊断，不删除数据、不阻止 clean 发布。固定阈值避免数据驱动调参。
ABNORMAL_JUMP_WARN_THRESHOLDS = {
    "4h": 0.20,
    "1d": 0.30,
}


class QualityIssue(TypedDict):
    severity: Severity
    code: str
    row_index: int | None
    timestamp_utc: str | None
    details: dict[str, object]


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(UTC)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _issue(
    severity: Severity,
    code: str,
    *,
    row_index: int | None = None,
    timestamp: datetime | None = None,
    details: Mapping[str, object] | None = None,
) -> QualityIssue:
    return {
        "severity": severity,
        "code": code,
        "row_index": row_index,
        "timestamp_utc": _utc_text(timestamp) if timestamp is not None else None,
        "details": dict(details or {}),
    }


def _missing_intervals(
    missing: Sequence[datetime],
    delta: timedelta,
) -> list[tuple[datetime, datetime, int]]:
    if not missing:
        return []
    intervals: list[tuple[datetime, datetime, int]] = []
    start = missing[0]
    previous = missing[0]
    count = 1
    for current in missing[1:]:
        if current == previous + delta:
            previous = current
            count += 1
            continue
        intervals.append((start, previous + delta, count))
        start = current
        previous = current
        count = 1
    intervals.append((start, previous + delta, count))
    return intervals


def validate_partition(
    rows: Sequence[Mapping[str, object]],
    *,
    timeframe: str,
    interval_start: datetime,
    interval_end_exclusive: datetime,
) -> dict[str, object]:
    """校验一个开发池分区；函数只报告问题，不修改、补齐或重排输入。"""

    if timeframe not in TIMEFRAME_DELTAS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    start = _parse_utc(interval_start)
    end = _parse_utc(interval_end_exclusive)
    if start is None or end is None or start >= end:
        raise ValueError("quality interval must be a non-empty UTC range")

    delta = TIMEFRAME_DELTAS[timeframe]
    issues: list[QualityIssue] = []
    parsed_timestamps: list[tuple[int, datetime]] = []
    valid_closes: dict[datetime, float] = {}

    for row_index, row in enumerate(rows):
        timestamp = _parse_utc(row.get("date", row.get("timestamp")))
        if timestamp is None:
            issues.append(_issue("ERROR", "timestamp_not_utc", row_index=row_index))
        else:
            parsed_timestamps.append((row_index, timestamp))
            if not (start <= timestamp < end):
                issues.append(
                    _issue(
                        "ERROR",
                        "timestamp_out_of_development_range",
                        row_index=row_index,
                        timestamp=timestamp,
                    )
                )
            if (timestamp - start) % delta != timedelta(0):
                issues.append(
                    _issue(
                        "ERROR",
                        "timestamp_off_grid",
                        row_index=row_index,
                        timestamp=timestamp,
                    )
                )

        numbers: dict[str, float] = {}
        for field in ("open", "high", "low", "close", "volume"):
            number = _finite_number(row.get(field))
            if number is None:
                issues.append(
                    _issue(
                        "ERROR",
                        "non_finite_value",
                        row_index=row_index,
                        timestamp=timestamp,
                        details={"field": field},
                    )
                )
            else:
                numbers[field] = number

        for field in ("open", "high", "low", "close"):
            value = numbers.get(field)
            if value is not None and value <= 0:
                issues.append(
                    _issue(
                        "ERROR",
                        "nonpositive_price",
                        row_index=row_index,
                        timestamp=timestamp,
                        details={"field": field},
                    )
                )

        price_fields = ("open", "high", "low", "close")
        if all(field in numbers and numbers[field] > 0 for field in price_fields):
            open_price = numbers["open"]
            high = numbers["high"]
            low = numbers["low"]
            close = numbers["close"]
            if not (low <= min(open_price, close) <= max(open_price, close) <= high):
                issues.append(
                    _issue(
                        "ERROR",
                        "invalid_ohlc_relation",
                        row_index=row_index,
                        timestamp=timestamp,
                    )
                )
            elif timestamp is not None:
                valid_closes.setdefault(timestamp, close)

        volume = numbers.get("volume")
        if volume is not None and volume < 0:
            issues.append(
                _issue(
                    "ERROR",
                    "negative_volume",
                    row_index=row_index,
                    timestamp=timestamp,
                )
            )
        elif volume == 0:
            issues.append(
                _issue(
                    "WARN",
                    "zero_volume",
                    row_index=row_index,
                    timestamp=timestamp,
                )
            )

    for (previous_index, previous), (current_index, current) in pairwise(parsed_timestamps):
        if current <= previous:
            issues.append(
                _issue(
                    "ERROR",
                    "non_increasing_timestamp",
                    row_index=current_index,
                    timestamp=current,
                    details={"previous_row_index": previous_index},
                )
            )

    timestamp_counts = Counter(timestamp for _, timestamp in parsed_timestamps)
    for timestamp, count in sorted(timestamp_counts.items()):
        if count > 1:
            issues.append(
                _issue(
                    "ERROR",
                    "duplicate_timestamp",
                    timestamp=timestamp,
                    details={"count": count},
                )
            )

    expected: list[datetime] = []
    current = start
    while current < end:
        expected.append(current)
        current += delta
    observed = {timestamp for _, timestamp in parsed_timestamps if start <= timestamp < end}
    missing = [timestamp for timestamp in expected if timestamp not in observed]
    for missing_start, missing_end, count in _missing_intervals(missing, delta):
        issues.append(
            _issue(
                "ERROR",
                "missing_candle_interval",
                timestamp=missing_start,
                details={
                    "end_exclusive_utc": _utc_text(missing_end),
                    "candle_count": count,
                },
            )
        )

    threshold = ABNORMAL_JUMP_WARN_THRESHOLDS[timeframe]
    sorted_closes = sorted(valid_closes.items())
    for (previous_time, previous_close), (current_time, current_close) in pairwise(sorted_closes):
        if current_time != previous_time + delta:
            continue
        absolute_return = abs(current_close / previous_close - 1.0)
        if absolute_return >= threshold:
            issues.append(
                _issue(
                    "WARN",
                    "abnormal_close_jump",
                    timestamp=current_time,
                    details={
                        "absolute_return": round(absolute_return, 12),
                        "threshold": threshold,
                    },
                )
            )

    issues.sort(
        key=lambda item: (
            item["timestamp_utc"] or "",
            item["row_index"] if item["row_index"] is not None else -1,
            item["severity"],
            item["code"],
        )
    )
    counts = Counter(issue["code"] for issue in issues)
    error_count = sum(issue["severity"] == "ERROR" for issue in issues)
    warning_count = sum(issue["severity"] == "WARN" for issue in issues)
    if error_count:
        status = "REJECTED"
    elif warning_count:
        status = "ACCEPTED_WITH_WARNINGS"
    else:
        status = "ACCEPTED"
    return {
        "status": status,
        "timeframe": timeframe,
        "interval_start": _utc_text(start),
        "interval_end_exclusive": _utc_text(end),
        "input_row_count": len(rows),
        "expected_candle_count": len(expected),
        "observed_unique_in_range_count": len(observed),
        "error_count": error_count,
        "warning_count": warning_count,
        "counts_by_code": dict(sorted(counts.items())),
        "issues": issues,
    }
