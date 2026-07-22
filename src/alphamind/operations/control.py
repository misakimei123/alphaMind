"""R3-06 持久化运行控制面：暂停 AI、停止新开仓与紧急模式。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
MAX_IDEMPOTENCY_KEY_LENGTH = 200


class OperationalControlError(RuntimeError):
    """运行控制状态无法可信读取或迁移时 fail-closed。"""


class OperationalControlAction(StrEnum):
    PAUSE_AI = "PAUSE_AI"
    RESUME_AI = "RESUME_AI"
    STOP_ENTRIES = "STOP_ENTRIES"
    RESUME_ENTRIES = "RESUME_ENTRIES"
    EMERGENCY = "EMERGENCY"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("operational control timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class OperationalControlSnapshot:
    """当前控制投影；安全退出在任何状态下都必须保持可用。"""

    revision: int = 0
    ai_paused: bool = False
    entry_stopped: bool = False
    emergency: bool = False
    manual_review_required: bool = False
    cancel_pending_entries: bool = False
    safe_exit_allowed: bool = True
    updated_at_utc: str | None = None

    @property
    def reason_codes(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.emergency:
            reasons.append("EMERGENCY_MODE")
        if self.ai_paused:
            reasons.append("AI_PAUSED")
        if self.entry_stopped:
            reasons.append("ENTRY_STOPPED")
        return tuple(reasons or ["NORMAL_OPERATION"])

    def __post_init__(self) -> None:
        if self.revision < 0 or not self.safe_exit_allowed:
            raise ValueError("operational control snapshot is unsafe")
        if self.emergency and not all(
            (
                self.ai_paused,
                self.entry_stopped,
                self.manual_review_required,
                self.cancel_pending_entries,
            )
        ):
            raise ValueError("emergency controls must fail closed")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "revision": self.revision,
            "ai_paused": self.ai_paused,
            "entry_stopped": self.entry_stopped,
            "emergency": self.emergency,
            "manual_review_required": self.manual_review_required,
            "cancel_pending_entries": self.cancel_pending_entries,
            "safe_exit_allowed": self.safe_exit_allowed,
            "reason_codes": list(self.reason_codes),
            "updated_at_utc": self.updated_at_utc,
        }

    @classmethod
    def from_document(cls, value: dict[str, Any]) -> OperationalControlSnapshot:
        if value.get("schema_version") != 1:
            raise ValueError("operational control schema version is invalid")
        snapshot = cls(
            revision=int(value["revision"]),
            ai_paused=value["ai_paused"],
            entry_stopped=value["entry_stopped"],
            emergency=value["emergency"],
            manual_review_required=value["manual_review_required"],
            cancel_pending_entries=value["cancel_pending_entries"],
            safe_exit_allowed=value["safe_exit_allowed"],
            updated_at_utc=value["updated_at_utc"],
        )
        if value.get("reason_codes") != list(snapshot.reason_codes):
            raise ValueError("operational control reason codes are invalid")
        return snapshot


class OperationalControlStore:
    """以不可变事件重放控制状态；损坏、冲突与越权恢复全部 fail-closed。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._initialize()
        except (OSError, sqlite3.Error) as error:
            raise OperationalControlError(
                "operational control store could not be initialized"
            ) from error

    def _initialize(self) -> None:
        mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise OperationalControlError("operational control store requires SQLite WAL mode")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operational_control_event (
                event_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL UNIQUE CHECK (sequence >= 1),
                idempotency_key TEXT NOT NULL UNIQUE,
                event_sha256 TEXT NOT NULL,
                event_json TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                snapshot_json TEXT NOT NULL
            )
            """
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> OperationalControlStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def current(self) -> OperationalControlSnapshot:
        with self._lock:
            try:
                rows = self._connection.execute(
                    "SELECT * FROM operational_control_event ORDER BY sequence"
                ).fetchall()
                return self._verify_and_replay(rows)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
                raise OperationalControlError("operational control history is invalid") from error

    def apply(
        self,
        action: OperationalControlAction,
        *,
        occurred_at_utc: datetime,
        actor_user_id_sha256: str,
        actor_chat_id_sha256: str,
        idempotency_key: str,
        risk_entry_allowed: bool | None = None,
    ) -> OperationalControlSnapshot:
        """先提交控制事实再返回；同一幂等键只允许完全相同的请求。"""

        if not isinstance(action, OperationalControlAction):
            raise ValueError("operational control action is invalid")
        if any(
            SHA256_PATTERN.fullmatch(value) is None
            for value in (actor_user_id_sha256, actor_chat_id_sha256)
        ):
            raise ValueError("operational control actor hash is invalid")
        if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError("operational control idempotency key is invalid")
        occurred = _utc_text(occurred_at_utc)
        request = {
            "schema_version": 1,
            "action": action.value,
            "occurred_at_utc": occurred,
            "idempotency_key": idempotency_key,
            "actor": {
                "user_id_sha256": actor_user_id_sha256,
                "chat_id_sha256": actor_chat_id_sha256,
            },
            "risk_entry_allowed": risk_entry_allowed,
        }
        request_json = _canonical_json(request)
        event_sha256 = _sha256(request_json)

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                rows = self._connection.execute(
                    "SELECT * FROM operational_control_event ORDER BY sequence"
                ).fetchall()
                current = self._verify_and_replay(rows)
                existing = self._connection.execute(
                    "SELECT event_sha256, snapshot_json FROM operational_control_event "
                    "WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["event_sha256"] != event_sha256:
                        raise OperationalControlError(
                            "operational control idempotency key was reused"
                        )
                    snapshot = OperationalControlSnapshot.from_document(
                        json.loads(existing["snapshot_json"])
                    )
                    self._connection.execute("COMMIT")
                    return snapshot
                updated = self._transition(
                    current,
                    action,
                    occurred_at_utc=occurred,
                    risk_entry_allowed=risk_entry_allowed,
                )
                snapshot_json = _canonical_json(updated.to_safe_dict())
                event_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"alphamind:operations:{idempotency_key}")
                )
                self._connection.execute(
                    "INSERT INTO operational_control_event VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        updated.revision,
                        idempotency_key,
                        event_sha256,
                        request_json,
                        _sha256(snapshot_json),
                        snapshot_json,
                    ),
                )
                self._connection.execute("COMMIT")
                return updated
            except OperationalControlError:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise OperationalControlError("operational control transition failed") from error

    @classmethod
    def _verify_and_replay(cls, rows: list[sqlite3.Row]) -> OperationalControlSnapshot:
        current = OperationalControlSnapshot()
        for expected_sequence, row in enumerate(rows, start=1):
            event_json = str(row["event_json"])
            snapshot_json = str(row["snapshot_json"])
            if (
                row["sequence"] != expected_sequence
                or row["event_sha256"] != _sha256(event_json)
                or row["snapshot_sha256"] != _sha256(snapshot_json)
            ):
                raise ValueError("operational control event chain is invalid")
            event = json.loads(event_json)
            idempotency_key = event["idempotency_key"]
            expected_event_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"alphamind:operations:{idempotency_key}")
            )
            if row["idempotency_key"] != idempotency_key or row["event_id"] != expected_event_id:
                raise ValueError("operational control event identity is invalid")
            replayed = cls._transition(
                current,
                OperationalControlAction(event["action"]),
                occurred_at_utc=event["occurred_at_utc"],
                risk_entry_allowed=event["risk_entry_allowed"],
            )
            stored = OperationalControlSnapshot.from_document(json.loads(snapshot_json))
            if replayed != stored:
                raise ValueError("operational control projection does not match history")
            current = stored
        return current

    @staticmethod
    def _transition(
        current: OperationalControlSnapshot,
        action: OperationalControlAction,
        *,
        occurred_at_utc: str,
        risk_entry_allowed: bool | None,
    ) -> OperationalControlSnapshot:
        ai_paused = current.ai_paused
        entry_stopped = current.entry_stopped
        emergency = current.emergency
        manual_review_required = current.manual_review_required
        cancel_pending_entries = current.cancel_pending_entries
        if action is OperationalControlAction.PAUSE_AI:
            ai_paused = True
        elif action is OperationalControlAction.RESUME_AI:
            if current.emergency:
                raise OperationalControlError("emergency mode requires manual review")
            ai_paused = False
        elif action is OperationalControlAction.STOP_ENTRIES:
            entry_stopped = True
        elif action is OperationalControlAction.RESUME_ENTRIES:
            if current.emergency:
                raise OperationalControlError("emergency mode requires manual review")
            if risk_entry_allowed is not True:
                raise OperationalControlError(
                    "entry resume requires an entry-allowed risk snapshot"
                )
            entry_stopped = False
        elif action is OperationalControlAction.EMERGENCY:
            ai_paused = True
            entry_stopped = True
            emergency = True
            manual_review_required = True
            cancel_pending_entries = True
        return OperationalControlSnapshot(
            revision=current.revision + 1,
            ai_paused=ai_paused,
            entry_stopped=entry_stopped,
            emergency=emergency,
            manual_review_required=manual_review_required,
            cancel_pending_entries=cancel_pending_entries,
            safe_exit_allowed=True,
            updated_at_utc=occurred_at_utc,
        )
