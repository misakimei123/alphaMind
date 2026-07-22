from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphamind.config import load_effective_config
from alphamind.operations import OperationalControlAction, OperationalControlStore
from alphamind.scheduler import (
    CycleInvocation,
    CycleRunner,
    CycleRunResult,
    CycleScheduler,
    CycleStateStore,
    CycleStatus,
    CycleTrigger,
    build_read_only_snapshot_handler,
    next_schedule_utc,
    wait_until_worker_released,
)
from alphamind.scheduler.cli import main as scheduler_main

PROJECT_ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 7, 18, 12, 7, 30, tzinfo=UTC)


def _runner(
    tmp_path: Path,
    handler: object,
    *,
    timeout_seconds: float = 1,
) -> tuple[CycleRunner, CycleStateStore]:
    assert callable(handler)
    store = CycleStateStore(tmp_path / "state" / "cycles.sqlite")
    return (
        CycleRunner(
            state_store=store,
            lock_path=tmp_path / "state" / "cycles.lock",
            snapshot_directory=tmp_path / "snapshots",
            timeout_seconds=timeout_seconds,
            effective_config_sha256="a" * 64,
            handler=handler,
            now=lambda: NOW,
        ),
        store,
    )


def _copy_configuration_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "configs", root / "configs")
    shutil.copytree(PROJECT_ROOT / "data" / "schemas", root / "data" / "schemas")
    shutil.copytree(PROJECT_ROOT / "prompts", root / "prompts")
    return root


def test_schedule_boundaries_are_utc_aligned_and_do_not_backfill() -> None:
    assert next_schedule_utc(NOW, 30) == datetime(2026, 7, 18, 12, 30, tzinfo=UTC)
    exact = datetime(2026, 7, 18, 13, 0, tzinfo=UTC)
    assert next_schedule_utc(exact, 30) == exact


def test_successful_manual_cycle_publishes_atomic_snapshot_and_state(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path, lambda invocation: {"cycle": invocation.cycle_id})

    result = runner.run(CycleTrigger.MANUAL)

    assert result.status is CycleStatus.SUCCEEDED
    assert re.fullmatch(r"cycle-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", result.cycle_id)
    assert result.snapshot_path is not None
    payload = result.snapshot_path.read_bytes()
    document = json.loads(payload)
    assert document["cycle"]["cycle_id"] == result.cycle_id
    assert document["cycle"]["trigger"] == "manual"
    assert document["observation"] == {"cycle": result.cycle_id}
    assert result.snapshot_sha256 == hashlib.sha256(payload).hexdigest()
    assert not list(result.snapshot_path.parent.glob("*.tmp"))
    record = store.recent(1)[0]
    assert record.status == "SUCCEEDED"
    assert record.worker_finished_at_utc is not None
    assert record.snapshot_sha256 == result.snapshot_sha256


