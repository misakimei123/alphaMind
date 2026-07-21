"""R3 Proposal Store：候选 Action、人工授权与执行前重新校验状态机。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from referencing import Registry, Resource

from alphamind.ai.journal import DecisionOutcome, StoredDecisionRecord
from alphamind.config import EffectiveConfig

JsonObject = dict[str, Any]
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
ACTION_ID_PATTERN = re.compile(r"^act-([0-9]{8}T[0-9]{6}Z-[a-f0-9]{12})$")
MAX_EVENTS = 50


class ProposalStoreError(RuntimeError):
    """Proposal 无法安全创建、迁移或读取。"""


class ProposalState(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVALIDATING = "REVALIDATING"
    QUEUED = "QUEUED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ProposalAuthorization:
    """R3-03 将从运行环境生成这些不可逆 hash；R3-01 不接触原始 ID/nonce。"""

    nonce_sha256: str
    allowed_user_id_sha256: tuple[str, ...]
    allowed_chat_id_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        if SHA256_PATTERN.fullmatch(self.nonce_sha256) is None:
            raise ValueError("proposal nonce hash is invalid")
        for label, values in (
            ("user", self.allowed_user_id_sha256),
            ("chat", self.allowed_chat_id_sha256),
        ):
            if not values or len(values) > 20 or len(values) != len(set(values)):
                raise ValueError(f"proposal allowed {label} hashes are invalid")
            if any(SHA256_PATTERN.fullmatch(value) is None for value in values):
                raise ValueError(f"proposal allowed {label} hash is invalid")

    def to_document(self) -> JsonObject:
        return {
            "allowed_user_id_sha256": sorted(self.allowed_user_id_sha256),
            "allowed_chat_id_sha256": sorted(self.allowed_chat_id_sha256),
            "decided_by": None,
        }


@dataclass(frozen=True, slots=True)
class StoredProposal:
    proposal_id: str
    cycle_id: str
    action_id: str
    source_record_sha256: str
    state: ProposalState
    record_sha256: str
    _document_json: str

    @property
    def document(self) -> JsonObject:
        value = json.loads(self._document_json)
        assert isinstance(value, dict)
        return value


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
        raise ValueError("proposal timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _proposal_id(action_id: str) -> str:
    matched = ACTION_ID_PATTERN.fullmatch(action_id)
    if matched is None:
        raise ProposalStoreError("candidate action id is invalid")
    return f"proposal-{matched.group(1)}"


def _event_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"alphamind:approval:{idempotency_key}"))


class ProposalStore:
    """以不可变事件为依据维护 Proposal 当前投影，不拥有订单或成交事实。"""

    def __init__(self, effective: EffectiveConfig, path: str | Path | None = None) -> None:
        self.effective = effective
        configured = Path(str(effective.runtime["approval"]["store_path"]))
        selected = configured if path is None else Path(path)
        self.path = selected if selected.is_absolute() else effective.project_root / selected
        self.path = self.path.resolve()
        if path is None:
            try:
                self.path.relative_to(effective.project_root)
            except ValueError:
                raise ProposalStoreError(
                    "proposal store path must stay inside project root"
                ) from None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.approval_ttl = timedelta(minutes=int(effective.runtime["approval"]["ttl_minutes"]))
        self._validator = self._load_validator(effective.project_root)
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
            raise ProposalStoreError("proposal store could not be initialized") from error

    @staticmethod
    def _load_validator(project_root: Path) -> jsonschema.Draft202012Validator:
        schema_root = project_root / "data" / "schemas"
        names = (
            "trade-action.schema.yaml",
            "approval-event.schema.yaml",
            "approval-record.schema.yaml",
        )
        schemas: dict[str, JsonObject] = {}
        resources: list[tuple[str, Resource[JsonObject]]] = []
        try:
            for name in names:
                raw = yaml.safe_load((schema_root / name).read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or not isinstance(raw.get("$id"), str):
                    raise ProposalStoreError("approval schema is invalid")
                schemas[name] = raw
                resources.append((str(raw["$id"]), Resource.from_contents(raw)))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise ProposalStoreError("approval schema could not be loaded") from error
        registry = Registry().with_resources(resources)
        return jsonschema.Draft202012Validator(
            schemas["approval-record.schema.yaml"],
            registry=registry,
            format_checker=jsonschema.FormatChecker(),
        )

    def _initialize(self) -> None:
        journal_mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise ProposalStoreError("proposal store requires SQLite WAL mode")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        existing = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'proposal'"
        ).fetchone()
        if existing is not None and "REVALIDATING" not in str(existing["sql"]):
            self._migrate_r3_01_store()
        self._connection.executescript(self._schema_sql())

    @staticmethod
    def _schema_sql() -> str:
        return """
            CREATE TABLE IF NOT EXISTS proposal (
                proposal_id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL,
                action_id TEXT NOT NULL UNIQUE,
                source_record_sha256 TEXT NOT NULL,
                action_sha256 TEXT NOT NULL,
                action_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'DRAFT', 'VALIDATED', 'PENDING_APPROVAL',
                        'APPROVED', 'REJECTED', 'EXPIRED',
                        'REVALIDATING', 'QUEUED', 'CANCELLED'
                    )
                ),
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                nonce_sha256 TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                authorization_json TEXT NOT NULL,
                execution_json TEXT,
                record_sha256 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS proposal_state_expiry
            ON proposal(state, expires_at_utc, created_at_utc);
            CREATE TABLE IF NOT EXISTS proposal_event (
                event_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                idempotency_key TEXT NOT NULL UNIQUE,
                event_sha256 TEXT NOT NULL,
                event_json TEXT NOT NULL,
                UNIQUE(proposal_id, sequence),
                FOREIGN KEY (proposal_id) REFERENCES proposal(proposal_id)
            );
            """

    def _migrate_r3_01_store(self) -> None:
        """保留既有审批事实，并扩展 SQLite CHECK 到 R3-04 状态。"""

        self._connection.execute("PRAGMA foreign_keys=OFF")
        try:
            # executescript 会在脚本开始前提交；因此 BEGIN/COMMIT 必须和迁移语句位于同一脚本。
            self._connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                ALTER TABLE proposal_event RENAME TO proposal_event_r3_01;
                ALTER TABLE proposal RENAME TO proposal_r3_01;
                {self._schema_sql()}
                INSERT INTO proposal SELECT * FROM proposal_r3_01;
                INSERT INTO proposal_event SELECT * FROM proposal_event_r3_01;
                DROP TABLE proposal_event_r3_01;
                DROP TABLE proposal_r3_01;
                COMMIT;
                """
            )
        except sqlite3.Error:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        finally:
            self._connection.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._connection.close()

    def ingest_decision(
        self,
        decision_record: StoredDecisionRecord,
        authorizations: Mapping[str, ProposalAuthorization],
        *,
        now_utc: datetime,
    ) -> tuple[StoredProposal, ...]:
        """把 Journal 中的合法非 HOLD Action 原子投影成 VALIDATED Proposal。"""

        now_text = _utc_text(now_utc)
        self._validate_source_record(decision_record)
        decision = decision_record.document["decision"]
        assert isinstance(decision, dict)
        actions = decision["actions"]
        assert isinstance(actions, list)
        candidates = [
            action
            for action in actions
            if isinstance(action, dict) and action.get("action") != "HOLD"
        ]
        action_ids = {str(action["action_id"]) for action in candidates}
        if action_ids != set(authorizations):
            raise ProposalStoreError("proposal authorization set does not match candidate actions")
        if now_utc < decision_record.recorded_at_utc:
            raise ProposalStoreError("proposal creation precedes its decision record")

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                stored: list[StoredProposal] = []
                for action in candidates:
                    action_id = str(action["action_id"])
                    authorization = authorizations[action_id]
                    proposal_id = _proposal_id(action_id)
                    existing = self._connection.execute(
                        "SELECT * FROM proposal WHERE proposal_id = ?", (proposal_id,)
                    ).fetchone()
                    if existing is not None:
                        self._verify_existing_ingest(
                            existing,
                            decision_record=decision_record,
                            action=action,
                            authorization=authorization,
                        )
                        stored.append(self._stored_proposal(existing))
                        continue
                    action_valid_until = decision_record.recorded_at_utc + timedelta(
                        seconds=int(action["valid_for_seconds"])
                    )
                    expires_at = min(now_utc + self.approval_ttl, action_valid_until)
                    if expires_at <= now_utc:
                        raise ProposalStoreError(
                            "candidate action expired before proposal creation"
                        )
                    stored.append(
                        self._insert_proposal(
                            proposal_id=proposal_id,
                            decision_record=decision_record,
                            action=action,
                            authorization=authorization,
                            created_at=now_text,
                            expires_at=_utc_text(expires_at),
                        )
                    )
                self._connection.execute("COMMIT")
                return tuple(stored)
            except ProposalStoreError:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise ProposalStoreError("proposal ingestion failed") from error

    def _insert_proposal(
        self,
        *,
        proposal_id: str,
        decision_record: StoredDecisionRecord,
        action: JsonObject,
        authorization: ProposalAuthorization,
        created_at: str,
        expires_at: str,
    ) -> StoredProposal:
        action_json = _canonical_json(action)
        action_sha256 = _sha256(action_json)
        action_id = str(action["action_id"])
        base_key = f"proposal:{proposal_id}"
        events = [
            self._event(
                proposal_id=proposal_id,
                action_id=action_id,
                event_type="CREATED",
                from_state=None,
                to_state=ProposalState.DRAFT,
                occurred_at=created_at,
                actor_type="system",
                idempotency_key=f"{base_key}:create",
                reason_codes=("ACTION_RECEIVED",),
            ),
            self._event(
                proposal_id=proposal_id,
                action_id=action_id,
                event_type="VALIDATION_PASSED",
                from_state=ProposalState.DRAFT,
                to_state=ProposalState.VALIDATED,
                occurred_at=created_at,
                actor_type="system",
                idempotency_key=f"{base_key}:validate",
                reason_codes=("DETERMINISTIC_VALIDATION_PASSED",),
            ),
        ]
        document = {
            "schema_version": 1,
            "proposal_id": proposal_id,
            "cycle_id": decision_record.cycle_id,
            "action": action,
            "action_sha256": action_sha256,
            "state": ProposalState.VALIDATED.value,
            "created_at_utc": created_at,
            "updated_at_utc": created_at,
            "expires_at_utc": expires_at,
            "nonce_sha256": authorization.nonce_sha256,
            "idempotency_key": base_key,
            "authorization": authorization.to_document(),
            "events": events,
            "execution": None,
        }
        self._validate_record(document)
        record_sha256 = _sha256(_canonical_json(document))
        self._connection.execute(
            """
            INSERT INTO proposal (
                proposal_id, cycle_id, action_id, source_record_sha256,
                action_sha256, action_json, state, created_at_utc, updated_at_utc,
                expires_at_utc, nonce_sha256, idempotency_key, authorization_json,
                execution_json, record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, 'VALIDATED', ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                proposal_id,
                decision_record.cycle_id,
                action_id,
                decision_record.record_sha256,
                action_sha256,
                action_json,
                created_at,
                created_at,
                expires_at,
                authorization.nonce_sha256,
                base_key,
                _canonical_json(authorization.to_document()),
                record_sha256,
            ),
        )
        for sequence, event in enumerate(events):
            event_json = _canonical_json(event)
            self._connection.execute(
                """
                INSERT INTO proposal_event (
                    event_id, proposal_id, sequence, idempotency_key,
                    event_sha256, event_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    proposal_id,
                    sequence,
                    event["idempotency_key"],
                    _sha256(event_json),
                    event_json,
                ),
            )
        row = self._connection.execute(
            "SELECT * FROM proposal WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        assert row is not None
        return self._stored_proposal(row)

    def request_approval(
        self,
        proposal_id: str,
        *,
        occurred_at_utc: datetime,
        idempotency_key: str,
    ) -> StoredProposal:
        return self._transition(
            proposal_id,
            event_type="APPROVAL_REQUESTED",
            expected_state=ProposalState.VALIDATED,
            to_state=ProposalState.PENDING_APPROVAL,
            occurred_at_utc=occurred_at_utc,
            actor_type="system",
            user_id_sha256=None,
            chat_id_sha256=None,
            nonce_sha256=None,
            idempotency_key=idempotency_key,
            reason_codes=("TELEGRAM_REQUEST_SENT",),
        )

    def decide(
        self,
        proposal_id: str,
        *,
        approved: bool,
        occurred_at_utc: datetime,
        user_id_sha256: str,
        chat_id_sha256: str,
        nonce_sha256: str,
        idempotency_key: str,
    ) -> StoredProposal:
        return self._transition(
            proposal_id,
            event_type="USER_APPROVED" if approved else "USER_REJECTED",
            expected_state=ProposalState.PENDING_APPROVAL,
            to_state=ProposalState.APPROVED if approved else ProposalState.REJECTED,
            occurred_at_utc=occurred_at_utc,
            actor_type="telegram_user",
            user_id_sha256=user_id_sha256,
            chat_id_sha256=chat_id_sha256,
            nonce_sha256=nonce_sha256,
            idempotency_key=idempotency_key,
            reason_codes=("USER_CONFIRMED" if approved else "USER_DECLINED",),
        )

    def expire(
        self,
        proposal_id: str,
        *,
        occurred_at_utc: datetime,
        idempotency_key: str,
    ) -> StoredProposal:
        return self._transition(
            proposal_id,
            event_type="APPROVAL_EXPIRED",
            expected_state=ProposalState.PENDING_APPROVAL,
            to_state=ProposalState.EXPIRED,
            occurred_at_utc=occurred_at_utc,
            actor_type="system",
            user_id_sha256=None,
            chat_id_sha256=None,
            nonce_sha256=None,
            idempotency_key=idempotency_key,
            reason_codes=("APPROVAL_TTL_EXPIRED",),
        )

    def start_revalidation(
        self,
        proposal_id: str,
        *,
        occurred_at_utc: datetime,
        idempotency_key: str,
    ) -> StoredProposal:
        """把已授权 Action 交给 R3-04 重新校验，但尚不产生执行权。"""

        return self._transition(
            proposal_id,
            event_type="REVALIDATION_STARTED",
            expected_state=ProposalState.APPROVED,
            to_state=ProposalState.REVALIDATING,
            occurred_at_utc=occurred_at_utc,
            actor_type="system",
            user_id_sha256=None,
            chat_id_sha256=None,
            nonce_sha256=None,
            idempotency_key=idempotency_key,
            reason_codes=("EXECUTION_REVALIDATION_REQUIRED",),
        )

    def finish_revalidation(
        self,
        proposal_id: str,
        *,
        passed: bool,
        expired: bool,
        occurred_at_utc: datetime,
        idempotency_key: str,
        reason_codes: tuple[str, ...],
        execution: Mapping[str, Any] | None = None,
    ) -> StoredProposal:
        """持久化重新校验终态；只有通过时才绑定一次执行队列详情。"""

        if passed and expired:
            raise ValueError("passed revalidation cannot be expired")
        if (
            not reason_codes
            or len(reason_codes) > 16
            or len(reason_codes) != len(set(reason_codes))
        ):
            raise ValueError("revalidation reason codes are invalid")
        if passed:
            event_type = "EXECUTION_QUEUED"
            to_state = ProposalState.QUEUED
            execution_document = dict(execution) if execution is not None else None
            if execution_document is None:
                raise ValueError("passed revalidation requires execution evidence")
        else:
            event_type = "APPROVAL_EXPIRED" if expired else "EXECUTION_CANCELLED"
            to_state = ProposalState.EXPIRED if expired else ProposalState.CANCELLED
            execution_document = None
            if execution is not None:
                raise ValueError("rejected revalidation cannot bind execution evidence")
        return self._transition(
            proposal_id,
            event_type=event_type,
            expected_state=ProposalState.REVALIDATING,
            to_state=to_state,
            occurred_at_utc=occurred_at_utc,
            actor_type="system",
            user_id_sha256=None,
            chat_id_sha256=None,
            nonce_sha256=None,
            idempotency_key=idempotency_key,
            reason_codes=reason_codes,
            execution=execution_document,
        )

    def _transition(
        self,
        proposal_id: str,
        *,
        event_type: str,
        expected_state: ProposalState,
        to_state: ProposalState,
        occurred_at_utc: datetime,
        actor_type: str,
        user_id_sha256: str | None,
        chat_id_sha256: str | None,
        nonce_sha256: str | None,
        idempotency_key: str,
        reason_codes: tuple[str, ...],
        execution: JsonObject | None = None,
    ) -> StoredProposal:
        occurred_at = _utc_text(occurred_at_utc)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                prior = self._connection.execute(
                    "SELECT event_json FROM proposal_event WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if prior is not None:
                    existing_event = json.loads(str(prior["event_json"]))
                    self._verify_idempotent_event(
                        existing_event,
                        proposal_id=proposal_id,
                        event_type=event_type,
                        occurred_at=occurred_at,
                        actor_type=actor_type,
                        user_id_sha256=user_id_sha256,
                        chat_id_sha256=chat_id_sha256,
                        nonce_sha256=nonce_sha256,
                        reason_codes=reason_codes,
                    )
                    self._connection.execute("ROLLBACK")
                    stored = self.get(proposal_id)
                    if stored is None:
                        raise ProposalStoreError("idempotent proposal event is orphaned")
                    if execution is not None and stored.document["execution"] != execution:
                        raise ProposalStoreError("proposal execution idempotency conflict")
                    return stored
                row = self._connection.execute(
                    "SELECT * FROM proposal WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if row is None:
                    raise ProposalStoreError("proposal does not exist")
                current_state = ProposalState(str(row["state"]))
                if current_state is not expected_state:
                    raise ProposalStoreError("proposal state transition is not allowed")
                expires_at = _parse_utc(str(row["expires_at_utc"]))
                if event_type == "APPROVAL_REQUESTED" and occurred_at_utc >= expires_at:
                    raise ProposalStoreError("expired proposal cannot request approval")
                if event_type in {"USER_APPROVED", "USER_REJECTED"}:
                    self._validate_user_decision(
                        row,
                        occurred_at_utc=occurred_at_utc,
                        user_id_sha256=user_id_sha256,
                        chat_id_sha256=chat_id_sha256,
                        nonce_sha256=nonce_sha256,
                    )
                if event_type == "APPROVAL_EXPIRED" and occurred_at_utc < expires_at:
                    raise ProposalStoreError("proposal cannot expire before its TTL")
                if execution is not None and (
                    event_type != "EXECUTION_QUEUED" or row["execution_json"] is not None
                ):
                    raise ProposalStoreError("proposal execution binding is not allowed")
                sequence_row = self._connection.execute(
                    "SELECT COUNT(*) AS count FROM proposal_event WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                assert sequence_row is not None
                sequence = int(sequence_row["count"])
                if sequence >= MAX_EVENTS:
                    raise ProposalStoreError("proposal event history is full")
                event = self._event(
                    proposal_id=proposal_id,
                    action_id=str(row["action_id"]),
                    event_type=event_type,
                    from_state=current_state,
                    to_state=to_state,
                    occurred_at=occurred_at,
                    actor_type=actor_type,
                    user_id_sha256=user_id_sha256,
                    chat_id_sha256=chat_id_sha256,
                    nonce_sha256=nonce_sha256,
                    idempotency_key=idempotency_key,
                    reason_codes=reason_codes,
                )
                event_json = _canonical_json(event)
                self._connection.execute(
                    """
                    INSERT INTO proposal_event (
                        event_id, proposal_id, sequence, idempotency_key,
                        event_sha256, event_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        proposal_id,
                        sequence,
                        idempotency_key,
                        _sha256(event_json),
                        event_json,
                    ),
                )
                authorization = json.loads(str(row["authorization_json"]))
                if event_type in {"USER_APPROVED", "USER_REJECTED"}:
                    authorization["decided_by"] = {
                        "user_id_sha256": user_id_sha256,
                        "chat_id_sha256": chat_id_sha256,
                        "decided_at_utc": occurred_at,
                    }
                self._connection.execute(
                    """
                    UPDATE proposal
                    SET state = ?, updated_at_utc = ?, authorization_json = ?, execution_json = ?
                    WHERE proposal_id = ? AND state = ?
                    """,
                    (
                        to_state.value,
                        occurred_at,
                        _canonical_json(authorization),
                        None if execution is None else _canonical_json(execution),
                        proposal_id,
                        current_state.value,
                    ),
                )
                updated = self._connection.execute(
                    "SELECT * FROM proposal WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                assert updated is not None
                document = self._record_document(updated)
                self._validate_record(document)
                record_sha256 = _sha256(_canonical_json(document))
                self._connection.execute(
                    "UPDATE proposal SET record_sha256 = ? WHERE proposal_id = ?",
                    (record_sha256, proposal_id),
                )
                self._connection.execute("COMMIT")
                final_row = self._connection.execute(
                    "SELECT * FROM proposal WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                assert final_row is not None
                return self._stored_proposal(final_row)
            except ProposalStoreError:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise ProposalStoreError("proposal transition failed") from error

    def get(self, proposal_id: str) -> StoredProposal | None:
        try:
            row = self._connection.execute(
                "SELECT * FROM proposal WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        except sqlite3.Error as error:
            raise ProposalStoreError("proposal store is not readable") from error
        return None if row is None else self._stored_proposal(row)

    def pending(self, limit: int = 20) -> tuple[StoredProposal, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("proposal query limit must be in [1, 100]")
        try:
            rows = self._connection.execute(
                """
                SELECT * FROM proposal WHERE state = 'PENDING_APPROVAL'
                ORDER BY expires_at_utc, proposal_id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.Error as error:
            raise ProposalStoreError("proposal store is not readable") from error
        return tuple(self._stored_proposal(row) for row in rows)

    def _stored_proposal(self, row: sqlite3.Row) -> StoredProposal:
        document = self._record_document(row)
        self._validate_record(document)
        canonical = _canonical_json(document)
        actual_hash = _sha256(canonical)
        if actual_hash != row["record_sha256"]:
            raise ProposalStoreError("proposal record hash mismatch")
        return StoredProposal(
            proposal_id=str(row["proposal_id"]),
            cycle_id=str(row["cycle_id"]),
            action_id=str(row["action_id"]),
            source_record_sha256=str(row["source_record_sha256"]),
            state=ProposalState(str(row["state"])),
            record_sha256=actual_hash,
            _document_json=canonical,
        )

    def _record_document(self, row: sqlite3.Row) -> JsonObject:
        events_rows = self._connection.execute(
            "SELECT * FROM proposal_event WHERE proposal_id = ? ORDER BY sequence",
            (row["proposal_id"],),
        ).fetchall()
        events: list[JsonObject] = []
        for event_row in events_rows:
            event_json = str(event_row["event_json"])
            if _sha256(event_json) != event_row["event_sha256"]:
                raise ProposalStoreError("proposal event hash mismatch")
            event = json.loads(event_json)
            if not isinstance(event, dict):
                raise ProposalStoreError("proposal event is invalid")
            events.append(event)
        action_json = str(row["action_json"])
        if _sha256(action_json) != row["action_sha256"]:
            raise ProposalStoreError("proposal action hash mismatch")
        action = json.loads(action_json)
        authorization = json.loads(str(row["authorization_json"]))
        execution = (
            None if row["execution_json"] is None else json.loads(str(row["execution_json"]))
        )
        return {
            "schema_version": 1,
            "proposal_id": str(row["proposal_id"]),
            "cycle_id": str(row["cycle_id"]),
            "action": action,
            "action_sha256": str(row["action_sha256"]),
            "state": str(row["state"]),
            "created_at_utc": str(row["created_at_utc"]),
            "updated_at_utc": str(row["updated_at_utc"]),
            "expires_at_utc": str(row["expires_at_utc"]),
            "nonce_sha256": str(row["nonce_sha256"]),
            "idempotency_key": str(row["idempotency_key"]),
            "authorization": authorization,
            "events": events,
            "execution": execution,
        }

    def _validate_record(self, document: JsonObject) -> None:
        try:
            self._validator.validate(document)
        except jsonschema.ValidationError as error:
            raise ProposalStoreError("proposal record violates its schema") from error
        events = document["events"]
        assert isinstance(events, list)
        if not events or len(events) > MAX_EVENTS:
            raise ProposalStoreError("proposal event history length is invalid")
        expected_from: str | None = None
        previous_at: datetime | None = None
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                raise ProposalStoreError("proposal event is invalid")
            if event["proposal_id"] != document["proposal_id"]:
                raise ProposalStoreError("proposal event id binding mismatch")
            if event["action_id"] != document["action"]["action_id"]:
                raise ProposalStoreError("proposal event action binding mismatch")
            if event["from_state"] != expected_from:
                raise ProposalStoreError("proposal event order is invalid")
            occurred_at = _parse_utc(str(event["occurred_at_utc"]))
            if previous_at is not None and occurred_at < previous_at:
                raise ProposalStoreError("proposal event timestamps are not monotonic")
            expected_from = str(event["to_state"])
            previous_at = occurred_at
            if index == 0 and event["event_type"] != "CREATED":
                raise ProposalStoreError("proposal history must start with CREATED")
        if expected_from != document["state"]:
            raise ProposalStoreError("proposal projection state does not match history")
        if document["updated_at_utc"] != events[-1]["occurred_at_utc"]:
            raise ProposalStoreError("proposal update timestamp does not match history")
        action_json = _canonical_json(document["action"])
        if _sha256(action_json) != document["action_sha256"]:
            raise ProposalStoreError("proposal action content hash mismatch")

    @staticmethod
    def _event(
        *,
        proposal_id: str,
        action_id: str,
        event_type: str,
        from_state: ProposalState | None,
        to_state: ProposalState,
        occurred_at: str,
        actor_type: str,
        idempotency_key: str,
        reason_codes: tuple[str, ...],
        user_id_sha256: str | None = None,
        chat_id_sha256: str | None = None,
        nonce_sha256: str | None = None,
    ) -> JsonObject:
        return {
            "schema_version": 1,
            "event_id": _event_id(idempotency_key),
            "proposal_id": proposal_id,
            "action_id": action_id,
            "event_type": event_type,
            "from_state": None if from_state is None else from_state.value,
            "to_state": to_state.value,
            "occurred_at_utc": occurred_at,
            "actor": {
                "actor_type": actor_type,
                "user_id_sha256": user_id_sha256,
                "chat_id_sha256": chat_id_sha256,
            },
            "nonce_sha256": nonce_sha256,
            "idempotency_key": idempotency_key,
            "reason_codes": list(reason_codes),
        }

    @staticmethod
    def _validate_source_record(record: StoredDecisionRecord) -> None:
        if record.outcome is not DecisionOutcome.CANDIDATE_ACTIONS:
            raise ProposalStoreError("only candidate decision records can create proposals")
        canonical = _canonical_json(record.document)
        if _sha256(canonical) != record.record_sha256:
            raise ProposalStoreError("source decision record hash mismatch")
        document = record.document
        if document.get("runtime_authority") is not False:
            raise ProposalStoreError("source decision record claims runtime authority")
        decision = document.get("decision")
        if not isinstance(decision, dict) or decision.get("cycle_id") != record.cycle_id:
            raise ProposalStoreError("source decision binding is invalid")

    @staticmethod
    def _verify_existing_ingest(
        row: sqlite3.Row,
        *,
        decision_record: StoredDecisionRecord,
        action: JsonObject,
        authorization: ProposalAuthorization,
    ) -> None:
        stored_authorization = json.loads(str(row["authorization_json"]))
        if (
            row["source_record_sha256"] != decision_record.record_sha256
            or row["action_json"] != _canonical_json(action)
            or row["nonce_sha256"] != authorization.nonce_sha256
            or stored_authorization.get("allowed_user_id_sha256")
            != sorted(authorization.allowed_user_id_sha256)
            or stored_authorization.get("allowed_chat_id_sha256")
            != sorted(authorization.allowed_chat_id_sha256)
        ):
            raise ProposalStoreError("proposal id content conflict")

    @staticmethod
    def _validate_user_decision(
        row: sqlite3.Row,
        *,
        occurred_at_utc: datetime,
        user_id_sha256: str | None,
        chat_id_sha256: str | None,
        nonce_sha256: str | None,
    ) -> None:
        if occurred_at_utc >= _parse_utc(str(row["expires_at_utc"])):
            raise ProposalStoreError("proposal approval TTL has expired")
        authorization = json.loads(str(row["authorization_json"]))
        if (
            user_id_sha256 not in authorization["allowed_user_id_sha256"]
            or chat_id_sha256 not in authorization["allowed_chat_id_sha256"]
        ):
            raise ProposalStoreError("proposal actor is not authorized")
        if nonce_sha256 != row["nonce_sha256"]:
            raise ProposalStoreError("proposal nonce hash mismatch")

    @staticmethod
    def _verify_idempotent_event(
        existing: JsonObject,
        *,
        proposal_id: str,
        event_type: str,
        occurred_at: str,
        actor_type: str,
        user_id_sha256: str | None,
        chat_id_sha256: str | None,
        nonce_sha256: str | None,
        reason_codes: tuple[str, ...],
    ) -> None:
        expected = {
            "proposal_id": proposal_id,
            "event_type": event_type,
            "occurred_at_utc": occurred_at,
            "actor": {
                "actor_type": actor_type,
                "user_id_sha256": user_id_sha256,
                "chat_id_sha256": chat_id_sha256,
            },
            "nonce_sha256": nonce_sha256,
            "reason_codes": list(reason_codes),
        }
        if any(existing.get(key) != value for key, value in expected.items()):
            raise ProposalStoreError("proposal event idempotency conflict")
