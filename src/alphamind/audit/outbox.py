"""有界 SQLite WAL outbox，供 callback 只做本地持久化。"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alphamind.audit.events import MAX_EVENT_BYTES, canonical_json_bytes


class AuditBackpressureError(RuntimeError):
    """outbox 不可写或超过冻结阈值，调用方必须拒绝新入场。"""


@dataclass(frozen=True, slots=True)
class OutboxLimits:
    logical_capacity: int = 10_000
    warning_pending: int = 5_000
    warning_oldest_seconds: int = 120
    warning_file_bytes: int = 128 * 1024 * 1024
    entry_stop_pending: int = 8_000
    entry_stop_oldest_seconds: int = 300
    file_capacity_bytes: int = 256 * 1024 * 1024
    entry_stop_file_bytes: int = 192 * 1024 * 1024
    callback_timeout_seconds: float = 0.05
    maximum_attempts: int = 20
    lease_seconds: int = 60

    def __post_init__(self) -> None:
        if not 0 < self.entry_stop_pending < self.logical_capacity:
            raise ValueError("entry_stop_pending must reserve logical capacity")
        if not 0 < self.entry_stop_file_bytes < self.file_capacity_bytes:
            raise ValueError("entry_stop_file_bytes must reserve file capacity")


@dataclass(frozen=True, slots=True)
class BacklogMetrics:
    pending: int
    in_flight: int
    dead_letter: int
    delivered: int
    oldest_unconfirmed_seconds: float
    file_bytes: int
    warning: bool
    entry_backpressure: bool


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    event_id: str
    content_sha256: str
    event_json: str
    attempt_count: int


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("outbox timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class AuditOutbox:
    """保存未确认审计事件；不引用或修改 Freqtrade Runtime DB。"""

    def __init__(self, path: str | Path, *, limits: OutboxLimits | None = None) -> None:
        self.path = Path(path)
        self.limits = limits or OutboxLimits()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = self._connect()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.limits.callback_timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connection:
            journal_mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise RuntimeError("audit outbox requires SQLite WAL mode")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_outbox (
                    event_id TEXT PRIMARY KEY,
                    producer_component TEXT NOT NULL,
                    producer_instance_id TEXT NOT NULL,
                    producer_sequence INTEGER NOT NULL CHECK (producer_sequence >= 0),
                    content_sha256 TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    event_class TEXT NOT NULL CHECK (event_class IN ('ENTRY', 'SAFETY')),
                    state TEXT NOT NULL CHECK (
                        state IN ('PENDING', 'IN_FLIGHT', 'DELIVERED', 'DEAD_LETTER')
                    ),
                    created_at_utc TEXT NOT NULL,
                    next_attempt_at_utc TEXT NOT NULL,
                    lease_until_utc TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    delivered_at_utc TEXT,
                    last_error_class TEXT
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS audit_outbox_delivery_order
                ON audit_outbox(state, next_attempt_at_utc, producer_component, producer_sequence)
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_delivery_attempt (
                    event_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    attempted_at_utc TEXT NOT NULL,
                    error_class TEXT NOT NULL,
                    PRIMARY KEY (event_id, attempt_number),
                    FOREIGN KEY (event_id) REFERENCES audit_outbox(event_id) ON DELETE CASCADE
                )
                """
            )

    def close(self) -> None:
        self._connection.close()

    def _file_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            with contextlib.suppress(FileNotFoundError):
                total += candidate.stat().st_size
        return total

    def metrics(self, *, now: datetime) -> BacklogMetrics:
        rows = self._connection.execute(
            "SELECT state, COUNT(*) AS count FROM audit_outbox GROUP BY state"
        ).fetchall()
        counts = {str(row["state"]): int(row["count"]) for row in rows}
        oldest_row = self._connection.execute(
            """
            SELECT MIN(created_at_utc) AS oldest
            FROM audit_outbox
            WHERE state != 'DELIVERED'
            """
        ).fetchone()
        oldest = oldest_row["oldest"]
        oldest_seconds = 0.0
        if isinstance(oldest, str):
            oldest_seconds = max(0.0, (now - _parse_utc(oldest)).total_seconds())
        file_bytes = self._file_bytes()
        unconfirmed = (
            counts.get("PENDING", 0) + counts.get("IN_FLIGHT", 0) + counts.get("DEAD_LETTER", 0)
        )
        entry_backpressure = (
            unconfirmed >= self.limits.entry_stop_pending
            or oldest_seconds >= self.limits.entry_stop_oldest_seconds
            or file_bytes >= self.limits.entry_stop_file_bytes
        )
        warning = (
            unconfirmed >= self.limits.warning_pending
            or oldest_seconds >= self.limits.warning_oldest_seconds
            or file_bytes >= self.limits.warning_file_bytes
        )
        return BacklogMetrics(
            pending=counts.get("PENDING", 0),
            in_flight=counts.get("IN_FLIGHT", 0),
            dead_letter=counts.get("DEAD_LETTER", 0),
            delivered=counts.get("DELIVERED", 0),
            oldest_unconfirmed_seconds=oldest_seconds,
            file_bytes=file_bytes,
            warning=warning,
            entry_backpressure=entry_backpressure,
        )

    def next_sequence(self, *, producer_component: str, producer_instance_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT MAX(producer_sequence) AS maximum
            FROM audit_outbox
            WHERE producer_component = ? AND producer_instance_id = ?
            """,
            (producer_component, producer_instance_id),
        ).fetchone()
        maximum = row["maximum"]
        return 0 if maximum is None else int(maximum) + 1

    def append(
        self,
        event: dict[str, Any],
        *,
        event_class: str = "ENTRY",
        now: datetime | None = None,
    ) -> bool:
        """单事务追加事件；相同 ID+hash 视为幂等，冲突或背压均失败。"""

        if event_class not in {"ENTRY", "SAFETY"}:
            raise ValueError("event_class must be ENTRY or SAFETY")
        event_bytes = canonical_json_bytes(event)
        if len(event_bytes) > MAX_EVENT_BYTES:
            raise AuditBackpressureError("audit event exceeds 16 KiB")
        event_id = event.get("event_id")
        content_hash = event.get("event_content_sha256")
        producer = event.get("producer")
        if not isinstance(event_id, str) or not isinstance(content_hash, str):
            raise ValueError("audit event identity is missing")
        if not isinstance(producer, dict):
            raise ValueError("audit event producer is missing")
        if (
            event.get("runtime_authority") is not False
            or event.get("contains_secrets") is not False
        ):
            raise ValueError("audit event violates authority or secret boundary")
        payload = event.get("payload")
        payload_hash = event.get("payload_sha256")
        if hashlib.sha256(canonical_json_bytes(payload)).hexdigest() != payload_hash:
            raise ValueError("audit payload hash mismatch")
        content_view = dict(event)
        content_view.pop("event_content_sha256")
        if hashlib.sha256(canonical_json_bytes(content_view)).hexdigest() != content_hash:
            raise ValueError("audit event content hash mismatch")
        observed_at = now or datetime.now(UTC)
        with self._lock:
            try:
                existing = self._connection.execute(
                    "SELECT content_sha256 FROM audit_outbox WHERE event_id = ?", (event_id,)
                ).fetchone()
                if existing is not None:
                    if existing["content_sha256"] == content_hash:
                        return False
                    raise AuditBackpressureError("audit event id content conflict")
                metrics = self.metrics(now=observed_at)
                unconfirmed = metrics.pending + metrics.in_flight + metrics.dead_letter
                if unconfirmed >= self.limits.logical_capacity:
                    raise AuditBackpressureError("audit outbox logical capacity reached")
                if metrics.file_bytes >= self.limits.file_capacity_bytes:
                    raise AuditBackpressureError("audit outbox file capacity reached")
                if event_class == "ENTRY" and metrics.entry_backpressure:
                    raise AuditBackpressureError("audit outbox entry stop threshold reached")
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """
                    INSERT INTO audit_outbox (
                        event_id, producer_component, producer_instance_id,
                        producer_sequence, content_sha256, event_json, event_class,
                        state, created_at_utc, next_attempt_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        event_id,
                        producer["component"],
                        producer["instance_id"],
                        producer["sequence"],
                        content_hash,
                        event_bytes.decode("utf-8"),
                        event_class,
                        event["recorded_at_utc"],
                        event["recorded_at_utc"],
                    ),
                )
                self._connection.execute("COMMIT")
                return True
            except sqlite3.IntegrityError as error:
                self._connection.execute("ROLLBACK")
                existing = self._connection.execute(
                    "SELECT content_sha256 FROM audit_outbox WHERE event_id = ?", (event_id,)
                ).fetchone()
                if existing is not None and existing["content_sha256"] == content_hash:
                    return False
                raise AuditBackpressureError("audit event id content conflict") from error
            except AuditBackpressureError:
                raise
            except (OSError, sqlite3.Error, KeyError, TypeError) as error:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise AuditBackpressureError("audit outbox is not writable") from error

    def claim_batch(self, *, now: datetime, limit: int = 100) -> list[ClaimedEvent]:
        if not 1 <= limit <= 100:
            raise ValueError("writer batch limit must be in [1, 100]")
        now_text = _utc_text(now)
        lease_until = _utc_text(now + timedelta(seconds=self.limits.lease_seconds))
        with self._connection:
            # writer 崩溃后只回收 lease 已过期的事件，不触碰已交付事实。
            self._connection.execute(
                """
                UPDATE audit_outbox
                SET state = 'PENDING', lease_until_utc = NULL
                WHERE state = 'IN_FLIGHT' AND lease_until_utc <= ?
                """,
                (now_text,),
            )
            rows = self._connection.execute(
                """
                SELECT event_id, content_sha256, event_json, attempt_count
                FROM audit_outbox
                WHERE state = 'PENDING' AND next_attempt_at_utc <= ?
                ORDER BY producer_component, producer_sequence
                LIMIT ?
                """,
                (now_text, limit),
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in rows]
            self._connection.executemany(
                """
                UPDATE audit_outbox
                SET state = 'IN_FLIGHT', lease_until_utc = ?
                WHERE event_id = ? AND state = 'PENDING'
                """,
                ((lease_until, event_id) for event_id in event_ids),
            )
        return [
            ClaimedEvent(
                event_id=str(row["event_id"]),
                content_sha256=str(row["content_sha256"]),
                event_json=str(row["event_json"]),
                attempt_count=int(row["attempt_count"]),
            )
            for row in rows
        ]

    def mark_delivered(self, event_id: str, *, now: datetime) -> None:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE audit_outbox
                SET state = 'DELIVERED', delivered_at_utc = ?, lease_until_utc = NULL,
                    last_error_class = NULL
                WHERE event_id = ? AND state = 'IN_FLIGHT'
                """,
                (_utc_text(now), event_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("audit event is not owned by this writer lease")

    def mark_failed(
        self,
        event_id: str,
        *,
        now: datetime,
        error_class: str,
        retry_at: datetime,
        permanent: bool = False,
    ) -> None:
        row = self._connection.execute(
            "SELECT attempt_count FROM audit_outbox WHERE event_id = ? AND state = 'IN_FLIGHT'",
            (event_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("audit event is not owned by this writer lease")
        attempt = int(row["attempt_count"]) + 1
        state = "DEAD_LETTER" if permanent or attempt >= self.limits.maximum_attempts else "PENDING"
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO audit_delivery_attempt (
                    event_id, attempt_number, attempted_at_utc, error_class
                ) VALUES (?, ?, ?, ?)
                """,
                (event_id, attempt, _utc_text(now), error_class),
            )
            self._connection.execute(
                """
                UPDATE audit_outbox
                SET state = ?, attempt_count = ?, next_attempt_at_utc = ?,
                    lease_until_utc = NULL, last_error_class = ?
                WHERE event_id = ? AND state = 'IN_FLIGHT'
                """,
                (state, attempt, _utc_text(retry_at), error_class, event_id),
            )

    def cleanup_delivered(self, *, now: datetime, retention_days: int = 7) -> int:
        if retention_days < 7:
            raise ValueError("delivered audit retention must be at least 7 days")
        cutoff = _utc_text(now - timedelta(days=retention_days))
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM audit_outbox WHERE state = 'DELIVERED' AND delivered_at_utc < ?",
                (cutoff,),
            )
        if cursor.rowcount > 0:
            # 只由 sidecar 在清理事务后回收页；callback 从不执行 checkpoint 或 vacuum。
            self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._connection.execute("PRAGMA incremental_vacuum")
        return cursor.rowcount

    def state_for_test(self, event_id: str) -> dict[str, object]:
        """返回单条本地状态，供确定性单元/故障测试断言。"""

        row = self._connection.execute(
            "SELECT * FROM audit_outbox WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return dict(zip(row.keys(), row, strict=True))

    def attempts_for_test(self, event_id: str) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT attempt_number, attempted_at_utc, error_class
            FROM audit_delivery_attempt WHERE event_id = ? ORDER BY attempt_number
            """,
            (event_id,),
        ).fetchall()
        return [dict(zip(row.keys(), row, strict=True)) for row in rows]
