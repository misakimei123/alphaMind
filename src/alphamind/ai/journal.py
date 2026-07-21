"""AI 决策结果的 append-only SQLite journal。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
MAX_RECORD_BYTES = 64 * 1024


class DecisionJournalError(RuntimeError):
    """决策记录无法可靠追加或读取。"""


class DecisionOutcome(StrEnum):
    HOLD = "HOLD"
    CANDIDATE_ACTIONS = "CANDIDATE_ACTIONS"
    MODEL_ERROR = "MODEL_ERROR"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("decision journal timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class DecisionJournalEntry:
    """写入前的安全、完整决策事实；不包含 Prompt、API key 或异常消息。"""

    cycle_id: str
    recorded_at_utc: datetime
    outcome: DecisionOutcome
    environment: str
    profile_id: str
    model_id: str
    prompt_id: str
    prompt_version: int
    prompt_sha256: str
    config_sha256: str
    input_sha256: str
    schema_versions: dict[str, int]
    decision_sha256: str | None
    decision: JsonObject | None
    error_code: str | None
    response_id: str | None
    request_id: str | None
    validation: JsonObject | None
    usage: JsonObject

    def to_document(self) -> JsonObject:
        return {
            "record_schema_version": 1,
            "cycle_id": self.cycle_id,
            "recorded_at_utc": _utc_text(self.recorded_at_utc),
            "outcome": self.outcome.value,
            "environment": self.environment,
            "model": {"profile_id": self.profile_id, "model_id": self.model_id},
            "prompt": {
                "id": self.prompt_id,
                "version": self.prompt_version,
                "sha256": self.prompt_sha256,
            },
            "config_sha256": self.config_sha256,
            "input_sha256": self.input_sha256,
            "schema_versions": dict(sorted(self.schema_versions.items())),
            "decision_sha256": self.decision_sha256,
            "decision": self.decision,
            "error_code": self.error_code,
            "provider_ids": {
                "response_id": self.response_id,
                "request_id": self.request_id,
            },
            "validation": self.validation,
            "usage": self.usage,
            # Journal 是 AI 输出事实源，不声明订单、成交或 Freqtrade Runtime DB 事实。
            "runtime_authority": False,
            "contains_secrets": False,
        }


@dataclass(frozen=True, slots=True)
class StoredDecisionRecord:
    cycle_id: str
    outcome: DecisionOutcome
    recorded_at_utc: datetime
    input_sha256: str
    record_sha256: str
    document: JsonObject


class DecisionJournal:
    """每个 cycle 只允许一个不可变终态，供 R3 读取合法候选动作。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
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
            raise DecisionJournalError("decision journal could not be initialized") from error

    def _initialize(self) -> None:
        journal_mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise DecisionJournalError("decision journal requires SQLite WAL mode")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_decision_journal (
                cycle_id TEXT PRIMARY KEY,
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('HOLD', 'CANDIDATE_ACTIONS', 'MODEL_ERROR')
                ),
                recorded_at_utc TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                record_sha256 TEXT NOT NULL,
                record_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ai_decision_journal_recorded_at
            ON ai_decision_journal(recorded_at_utc DESC)
            """
        )

    def close(self) -> None:
        self._connection.close()

    def append(self, entry: DecisionJournalEntry) -> bool:
        """原子追加；相同 cycle+内容幂等，任何同 cycle 异文均拒绝覆盖。"""

        document = entry.to_document()
        self._validate_document(document)
        record_json = _canonical_json(document)
        if len(record_json.encode("utf-8")) > MAX_RECORD_BYTES:
            raise DecisionJournalError("decision journal record exceeds 64 KiB")
        record_sha256 = _sha256(record_json)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT record_sha256 FROM ai_decision_journal WHERE cycle_id = ?",
                    (entry.cycle_id,),
                ).fetchone()
                if existing is not None:
                    self._connection.execute("ROLLBACK")
                    if str(existing["record_sha256"]) == record_sha256:
                        return False
                    raise DecisionJournalError("decision cycle content conflict")
                self._connection.execute(
                    """
                    INSERT INTO ai_decision_journal (
                        cycle_id, outcome, recorded_at_utc, input_sha256,
                        record_sha256, record_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.cycle_id,
                        entry.outcome.value,
                        document["recorded_at_utc"],
                        entry.input_sha256,
                        record_sha256,
                        record_json,
                    ),
                )
                self._connection.execute("COMMIT")
                return True
            except DecisionJournalError:
                raise
            except (OSError, sqlite3.Error) as error:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise DecisionJournalError("decision journal is not writable") from error

    def get(self, cycle_id: str) -> StoredDecisionRecord | None:
        try:
            row = self._connection.execute(
                "SELECT * FROM ai_decision_journal WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
        except sqlite3.Error as error:
            raise DecisionJournalError("decision journal is not readable") from error
        if row is None:
            return None
        return self._stored_record(row)

    def recent(self, limit: int = 20) -> tuple[StoredDecisionRecord, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("decision journal limit must be in [1, 100]")
        try:
            rows = self._connection.execute(
                """
                SELECT * FROM ai_decision_journal
                ORDER BY recorded_at_utc DESC, cycle_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.Error as error:
            raise DecisionJournalError("decision journal is not readable") from error
        return tuple(self._stored_record(row) for row in rows)

    @staticmethod
    def _stored_record(row: sqlite3.Row) -> StoredDecisionRecord:
        record_json = str(row["record_json"])
        expected_hash = str(row["record_sha256"])
        if _sha256(record_json) != expected_hash:
            raise DecisionJournalError("decision journal content hash mismatch")
        try:
            document = json.loads(record_json)
        except json.JSONDecodeError as error:
            raise DecisionJournalError("decision journal content is invalid") from error
        if not isinstance(document, dict):
            raise DecisionJournalError("decision journal content is invalid")
        DecisionJournal._validate_document(document)
        if (
            document["cycle_id"] != row["cycle_id"]
            or document["outcome"] != row["outcome"]
            or document["recorded_at_utc"] != row["recorded_at_utc"]
            or document["input_sha256"] != row["input_sha256"]
        ):
            raise DecisionJournalError("decision journal indexed fields mismatch")
        return StoredDecisionRecord(
            cycle_id=str(row["cycle_id"]),
            outcome=DecisionOutcome(str(row["outcome"])),
            recorded_at_utc=datetime.fromisoformat(
                str(row["recorded_at_utc"]).replace("Z", "+00:00")
            ),
            input_sha256=str(row["input_sha256"]),
            record_sha256=expected_hash,
            document=document,
        )

    @staticmethod
    def _validate_document(document: JsonObject) -> None:
        if document.get("record_schema_version") != 1:
            raise DecisionJournalError("decision journal schema version is unsupported")
        if document.get("runtime_authority") is not False:
            raise DecisionJournalError("decision journal cannot claim runtime authority")
        if document.get("contains_secrets") is not False:
            raise DecisionJournalError("decision journal cannot contain secrets")
        outcome = document.get("outcome")
        decision = document.get("decision")
        decision_sha256 = document.get("decision_sha256")
        error_code = document.get("error_code")
        for field in ("prompt", "config_sha256", "input_sha256", "schema_versions"):
            if field not in document:
                raise DecisionJournalError("decision journal version binding is incomplete")
        prompt = document["prompt"]
        if not isinstance(prompt, dict):
            raise DecisionJournalError("decision journal prompt binding is invalid")
        hashes = (
            document["config_sha256"],
            document["input_sha256"],
            prompt.get("sha256"),
        )
        if any(
            not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None
            for value in hashes
        ):
            raise DecisionJournalError("decision journal hash binding is invalid")
        schema_versions = document["schema_versions"]
        if (
            not isinstance(schema_versions, dict)
            or not schema_versions
            or any(
                not isinstance(name, str) or not isinstance(version, int) or version < 1
                for name, version in schema_versions.items()
            )
        ):
            raise DecisionJournalError("decision journal schema binding is invalid")
        if outcome in {DecisionOutcome.HOLD.value, DecisionOutcome.CANDIDATE_ACTIONS.value}:
            if (
                not isinstance(decision, dict)
                or error_code is not None
                or not isinstance(decision_sha256, str)
                or SHA256_PATTERN.fullmatch(decision_sha256) is None
                or _sha256(_canonical_json(decision)) != decision_sha256
            ):
                raise DecisionJournalError("successful decision journal record is incomplete")
            actions = decision.get("actions")
            if not isinstance(actions, list) or not actions:
                raise DecisionJournalError("successful decision journal actions are missing")
            actionable = any(
                isinstance(action, dict) and action.get("action") != "HOLD" for action in actions
            )
            if actionable != (outcome == DecisionOutcome.CANDIDATE_ACTIONS.value):
                raise DecisionJournalError("decision journal outcome does not match its actions")
        elif outcome == DecisionOutcome.MODEL_ERROR.value:
            if (
                decision is not None
                or decision_sha256 is not None
                or not isinstance(error_code, str)
                or ERROR_CODE_PATTERN.fullmatch(error_code) is None
            ):
                raise DecisionJournalError("model error journal record is incomplete")
        else:
            raise DecisionJournalError("decision journal outcome is invalid")