def test_manual_and_scheduled_triggers_cannot_overlap(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_handler(_: CycleInvocation) -> dict[str, bool]:
        entered.set()
        release.wait(2)
        return {"released": True}

    first, store = _runner(tmp_path, blocking_handler)
    second, _ = _runner(tmp_path, lambda _: {"must_not_run": True})
    holder: list[CycleRunResult] = []
    thread = threading.Thread(
        target=lambda: holder.append(first.run(CycleTrigger.SCHEDULED, scheduled_for_utc=NOW)),
        daemon=True,
    )
    thread.start()
    assert entered.wait(1)

    skipped = second.run(CycleTrigger.MANUAL)
    assert skipped.status is CycleStatus.SKIPPED_OVERLAP
    assert skipped.blocked_by_cycle_id is not None
    skipped_record = next(row for row in store.recent() if row.cycle_id == skipped.cycle_id)
    assert skipped_record.status == "SKIPPED_OVERLAP"

    release.set()
    thread.join(1)
    assert holder and holder[0].status is CycleStatus.SUCCEEDED


def test_timeout_keeps_overlap_lock_until_worker_really_finishes(tmp_path: Path) -> None:
    release = threading.Event()

    def slow_handler(_: CycleInvocation) -> dict[str, bool]:
        release.wait(2)
        return {"late": True}

    timed_runner, store = _runner(tmp_path, slow_handler, timeout_seconds=0.03)
    other_runner, _ = _runner(tmp_path, lambda _: {"next": True})
    timed_out = timed_runner.run(CycleTrigger.MANUAL)

    assert timed_out.status is CycleStatus.TIMED_OUT
    assert other_runner.run(CycleTrigger.MANUAL).status is CycleStatus.SKIPPED_OVERLAP
    timed_record = next(row for row in store.recent() if row.cycle_id == timed_out.cycle_id)
    assert timed_record.status == "TIMED_OUT"
    assert timed_record.worker_finished_at_utc is None

    release.set()
    assert wait_until_worker_released(store, timed_out.cycle_id)
    assert other_runner.run(CycleTrigger.MANUAL).status is CycleStatus.SUCCEEDED
    timed_record = next(row for row in store.recent() if row.cycle_id == timed_out.cycle_id)
    assert timed_record.status == "TIMED_OUT"
    assert timed_record.snapshot_path is None


def test_handler_failure_records_only_error_class_and_releases_lock(tmp_path: Path) -> None:
    def failing_handler(_: CycleInvocation) -> dict[str, object]:
        raise RuntimeError("sensitive failure detail")

    runner, store = _runner(tmp_path, failing_handler)
    result = runner.run(CycleTrigger.MANUAL)

    assert result.status is CycleStatus.FAILED
    assert result.error_class == "RuntimeError"
    assert result.snapshot_path is None
    serialized = json.dumps(store.recent(1)[0].to_dict())
    assert "sensitive failure detail" not in serialized

    publishing_runner, publishing_store = _runner(
        tmp_path / "publish",
        lambda _: {"not_json": {1, 2}},
    )
    publishing_result = publishing_runner.run(CycleTrigger.MANUAL)
    assert publishing_result.status is CycleStatus.FAILED
    assert publishing_result.error_class == "TypeError"
    assert publishing_store.recent(1)[0].status == "FAILED"


def test_restart_marks_unlocked_running_cycle_abandoned_before_next_cycle(
    tmp_path: Path,
) -> None:
    store = CycleStateStore(tmp_path / "state" / "cycles.sqlite")
    abandoned_id = "cycle-20260718T120000Z-deadbeef"
    store.create_running(
        CycleInvocation(
            cycle_id=abandoned_id,
            trigger=CycleTrigger.SCHEDULED,
            requested_at_utc=NOW,
            scheduled_for_utc=NOW,
            started_at_utc=NOW,
            deadline_utc=NOW,
            effective_config_sha256="a" * 64,
        )
    )
    runner = CycleRunner(
        state_store=store,
        lock_path=tmp_path / "state" / "cycles.lock",
        snapshot_directory=tmp_path / "snapshots",
        timeout_seconds=1,
        effective_config_sha256="a" * 64,
        handler=lambda _: {"restarted": True},
        now=lambda: NOW,
    )

    assert runner.run(CycleTrigger.MANUAL).status is CycleStatus.SUCCEEDED
    abandoned = next(row for row in store.recent() if row.cycle_id == abandoned_id)
    assert abandoned.status == "ABANDONED"
    assert abandoned.error_class == "ProcessExited"


def test_default_observer_lists_capability_markets_and_fails_closed_without_risk(
    tmp_path: Path,
) -> None:
    root = _copy_configuration_project(tmp_path)
    effective = load_effective_config(root, environ={})
    runner, _ = _runner(tmp_path, build_read_only_snapshot_handler(effective))

    result = runner.run(CycleTrigger.MANUAL)
    assert result.snapshot_path is not None
    observation = json.loads(result.snapshot_path.read_text(encoding="utf-8"))["observation"]

    assert observation["read_only"] is True
    assert observation["operational_control"] == {
        "schema_version": 1,
        "revision": 0,
        "ai_paused": False,
        "entry_stopped": False,
        "emergency": False,
        "manual_review_required": False,
        "cancel_pending_entries": False,
        "safe_exit_allowed": True,
        "reason_codes": ["NORMAL_OPERATION"],
        "updated_at_utc": None,
    }
    assert observation["market_capability"]["available_spot_pairs"] == [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "HYPE/USDT",
    ]
    assert observation["market_capability"]["available_futures_pairs"] == [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "HYPE/USDT:USDT",
    ]
    assert observation["risk_snapshot"] == {
        "path": str(Path("user_data/risk/risk-snapshot.json")),
        "available": False,
        "entry_allowed": False,
        "close_only": True,
        "kill_switch": False,
        "safe_exit_allowed": True,
        "reason_codes": ["snapshot_missing"],
        "snapshot": None,
    }
    assert observation["deferred_to_later_stages"] == [
        "news_collection",
        "model_decision",
        "telegram_approval",
        "trade_execution",
    ]


def test_observer_projects_entry_stop_into_effective_risk_state(tmp_path: Path) -> None:
    root = _copy_configuration_project(tmp_path)
    effective = load_effective_config(root, environ={})
    path = root / str(effective.runtime["operations"]["control_store_path"])
    with OperationalControlStore(path) as controls:
        controls.apply(
            OperationalControlAction.STOP_ENTRIES,
            occurred_at_utc=NOW,
            actor_user_id_sha256="a" * 64,
            actor_chat_id_sha256="b" * 64,
            idempotency_key="test:scheduler:stop-entries",
        )
    runner, _ = _runner(tmp_path / "runner", build_read_only_snapshot_handler(effective))

    result = runner.run(CycleTrigger.MANUAL)

    assert result.snapshot_path is not None
    observation = json.loads(result.snapshot_path.read_text(encoding="utf-8"))["observation"]
    assert observation["operational_control"]["entry_stopped"] is True
    assert observation["risk_snapshot"]["entry_allowed"] is False
    assert observation["risk_snapshot"]["close_only"] is True
    assert "operational_entry_stopped" in observation["risk_snapshot"]["reason_codes"]


def test_daemon_uses_scheduled_trigger_and_preserves_boundary(tmp_path: Path) -> None:
    stop = threading.Event()
    observed: list[str] = []
    current = [NOW]

    def handler(invocation: CycleInvocation) -> dict[str, bool]:
        observed.append(invocation.trigger.value)
        stop.set()
        return {"scheduled": True}

    runner, store = _runner(tmp_path, handler)
    scheduler = CycleScheduler(
        runner,
        interval_minutes=30,
        now=lambda: current[0],
        poll_seconds=0.01,
    )
    thread = threading.Thread(target=lambda: scheduler.run_forever(stop), daemon=True)
    thread.start()
    current[0] = datetime(2026, 7, 18, 12, 30, tzinfo=UTC)
    thread.join(1)
    assert observed == ["scheduled"]
    record = store.recent(1)[0]
    assert record.scheduled_for_utc == "2026-07-18T12:30:00.000000Z"


def test_cli_manual_trigger_and_status_survive_process_style_reopen(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _copy_configuration_project(tmp_path)

    assert scheduler_main(["--project-root", str(root)]) == 0
    trigger_output = json.loads(capsys.readouterr().out)
    assert trigger_output["status"] == "SUCCEEDED"

    assert scheduler_main(["--project-root", str(root), "--status"]) == 0
    status_output = json.loads(capsys.readouterr().out)
    assert status_output["cycles"][0]["cycle_id"] == trigger_output["cycle_id"]
    assert status_output["cycles"][0]["status"] == "SUCCEEDED"
