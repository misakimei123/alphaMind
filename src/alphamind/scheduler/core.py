"""R1-06 不可重叠周期调度、超时和 SQLite 状态记录。"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

JsonObject = dict[str, Any]
CycleHandler = Callable[["CycleInvocation"], Mapping[str, Any]]
Now = Callable[[], datetime]


class CycleTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class CycleStatus(StrEnum):
    ABANDONED = "ABANDONED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    SKIPPED_OVERLAP = "SKIPPED_OVERLAP"


@dataclass(frozen=True, slots=True)
class CycleInvocation:
    cycle_id: str
    trigger: CycleTrigger
    requested_at_utc: datetime
    scheduled_for_utc: datetime | None
    started_at_utc: datetime
    deadline_utc: datetime
    effective_config_sha256: str


@dataclass(frozen=True, slots=True)
class CycleRunResult:
    cycle_id: str
    status: CycleStatus
    trigger: CycleTrigger
    snapshot_path: Path | None
    snapshot_sha256: str | None
    blocked_by_cycle_id: str | None
    error_class: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "status": self.status.value,
            "trigger": self.trigger.value,
            "snapshot_path": None if self.snapshot_path is None else str(self.snapshot_path),
            "snapshot_sha256": self.snapshot_sha256,
            "blocked_by_cycle_id": self.blocked_by_cycle_id,
            "error_class": self.error_class,
        }


@dataclass(frozen=True, slots=True)
class CycleRecord:
    cycle_id: str
    trigger: str
    status: str
    requested_at_utc: str
    scheduled_for_utc: str | None
    started_at_utc: str | None
    deadline_utc: str | None
    completed_at_utc: str | None
    worker_finished_at_utc: str | None
    effective_config_sha256: str
    snapshot_path: str | None
    snapshot_sha256: str | None
    blocked_by_cycle_id: str | None
    error_class: str | None
    owner_pid: int

    def to_dict(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in (
                "cycle_id",
                "trigger",
                "status",
                "requested_at_utc",
                "scheduled_for_utc",
                "started_at_utc",
                "deadline_utc",
                "completed_at_utc",
                "worker_finished_at_utc",
                "effective_config_sha256",
                "snapshot_path",
                "snapshot_sha256",
                "blocked_by_cycle_id",
                "error_class",
                "owner_pid",
            )
        }


def _require_utc(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    _require_utc(value, field_name="timestamp")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _cycle_id(now_utc: datetime) -> str:
    _require_utc(now_utc, field_name="now_utc")
    stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    return f"cycle-{stamp}-{uuid.uuid4().hex[:8]}"


def next_schedule_utc(now_utc: datetime, interval_minutes: int) -> datetime:
    """返回位于或晚于 now 的 UTC epoch 对齐周期边界。"""

    _require_utc(now_utc, field_name="now_utc")
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    interval_seconds = interval_minutes * 60
    timestamp = now_utc.timestamp()
    boundary = int(timestamp // interval_seconds) * interval_seconds
    if timestamp > boundary:
        boundary += interval_seconds
    return datetime.fromtimestamp(boundary, tz=UTC)


class CycleStateStore:
    """只保存调度元数据；不成为账户、订单或交易事实权威。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduler_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                );
                INSERT OR IGNORE INTO scheduler_schema(singleton, schema_version) VALUES (1, 1);
                CREATE TABLE IF NOT EXISTS cycles (
                    cycle_id TEXT PRIMARY KEY,
                    trigger TEXT NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
                    status TEXT NOT NULL CHECK (status IN (
                        'ABANDONED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT',
                        'SKIPPED_OVERLAP'
                    )),
                    requested_at_utc TEXT NOT NULL,
                    scheduled_for_utc TEXT,
                    started_at_utc TEXT,
                    deadline_utc TEXT,
                    completed_at_utc TEXT,
                    worker_finished_at_utc TEXT,
                    effective_config_sha256 TEXT NOT NULL,
                    snapshot_path TEXT,
                    snapshot_sha256 TEXT,
                    blocked_by_cycle_id TEXT,
                    error_class TEXT,
                    owner_pid INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cycles_requested_at
                    ON cycles(requested_at_utc DESC);
                """
            )
            version = connection.execute(
                "SELECT schema_version FROM scheduler_schema WHERE singleton = 1"
            ).fetchone()
            if version is None or version[0] != 1:
                raise RuntimeError("scheduler state schema version is unsupported")

    def create_running(self, invocation: CycleInvocation) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cycles(
                    cycle_id, trigger, status, requested_at_utc, scheduled_for_utc,
                    started_at_utc, deadline_utc, effective_config_sha256, owner_pid
                ) VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?)
                """,
                (
                    invocation.cycle_id,
                    invocation.trigger.value,
                    _utc_text(invocation.requested_at_utc),
                    _utc_text(invocation.scheduled_for_utc),
                    _utc_text(invocation.started_at_utc),
                    _utc_text(invocation.deadline_utc),
                    invocation.effective_config_sha256,
                    os.getpid(),
                ),
            )

    def recover_abandoned(self, recovered_at_utc: datetime) -> tuple[str, ...]:
        """持有进程锁后，把上次进程遗留的 RUNNING 周期显式收口。"""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT cycle_id FROM cycles WHERE status = 'RUNNING' ORDER BY rowid"
            ).fetchall()
            cycle_ids = tuple(str(row[0]) for row in rows)
            connection.execute(
                """
                UPDATE cycles
                SET status = 'ABANDONED', completed_at_utc = ?, worker_finished_at_utc = ?,
                    error_class = 'ProcessExited'
                WHERE status = 'RUNNING'
                """,
                (_utc_text(recovered_at_utc), _utc_text(recovered_at_utc)),
            )
        return cycle_ids

    def create_skipped(
        self,
        *,
        cycle_id: str,
        trigger: CycleTrigger,
        requested_at_utc: datetime,
        scheduled_for_utc: datetime | None,
        effective_config_sha256: str,
        blocked_by_cycle_id: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cycles(
                    cycle_id, trigger, status, requested_at_utc, scheduled_for_utc,
                    completed_at_utc, worker_finished_at_utc, effective_config_sha256,
                    blocked_by_cycle_id, owner_pid
                ) VALUES (?, ?, 'SKIPPED_OVERLAP', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    trigger.value,
                    _utc_text(requested_at_utc),
                    _utc_text(scheduled_for_utc),
                    _utc_text(requested_at_utc),
                    _utc_text(requested_at_utc),
                    effective_config_sha256,
                    blocked_by_cycle_id,
                    os.getpid(),
                ),
            )

    def finish(
        self,
        cycle_id: str,
        *,
        status: CycleStatus,
        completed_at_utc: datetime,
        snapshot_path: Path | None = None,
        snapshot_sha256: str | None = None,
        error_class: str | None = None,
    ) -> None:
        if status not in {CycleStatus.SUCCEEDED, CycleStatus.FAILED, CycleStatus.TIMED_OUT}:
            raise ValueError("finish status is invalid")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE cycles
                SET status = ?, completed_at_utc = ?, snapshot_path = ?,
                    snapshot_sha256 = ?, error_class = ?
                WHERE cycle_id = ? AND status = 'RUNNING'
                """,
                (
                    status.value,
                    _utc_text(completed_at_utc),
                    None if snapshot_path is None else str(snapshot_path),
                    snapshot_sha256,
                    error_class,
                    cycle_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("cycle state transition lost ownership")

    def mark_worker_finished(self, cycle_id: str, finished_at_utc: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE cycles SET worker_finished_at_utc = ?
                WHERE cycle_id = ? AND worker_finished_at_utc IS NULL
                """,
                (_utc_text(finished_at_utc), cycle_id),
            )

    def latest_blocking_cycle_id(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cycle_id FROM cycles
                WHERE status = 'RUNNING'
                   OR (status = 'TIMED_OUT' AND worker_finished_at_utc IS NULL)
                ORDER BY requested_at_utc DESC, rowid DESC LIMIT 1
                """
            ).fetchone()
        return None if row is None else str(row[0])

    def recent(self, limit: int = 20) -> tuple[CycleRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("status limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cycles ORDER BY requested_at_utc DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(CycleRecord(**dict(row)) for row in rows)


class _HeldCycleLock:
    def __init__(self, path: Path, lock: FileLock) -> None:
        self.path = path
        self._lock = lock
        self._released = False
        self._guard = threading.Lock()

    @classmethod
    def try_acquire(cls, path: Path) -> _HeldCycleLock | None:
        resolved = path.resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(resolved, timeout=0, thread_local=False)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return None
        return cls(resolved, lock)

    def release(self) -> None:
        with self._guard:
            if self._released:
                return
            self._lock.release()
            self._released = True


def _publish_snapshot(
    directory: Path,
    cycle_id: str,
    document: Mapping[str, Any],
) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{cycle_id}.json"
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{cycle_id}.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, hashlib.sha256(payload).hexdigest()


class CycleRunner:
    def __init__(
        self,
        *,
        state_store: CycleStateStore,
        lock_path: str | Path,
        snapshot_directory: str | Path,
        timeout_seconds: float,
        effective_config_sha256: str,
        handler: CycleHandler,
        now: Now = _now_utc,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.state_store = state_store
        self.lock_path = Path(lock_path).resolve()
        self.snapshot_directory = Path(snapshot_directory).resolve()
        self.timeout_seconds = timeout_seconds
        self.effective_config_sha256 = effective_config_sha256
        self.handler = handler
        self.now = now

    def run(
        self,
        trigger: CycleTrigger | str,
        *,
        scheduled_for_utc: datetime | None = None,
    ) -> CycleRunResult:
        cycle_trigger = CycleTrigger(trigger)
        requested_at = self.now()
        _require_utc(requested_at, field_name="requested_at_utc")
        if scheduled_for_utc is not None:
            _require_utc(scheduled_for_utc, field_name="scheduled_for_utc")
        cycle_id = _cycle_id(requested_at)
        held_lock = _HeldCycleLock.try_acquire(self.lock_path)
        if held_lock is None:
            blocked_by = self.state_store.latest_blocking_cycle_id()
            self.state_store.create_skipped(
                cycle_id=cycle_id,
                trigger=cycle_trigger,
                requested_at_utc=requested_at,
                scheduled_for_utc=scheduled_for_utc,
                effective_config_sha256=self.effective_config_sha256,
                blocked_by_cycle_id=blocked_by,
            )
            return CycleRunResult(
                cycle_id,
                CycleStatus.SKIPPED_OVERLAP,
                cycle_trigger,
                None,
                None,
                blocked_by,
                None,
            )

        started_at = self.now()
        deadline = started_at + timedelta(seconds=self.timeout_seconds)
        invocation = CycleInvocation(
            cycle_id,
            cycle_trigger,
            requested_at,
            scheduled_for_utc,
            started_at,
            deadline,
            self.effective_config_sha256,
        )
        try:
            self.state_store.recover_abandoned(started_at)
            self.state_store.create_running(invocation)
        except Exception:
            held_lock.release()
            raise
        outcomes: queue.Queue[tuple[JsonObject | None, str | None]] = queue.Queue(maxsize=1)
        worker_finished = threading.Event()

        def execute() -> None:
            try:
                outcomes.put((dict(self.handler(invocation)), None))
            except Exception as error:
                outcomes.put((None, type(error).__name__))
            finally:
                worker_finished.set()

        worker = threading.Thread(
            target=execute,
            name=f"alphamind-{cycle_id}",
            daemon=True,
        )
        worker.start()
        try:
            observation, error_class = outcomes.get(timeout=self.timeout_seconds)
        except queue.Empty:
            completed_at = self.now()
            self.state_store.finish(
                cycle_id,
                status=CycleStatus.TIMED_OUT,
                completed_at_utc=completed_at,
                error_class="CycleTimeout",
            )

            def release_after_worker() -> None:
                worker_finished.wait()
                try:
                    self.state_store.mark_worker_finished(cycle_id, self.now())
                finally:
                    held_lock.release()

            threading.Thread(
                target=release_after_worker,
                name=f"alphamind-timeout-cleanup-{cycle_id}",
                daemon=True,
            ).start()
            return CycleRunResult(
                cycle_id,
                CycleStatus.TIMED_OUT,
                cycle_trigger,
                None,
                None,
                None,
                "CycleTimeout",
            )

        completed_at = self.now()
        snapshot_path: Path | None = None
        snapshot_sha256: str | None = None
        status = CycleStatus.FAILED if error_class is not None else CycleStatus.SUCCEEDED
        try:
            if observation is not None:
                document = {
                    "schema_version": 1,
                    "cycle": {
                        "cycle_id": cycle_id,
                        "trigger": cycle_trigger.value,
                        "requested_at_utc": _utc_text(requested_at),
                        "scheduled_for_utc": _utc_text(scheduled_for_utc),
                        "started_at_utc": _utc_text(started_at),
                        "deadline_utc": _utc_text(deadline),
                        "completed_at_utc": _utc_text(completed_at),
                        "effective_config_sha256": self.effective_config_sha256,
                    },
                    "observation": observation,
                }
                try:
                    snapshot_path, snapshot_sha256 = _publish_snapshot(
                        self.snapshot_directory, cycle_id, document
                    )
                except (OSError, TypeError, ValueError) as error:
                    status = CycleStatus.FAILED
                    error_class = type(error).__name__
                    snapshot_path = None
                    snapshot_sha256 = None
            self.state_store.finish(
                cycle_id,
                status=status,
                completed_at_utc=completed_at,
                snapshot_path=snapshot_path,
                snapshot_sha256=snapshot_sha256,
                error_class=error_class,
            )
            self.state_store.mark_worker_finished(cycle_id, completed_at)
        finally:
            held_lock.release()
        return CycleRunResult(
            cycle_id,
            status,
            cycle_trigger,
            snapshot_path,
            snapshot_sha256,
            None,
            error_class,
        )


class CycleScheduler:
    def __init__(
        self,
        runner: CycleRunner,
        *,
        interval_minutes: int,
        now: Now = _now_utc,
        poll_seconds: float = 1.0,
    ) -> None:
        if interval_minutes <= 0 or poll_seconds <= 0:
            raise ValueError("scheduler interval and poll must be positive")
        self.runner = runner
        self.interval_minutes = interval_minutes
        self.now = now
        self.poll_seconds = poll_seconds

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        interval = timedelta(minutes=self.interval_minutes)
        next_due = next_schedule_utc(self.now(), self.interval_minutes)
        while not stop.is_set():
            current = self.now()
            wait_seconds = max(0.0, (next_due - current).total_seconds())
            if wait_seconds > 0:
                stop.wait(min(wait_seconds, self.poll_seconds))
                continue
            self.runner.run(CycleTrigger.SCHEDULED, scheduled_for_utc=next_due)
            current = self.now()
            next_due += interval
            while next_due <= current:
                next_due += interval


def wait_until_worker_released(
    store: CycleStateStore,
    cycle_id: str,
    *,
    timeout_seconds: float = 2.0,
) -> bool:
    """仅供运维/测试等待超时 worker 退出，不参与调度决策。"""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = next((row for row in store.recent(1000) if row.cycle_id == cycle_id), None)
        if record is not None and record.worker_finished_at_utc is not None:
            return True
        time.sleep(0.01)
    return False
