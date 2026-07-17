"""Audit Writer：从 outbox 异步、幂等地写入独立 Research/Audit DB。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from alphamind.audit.outbox import AuditOutbox, ClaimedEvent


class DeliveryResult(StrEnum):
    INSERTED = "inserted"
    DUPLICATE_SAME_CONTENT = "duplicate_same_content"
    CONTENT_CONFLICT = "content_conflict"


class SQLiteAuditSink:
    """只拥有 Audit DB schema；接口没有 Runtime DB 或 Trade/Order 写入口。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_event (
                    event_id TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    persisted_at_utc TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        self._connection.close()

    def deliver(self, event: ClaimedEvent, *, persisted_at_utc: str) -> DeliveryResult:
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO audit_event (
                    event_id, content_sha256, event_json, persisted_at_utc
                ) VALUES (?, ?, ?, ?)
                """,
                (event.event_id, event.content_sha256, event.event_json, persisted_at_utc),
            )
            if cursor.rowcount == 1:
                return DeliveryResult.INSERTED
            existing = self._connection.execute(
                "SELECT content_sha256 FROM audit_event WHERE event_id = ?", (event.event_id,)
            ).fetchone()
        if existing is not None and existing["content_sha256"] == event.content_sha256:
            return DeliveryResult.DUPLICATE_SAME_CONTENT
        return DeliveryResult.CONTENT_CONFLICT

    def count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0])


@dataclass(frozen=True, slots=True)
class WriterRunResult:
    claimed: int
    delivered: int
    retried: int
    dead_lettered: int
    cleaned: int


class AuditWriter:
    def __init__(self, outbox: AuditOutbox, sink: SQLiteAuditSink) -> None:
        self.outbox = outbox
        self.sink = sink

    @staticmethod
    def _retry_delay(event_id: str, attempt: int) -> timedelta:
        # 稳定 hash 提供可复测 jitter，范围为指数退避的 75% 至 125%。
        digest = hashlib.sha256(f"{event_id}:{attempt}".encode()).digest()
        jitter = 0.75 + int.from_bytes(digest[:2], "big") / 65_535 * 0.5
        seconds = min(60.0, 2 ** max(0, attempt - 1)) * jitter
        return timedelta(seconds=min(60.0, seconds))

    def run_once(self, *, now: datetime, batch_size: int = 100) -> WriterRunResult:
        claimed = self.outbox.claim_batch(now=now, limit=batch_size)
        delivered = 0
        retried = 0
        dead_lettered = 0
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("writer timestamp must be timezone-aware")
        persisted_at = now.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        for event in claimed:
            try:
                result = self.sink.deliver(event, persisted_at_utc=persisted_at)
                if result is DeliveryResult.CONTENT_CONFLICT:
                    self.outbox.mark_failed(
                        event.event_id,
                        now=now,
                        error_class="content_hash_conflict",
                        retry_at=now,
                        permanent=True,
                    )
                    dead_lettered += 1
                    continue
                self.outbox.mark_delivered(event.event_id, now=now)
                delivered += 1
            except (OSError, sqlite3.Error) as error:
                attempt = event.attempt_count + 1
                permanent = attempt >= self.outbox.limits.maximum_attempts
                self.outbox.mark_failed(
                    event.event_id,
                    now=now,
                    error_class=type(error).__name__.lower(),
                    retry_at=now + self._retry_delay(event.event_id, attempt),
                    permanent=permanent,
                )
                if permanent:
                    dead_lettered += 1
                else:
                    retried += 1
        cleaned = self.outbox.cleanup_delivered(now=now)
        return WriterRunResult(len(claimed), delivered, retried, dead_lettered, cleaned)
