from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alphamind.research.anti_cheat import (
    DatasetAccessGuard,
    scan_strategy_source,
    validate_registry_selection,
    validate_signal_execution_separation,
    validate_timestamp_boundary,
)


def test_static_scan_accepts_rolling_and_row_wise_aggregation() -> None:
    source = """
def indicators(frame):
    frame["channel"] = frame["high"].rolling(20).max().shift(1)
    frame["row_high"] = frame[["open", "close"]].max(axis=1)
    return frame
"""
    assert scan_strategy_source(source) == ()


@pytest.mark.parametrize(
    ("expression", "rule"),
    [
        ("frame['x'].shift(-1)", "negative_shift"),
        ("frame.iloc[-1]", "absolute_positional_dataframe_access"),
        ("frame['x'].bfill()", "backward_fill"),
        ("scaler.fit_transform(frame)", "full_sample_transform"),
        ("frame['x'].mean()", "full_sample_aggregation"),
        ("frame['x'].rolling(3, center=True).mean()", "centered_window"),
    ],
)
def test_static_scan_rejects_future_and_full_sample_patterns(expression: str, rule: str) -> None:
    findings = scan_strategy_source(f"result = {expression}\n")
    assert rule in {finding.rule for finding in findings}


def test_dataset_access_guard_rejects_unapproved_and_escaping_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    approved = project / "data/clean"
    approved.mkdir(parents=True)
    guard = DatasetAccessGuard(project, [approved])

    assert (
        guard.approve(approved / "BTC_USDT-4h.feather")
        == (approved / "BTC_USDT-4h.feather").resolve()
    )
    with pytest.raises(PermissionError):
        guard.approve(project / "data/final_holdout/BTC_USDT-4h.feather")
    with pytest.raises(ValueError):
        guard.approve(tmp_path / "outside.feather")


def test_signal_execution_must_be_exactly_next_candle() -> None:
    trade = {
        "entry_signal_timestamp": "2025-01-01T00:00:00Z",
        "entry_timestamp": "2025-01-01T04:00:00Z",
    }
    assert validate_signal_execution_separation([trade], timedelta(hours=4)) == {
        "checked_trade_count": 1,
        "timing_mismatch_count": 0,
    }
    trade["entry_timestamp"] = "2025-01-01T00:00:00Z"
    with pytest.raises(RuntimeError, match="expected exactly"):
        validate_signal_execution_separation([trade], timedelta(hours=4))


def test_registry_check_rejects_unregistered_or_selected_trial() -> None:
    registry = {"entries": [{"experiment_id": "exp-1", "review_result": "PENDING"}]}
    summary = {
        "trials": [{"experiment_id": "exp-1"}],
        "selection": {"parameter_selection": None},
    }
    assert validate_registry_selection(registry, summary)["registered_trial_count"] == 1

    summary["trials"] = [{"experiment_id": "exp-2"}]
    with pytest.raises(RuntimeError, match="unregistered"):
        validate_registry_selection(registry, summary)


def test_timestamp_boundary_is_right_open() -> None:
    end = datetime(2025, 7, 1, tzinfo=UTC)
    accepted = [datetime(2025, 6, 30, 20, tzinfo=UTC)]
    assert validate_timestamp_boundary(accepted, end)["candle_count"] == 1
    with pytest.raises(RuntimeError, match="crosses"):
        validate_timestamp_boundary([end], end)
