"""R3-05 Telegram 执行与风险通知合同、持久化 outbox 和投递 worker。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from alphamind.approval.telegram import TelegramApprovalError, TelegramMessageRef

JsonObject = dict[str, Any]
SOURCE_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
PROPOSAL_ID_PATTERN = re.compile(r"^proposal-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$")
EXECUTION_ID_PATTERN = re.compile(r"^exec-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$")
SAFE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
REASON_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MAX_MESSAGE_CHARS = 4096
EXECUTION_MARKETS = {"spot", "linear_perpetual"}
EXECUTION_ACTIONS = {
    "OPEN",
    "ADD",
    "REDUCE",
    "CLOSE",
    "CANCEL_ORDER",
    "REPLACE_PROTECTION",
}


class NotificationKind(StrEnum):
    EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
    EXECUTION_PARTIALLY_FILLED = "EXECUTION_PARTIALLY_FILLED"
    EXECUTION_NOT_EXECUTED = "EXECUTION_NOT_EXECUTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    RISK_ALERT = "RISK_ALERT"


class NotificationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


SEVERITY_BY_KIND = {
    NotificationKind.EXECUTION_SUCCEEDED: NotificationSeverity.INFO,
    NotificationKind.EXECUTION_PARTIALLY_FILLED: NotificationSeverity.WARNING,
    NotificationKind.EXECUTION_NOT_EXECUTED: NotificationSeverity.WARNING,
    NotificationKind.EXECUTION_FAILED: NotificationSeverity.CRITICAL,
    NotificationKind.RISK_ALERT: NotificationSeverity.CRITICAL,
}


class NotificationOutboxError(RuntimeError):
    """通知事实无法安全持久化、认领或完成投递。"""


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("notification timestamp must use UTC")
    return parsed


def _decimal(value: object | None, *, label: str) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"notification {label} is invalid") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"notification {label} is invalid")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class NotificationFact:
    """只消费可信执行/风险组件产生的受限事实，不接受原始异常或响应正文。"""

    source_event_id: str
    kind: NotificationKind
    occurred_at_utc: datetime
    reason_codes: tuple[str, ...]
    proposal_id: str | None = None
    execution_id: str | None = None
    instrument_id: str | None = None
    market: str | None = None
    action: str | None = None
    requested_quantity: Decimal | None = None
    filled_quantity: Decimal | None = None
    average_fill_price: Decimal | None = None
    order_ids: tuple[str, ...] = ()
    risk_state: str | None = None

    def __post_init__(self) -> None:
        if SOURCE_EVENT_ID_PATTERN.fullmatch(self.source_event_id) is None:
            raise ValueError("notification source event id is invalid")
        _utc_text(self.occurred_at_utc)
        if (
            not self.reason_codes
            or len(self.reason_codes) > 16
            or len(self.reason_codes) != len(set(self.reason_codes))
            or any(REASON_CODE_PATTERN.fullmatch(code) is None for code in self.reason_codes)
        ):
            raise ValueError("notification reason codes are invalid")
        if (
            len(self.order_ids) > 20
            or len(self.order_ids) != len(set(self.order_ids))
            or any(SAFE_TOKEN_PATTERN.fullmatch(value) is None for value in self.order_ids)
        ):
            raise ValueError("notification order ids are invalid")

        for label, decimal_value in (
            ("requested quantity", self.requested_quantity),
            ("filled quantity", self.filled_quantity),
            ("average fill price", self.average_fill_price),
        ):
            if decimal_value is not None and not isinstance(decimal_value, Decimal):
                raise ValueError(f"notification {label} must use Decimal")
        requested = _decimal(self.requested_quantity, label="requested quantity")
        filled = _decimal(self.filled_quantity, label="filled quantity")
        average = _decimal(self.average_fill_price, label="average fill price")
        if average == 0:
            raise ValueError("notification average fill price is invalid")

        if self.kind is NotificationKind.RISK_ALERT:
            if self.risk_state is None or REASON_CODE_PATTERN.fullmatch(self.risk_state) is None:
                raise ValueError("risk notification state is invalid")
            if (
                any(
                    value is not None
                    for value in (
                        self.proposal_id,
                        self.execution_id,
                        self.instrument_id,
                        self.market,
                        self.action,
                        self.requested_quantity,
                        self.filled_quantity,
                        self.average_fill_price,
                    )
                )
                or self.order_ids
            ):
                raise ValueError("risk notification cannot claim execution facts")
            return

        if (
            self.proposal_id is None
            or PROPOSAL_ID_PATTERN.fullmatch(self.proposal_id) is None
            or self.execution_id is None
            or EXECUTION_ID_PATTERN.fullmatch(self.execution_id) is None
        ):
            raise ValueError("execution notification binding is invalid")
        for label, token_value in (
            ("instrument", self.instrument_id),
            ("market", self.market),
            ("action", self.action),
        ):
            if token_value is None or SAFE_TOKEN_PATTERN.fullmatch(token_value) is None:
                raise ValueError(f"execution notification {label} is invalid")
        if self.market not in EXECUTION_MARKETS or self.action not in EXECUTION_ACTIONS:
            raise ValueError("execution notification market or action is unsupported")
        if self.risk_state is not None:
            raise ValueError("execution notification cannot embed a risk state")

        if self.kind is NotificationKind.EXECUTION_PARTIALLY_FILLED:
            if requested is None or filled is None or average is None or not 0 < filled < requested:
                raise ValueError("partial fill quantities are invalid")
        elif self.kind is NotificationKind.EXECUTION_SUCCEEDED:
            if filled is None or filled <= 0 or average is None:
                raise ValueError("successful execution fill is invalid")
            if requested is not None and filled > requested:
                raise ValueError("successful execution exceeds requested quantity")
        elif self.kind is NotificationKind.EXECUTION_NOT_EXECUTED:
            if (filled is not None and filled != 0) or average is not None:
                raise ValueError("not-executed notification cannot claim a fill")
        elif self.kind is NotificationKind.EXECUTION_FAILED:
            if filled is not None and filled > 0:
                if requested is None or average is None or filled >= requested:
                    raise ValueError("failed partial execution quantities are invalid")
            elif average is not None:
                raise ValueError("failed execution without fills cannot have an average price")

    @property
    def notification_id(self) -> str:
        value = uuid.uuid5(uuid.NAMESPACE_URL, f"alphamind:telegram:{self.source_event_id}")
        return f"notification-{value.hex}"

    @property
    def severity(self) -> NotificationSeverity:
        return SEVERITY_BY_KIND[self.kind]

    def to_document(self) -> JsonObject:
        return {
            "schema_version": 1,
            "notification_id": self.notification_id,
            "source_event_id": self.source_event_id,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "occurred_at_utc": _utc_text(self.occurred_at_utc),
            "proposal_id": self.proposal_id,
            "execution_id": self.execution_id,
            "instrument_id": self.instrument_id,
            "market": self.market,
            "action": self.action,
            "requested_quantity": _decimal_text(self.requested_quantity),
            "filled_quantity": _decimal_text(self.filled_quantity),
            "average_fill_price": _decimal_text(self.average_fill_price),
            "order_ids": list(self.order_ids),
            "risk_state": self.risk_state,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_document(cls, value: JsonObject) -> NotificationFact:
        fact = cls(
            source_event_id=str(value["source_event_id"]),
            kind=NotificationKind(str(value["kind"])),
            occurred_at_utc=_parse_utc(str(value["occurred_at_utc"])),
            reason_codes=tuple(str(code) for code in value["reason_codes"]),
            proposal_id=value.get("proposal_id"),
            execution_id=value.get("execution_id"),
            instrument_id=value.get("instrument_id"),
            market=value.get("market"),
            action=value.get("action"),
            requested_quantity=_decimal(
                value.get("requested_quantity"), label="requested quantity"
            ),
            filled_quantity=_decimal(value.get("filled_quantity"), label="filled quantity"),
            average_fill_price=_decimal(
                value.get("average_fill_price"), label="average fill price"
            ),
            order_ids=tuple(str(order_id) for order_id in value["order_ids"]),
            risk_state=value.get("risk_state"),
        )
        if value.get("notification_id") != fact.notification_id:
            raise ValueError("notification id does not match its source event")
        if value.get("severity") != fact.severity.value or value.get("schema_version") != 1:
            raise ValueError("notification envelope is invalid")
        return fact


@dataclass(frozen=True, slots=True)
class ClaimedNotification:
    notification_id: str
    fact: NotificationFact
    attempt_count: int


class TelegramNotificationOutbox:
    """先持久化再发送；不保存原始 Telegram 身份，也不拥有订单或风险事实。"""

    def __init__(
        self,
        path: str | Path,
        *,
        lease_seconds: int = 60,
        maximum_attempts: int = 8,
    ) -> None:
        if lease_seconds < 10 or maximum_attempts < 1:
            raise ValueError("notification outbox retry settings are invalid")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.maximum_attempts = maximum_attempts
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._connection:
            mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise NotificationOutboxError("notification outbox requires SQLite WAL mode")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_notification_outbox (
                    notification_id TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL,
                    fact_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('PENDING', 'IN_FLIGHT', 'DELIVERED', 'DEAD_LETTER')
                    ),
                    created_at_utc TEXT NOT NULL,
                    next_attempt_at_utc TEXT NOT NULL,
                    lease_until_utc TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    delivered_at_utc TEXT,
                    telegram_message_id INTEGER,
                    last_error_code TEXT
                )
                """
            )

    def close(self) -> None:
        self._connection.close()

    def enqueue(self, fact: NotificationFact) -> bool:
        document_json = _canonical_json(fact.to_document())
        content_sha256 = hashlib.sha256(document_json.encode()).hexdigest()
        occurred_at = _utc_text(fact.occurred_at_utc)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT content_sha256 FROM telegram_notification_outbox "
                    "WHERE notification_id = ?",
                    (fact.notification_id,),
                ).fetchone()
                if existing is not None:
                    self._connection.execute("ROLLBACK")
                    if existing["content_sha256"] != content_sha256:
                        raise NotificationOutboxError("notification id content conflict")
                    return False
                self._connection.execute(
                    """
                    INSERT INTO telegram_notification_outbox (
                        notification_id, content_sha256, fact_json, state,
                        created_at_utc, next_attempt_at_utc
                    ) VALUES (?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        fact.notification_id,
                        content_sha256,
                        document_json,
                        occurred_at,
                        occurred_at,
                    ),
                )
                self._connection.execute("COMMIT")
                return True
            except NotificationOutboxError:
                raise
            except sqlite3.Error as error:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise NotificationOutboxError("notification could not be enqueued") from error

    def claim_batch(self, *, now_utc: datetime, limit: int = 20) -> tuple[ClaimedNotification, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("notification batch limit must be in [1, 100]")
        now = _utc_text(now_utc)
        lease_until = _utc_text(now_utc + timedelta(seconds=self.lease_seconds))
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """
                    UPDATE telegram_notification_outbox
                    SET state = 'PENDING', lease_until_utc = NULL
                    WHERE state = 'IN_FLIGHT' AND lease_until_utc <= ?
                    """,
                    (now,),
                )
                rows = self._connection.execute(
                    """
                    SELECT notification_id, content_sha256, fact_json, attempt_count
                    FROM telegram_notification_outbox
                    WHERE state = 'PENDING' AND next_attempt_at_utc <= ?
                    ORDER BY created_at_utc, notification_id
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
                self._connection.executemany(
                    """
                    UPDATE telegram_notification_outbox
                    SET state = 'IN_FLIGHT', lease_until_utc = ?
                    WHERE notification_id = ? AND state = 'PENDING'
                    """,
                    ((lease_until, str(row["notification_id"])) for row in rows),
                )
                self._connection.execute("COMMIT")
            except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as error:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise NotificationOutboxError("notification batch could not be claimed") from error
        try:
            claimed: list[ClaimedNotification] = []
            for row in rows:
                fact_json = str(row["fact_json"])
                if hashlib.sha256(fact_json.encode()).hexdigest() != row["content_sha256"]:
                    raise ValueError("notification content hash mismatch")
                fact = NotificationFact.from_document(json.loads(fact_json))
                if fact.notification_id != row["notification_id"]:
                    raise ValueError("notification row identity mismatch")
                claimed.append(
                    ClaimedNotification(
                        notification_id=fact.notification_id,
                        fact=fact,
                        attempt_count=int(row["attempt_count"]),
                    )
                )
            return tuple(claimed)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise NotificationOutboxError("claimed notification is invalid") from error

    def mark_delivered(
        self,
        notification_id: str,
        *,
        now_utc: datetime,
        message_id: int,
    ) -> None:
        if isinstance(message_id, bool) or message_id <= 0:
            raise ValueError("Telegram message id must be positive")
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE telegram_notification_outbox
                SET state = 'DELIVERED', delivered_at_utc = ?, telegram_message_id = ?,
                    lease_until_utc = NULL, last_error_code = NULL
                WHERE notification_id = ? AND state = 'IN_FLIGHT'
                """,
                (_utc_text(now_utc), message_id, notification_id),
            )
        if cursor.rowcount != 1:
            raise NotificationOutboxError("notification is not owned by this worker")

    def mark_failed(
        self,
        notification_id: str,
        *,
        now_utc: datetime,
        error_code: str,
    ) -> None:
        if REASON_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError("notification error code is invalid")
        row = self._connection.execute(
            """
            SELECT attempt_count FROM telegram_notification_outbox
            WHERE notification_id = ? AND state = 'IN_FLIGHT'
            """,
            (notification_id,),
        ).fetchone()
        if row is None:
            raise NotificationOutboxError("notification is not owned by this worker")
        attempt_count = int(row["attempt_count"]) + 1
        state = "DEAD_LETTER" if attempt_count >= self.maximum_attempts else "PENDING"
        retry_at = now_utc + timedelta(seconds=min(300, 2 ** min(attempt_count, 8)))
        with self._connection:
            self._connection.execute(
                """
                UPDATE telegram_notification_outbox
                SET state = ?, attempt_count = ?, next_attempt_at_utc = ?,
                    lease_until_utc = NULL, last_error_code = ?
                WHERE notification_id = ? AND state = 'IN_FLIGHT'
                """,
                (state, attempt_count, _utc_text(retry_at), error_code, notification_id),
            )

    def state_for_test(self, notification_id: str) -> JsonObject:
        row = self._connection.execute(
            "SELECT * FROM telegram_notification_outbox WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if row is None:
            raise KeyError(notification_id)
        return dict(zip(row.keys(), row, strict=True))


class NotificationBot(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: None = None,
    ) -> TelegramMessageRef: ...


class NotificationMessageRenderer:
    """把受限事实渲染为纯文本；不展示异常正文、凭据或未验证交易所响应。"""

    def render(self, fact: NotificationFact) -> str:
        labels = {
            NotificationKind.EXECUTION_SUCCEEDED: "执行成功",
            NotificationKind.EXECUTION_PARTIALLY_FILLED: "部分成交",
            NotificationKind.EXECUTION_NOT_EXECUTED: "未执行",
            NotificationKind.EXECUTION_FAILED: "执行失败",
            NotificationKind.RISK_ALERT: "风险告警",
        }
        lines = [
            f"alphaMind {labels[fact.kind]}",
            f"级别: {fact.severity.value}",
            f"Notification: {fact.notification_id}",
            f"发生时间: {_utc_text(fact.occurred_at_utc)}",
        ]
        if fact.kind is NotificationKind.RISK_ALERT:
            lines.extend(
                (
                    f"风险状态: {fact.risk_state}",
                    f"原因代码: {', '.join(fact.reason_codes)}",
                    "新入场必须遵循当前 RiskSnapshot；安全退出与人工处置仍保持可用。",
                )
            )
        else:
            lines.extend(
                (
                    f"Proposal: {fact.proposal_id}",
                    f"Execution: {fact.execution_id}",
                    f"标的/市场/动作: {fact.instrument_id} / {fact.market} / {fact.action}",
                    f"请求数量: {_decimal_text(fact.requested_quantity) or '-'}",
                    f"累计成交: {_decimal_text(fact.filled_quantity) or '0'}",
                    f"成交均价: {_decimal_text(fact.average_fill_price) or '-'}",
                    f"订单引用: {', '.join(fact.order_ids) if fact.order_ids else '-'}",
                    f"原因代码: {', '.join(fact.reason_codes)}",
                )
            )
            if fact.kind in {
                NotificationKind.EXECUTION_PARTIALLY_FILLED,
                NotificationKind.EXECUTION_FAILED,
            }:
                lines.append("可能仍有实际风险敞口；以 Freqtrade Runtime DB 与交易所对账结果为准。")
            elif fact.kind is NotificationKind.EXECUTION_NOT_EXECUTED:
                lines.append("该通知不声明存在订单或成交。")
        message = "\n".join(lines)
        if len(message) <= MAX_MESSAGE_CHARS:
            return message
        return f"{message[: MAX_MESSAGE_CHARS - 16].rstrip()}\n[内容已截断]"


@dataclass(frozen=True, slots=True)
class NotificationWorkerResult:
    claimed: int
    delivered: int
    retried: int


class TelegramNotificationWorker:
    """投递已持久化事实；发送成功后崩溃可能重复，因此消息始终携带稳定 ID。"""

    def __init__(self, outbox: TelegramNotificationOutbox, bot: NotificationBot) -> None:
        self._outbox = outbox
        self._bot = bot
        self._renderer = NotificationMessageRenderer()

    async def run_once(
        self,
        *,
        chat_id: int,
        now_utc: datetime,
        batch_size: int = 20,
    ) -> NotificationWorkerResult:
        claimed = self._outbox.claim_batch(now_utc=now_utc, limit=batch_size)
        delivered = 0
        retried = 0
        for item in claimed:
            try:
                message = await self._bot.send_message(
                    chat_id,
                    self._renderer.render(item.fact),
                    reply_markup=None,
                )
            except TelegramApprovalError:
                # 只保存稳定错误码；Telegram 响应正文、token 和底层异常不得落库。
                self._outbox.mark_failed(
                    item.notification_id,
                    now_utc=now_utc,
                    error_code="TELEGRAM_DELIVERY_FAILED",
                )
                retried += 1
                continue
            self._outbox.mark_delivered(
                item.notification_id,
                now_utc=now_utc,
                message_id=message.message_id,
            )
            delivered += 1
        return NotificationWorkerResult(len(claimed), delivered, retried)
