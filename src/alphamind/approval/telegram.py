"""R3-02 Telegram 审批展示与已验证回调适配层。"""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from alphamind.approval.store import (
    ProposalState,
    ProposalStore,
    ProposalStoreError,
    StoredProposal,
)

JsonObject = dict[str, Any]
BOT_TOKEN_PATTERN = re.compile(r"^[0-9]{1,20}:[A-Za-z0-9_-]{20,128}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
PROPOSAL_ID_PATTERN = re.compile(r"^proposal-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$")
MAX_MESSAGE_CHARS = 4096
MAX_CALLBACK_BYTES = 64


class TelegramApprovalError(RuntimeError):
    """Telegram 展示或审批适配无法安全完成。"""


class TelegramCallbackAction(StrEnum):
    DETAIL = "detail"
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class TelegramMessageRef:
    """Telegram 消息坐标；仅在适配器内存中使用，不写入 Proposal Store。"""

    chat_id: int
    message_id: int

    def __post_init__(self) -> None:
        if isinstance(self.chat_id, bool) or not isinstance(self.chat_id, int):
            raise ValueError("Telegram chat id must be an integer")
        if isinstance(self.message_id, bool) or self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class VerifiedTelegramCallback:
    """R3-03 完成白名单、nonce 与原始 callback 校验后交给本层的最小事实。"""

    query_id: str
    action: TelegramCallbackAction
    proposal_id: str
    message: TelegramMessageRef
    occurred_at_utc: datetime
    user_id_sha256: str
    chat_id_sha256: str
    nonce_sha256: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if (
            not self.query_id
            or len(self.query_id) > 256
            or any(ord(character) < 32 for character in self.query_id)
        ):
            raise ValueError("Telegram callback query id is invalid")
        if PROPOSAL_ID_PATTERN.fullmatch(self.proposal_id) is None:
            raise ValueError("Telegram callback proposal id is invalid")
        if self.occurred_at_utc.tzinfo is None or self.occurred_at_utc.utcoffset() is None:
            raise ValueError("Telegram callback timestamp must be timezone-aware")
        for value in (self.user_id_sha256, self.chat_id_sha256, self.nonce_sha256):
            if SHA256_PATTERN.fullmatch(value) is None:
                raise ValueError("Telegram callback hash is invalid")
        if not self.idempotency_key or len(self.idempotency_key) > 200:
            raise ValueError("Telegram callback idempotency key is invalid")


@dataclass(frozen=True, slots=True)
class PublishedProposal:
    proposal: StoredProposal
    message: TelegramMessageRef


def callback_data(action: TelegramCallbackAction, proposal_id: str) -> str:
    """构造只含路由事实的短 payload；原始 nonce 与交易参数不得进入按钮。"""

    if PROPOSAL_ID_PATTERN.fullmatch(proposal_id) is None:
        raise ValueError("Telegram callback proposal id is invalid")
    value = f"{action.value}:{proposal_id}"
    if len(value.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("Telegram callback data exceeds 64 bytes")
    return value


class TelegramBotClient:
    """基于 python-telegram-bot 的窄接口；异常不向上暴露 token 或响应正文。"""

    def __init__(
        self,
        bot_token: str,
        *,
        timeout_seconds: float = 10.0,
        bot: Bot | None = None,
    ) -> None:
        if BOT_TOKEN_PATTERN.fullmatch(bot_token) is None:
            raise ValueError("Telegram bot token format is invalid")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Telegram timeout must be in (0, 30]")
        self._bot = Bot(token=bot_token) if bot is None else bot
        self._timeout_seconds = timeout_seconds

    async def __aenter__(self) -> TelegramBotClient:
        try:
            await self._bot.initialize()
        except TelegramError as error:
            raise TelegramApprovalError("Telegram bot initialization failed") from error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self._bot.shutdown()
        except TelegramError as error:
            raise TelegramApprovalError("Telegram bot shutdown failed") from error

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup,
    ) -> TelegramMessageRef:
        _validate_chat_id(chat_id)
        _validate_message_text(text)
        try:
            result = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                read_timeout=self._timeout_seconds,
                write_timeout=self._timeout_seconds,
                connect_timeout=self._timeout_seconds,
                pool_timeout=self._timeout_seconds,
            )
        except TelegramError as error:
            raise TelegramApprovalError("Telegram sendMessage failed") from error
        message_id = result.message_id
        if isinstance(message_id, bool) or message_id <= 0:
            raise TelegramApprovalError("Telegram sendMessage returned an invalid result")
        return TelegramMessageRef(chat_id=chat_id, message_id=message_id)

    async def edit_message_text(
        self,
        message: TelegramMessageRef,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> None:
        _validate_message_text(text)
        try:
            # reply_markup=None 会移除终态消息上的 inline keyboard。
            await self._bot.edit_message_text(
                text=text,
                chat_id=message.chat_id,
                message_id=message.message_id,
                reply_markup=reply_markup,
                read_timeout=self._timeout_seconds,
                write_timeout=self._timeout_seconds,
                connect_timeout=self._timeout_seconds,
                pool_timeout=self._timeout_seconds,
            )
        except TelegramError as error:
            raise TelegramApprovalError("Telegram editMessageText failed") from error

    async def answer_callback_query(self, query_id: str) -> None:
        if (
            not query_id
            or len(query_id) > 256
            or any(ord(character) < 32 for character in query_id)
        ):
            raise ValueError("Telegram callback query id is invalid")
        try:
            result = await self._bot.answer_callback_query(
                callback_query_id=query_id,
                read_timeout=self._timeout_seconds,
                write_timeout=self._timeout_seconds,
                connect_timeout=self._timeout_seconds,
                pool_timeout=self._timeout_seconds,
            )
        except TelegramError as error:
            raise TelegramApprovalError("Telegram callback acknowledgement failed") from error
        if result is not True:
            raise TelegramApprovalError("Telegram callback acknowledgement was rejected")


class ProposalMessageRenderer:
    """把受 Schema 约束的 Proposal 渲染为无 parse mode 的 Telegram 纯文本。"""

    def overview(self, proposal: StoredProposal) -> str:
        document = proposal.document
        action = _action(document)
        lines = [
            "AI 交易候选（仅供审批，不代表已下单）",
            f"Proposal: {proposal.proposal_id}",
            f"状态: {proposal.state.value}",
            f"标的: {_text(action.get('instrument_id'))}",
            "市场/方向/动作: "
            f"{_text(action.get('market'))} / {_text(action.get('side'))} / "
            f"{_text(action.get('action'))}",
            f"入场: {_entry_text(action.get('entry'))}",
            f"止损: {_text(action.get('stop_loss'))}",
            f"止盈: {_list_text(action.get('take_profit'))}",
            f"请求杠杆: {_text(action.get('requested_leverage'))}x",
            f"有效至: {_text(document.get('expires_at_utc'))}",
            f"理由代码: {_list_text(action.get('reason_codes'))}",
            "批准仅形成授权；执行前仍需重新校验账户、行情、风险和市场规则。",
        ]
        return _bounded_message("\n".join(lines))

    def detail(self, proposal: StoredProposal) -> str:
        document = proposal.document
        action = _action(document)
        risks = action.get("risks")
        risk_lines = (
            [f"- {_text(item, limit=400)}" for item in risks]
            if isinstance(risks, list) and risks
            else ["- 无"]
        )
        lines = [
            self.overview(proposal),
            "",
            f"订单偏好: {_text(action.get('order_preference'))}",
            f"减仓比例: {_text(action.get('reduce_fraction'))}",
            f"目标引用: {_text(action.get('target_reference_id'))}",
            f"新闻引用: {_list_text(action.get('news_refs'))}",
            f"说明: {_text(action.get('rationale'), limit=800)}",
            "风险:",
            *risk_lines,
        ]
        return _bounded_message("\n".join(lines))

    def terminal(self, proposal: StoredProposal) -> str:
        labels = {
            ProposalState.APPROVED: "已批准（尚未执行）",
            ProposalState.REJECTED: "已拒绝",
            ProposalState.EXPIRED: "已过期",
        }
        label = labels.get(proposal.state, proposal.state.value)
        return _bounded_message(
            f"{self.overview(proposal)}\n\n审批结果: {label}\n此消息已不可再次操作。"
        )

    def keyboard(self, proposal_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "查看详情",
                        callback_data=callback_data(TelegramCallbackAction.DETAIL, proposal_id),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "批准",
                        callback_data=callback_data(TelegramCallbackAction.APPROVE, proposal_id),
                    ),
                    InlineKeyboardButton(
                        "拒绝",
                        callback_data=callback_data(TelegramCallbackAction.REJECT, proposal_id),
                    ),
                ],
            ]
        )


class TelegramApprovalAdapter:
    """连接 Telegram 消息与 Proposal Store，不负责身份或 nonce 验证。"""

    def __init__(
        self,
        store: ProposalStore,
        bot: TelegramBotClient,
        renderer: ProposalMessageRenderer | None = None,
    ) -> None:
        self._store = store
        self._bot = bot
        self._renderer = renderer or ProposalMessageRenderer()

    async def publish(
        self,
        proposal_id: str,
        *,
        chat_id: int,
        occurred_at_utc: datetime,
    ) -> PublishedProposal:
        """消息确认发送后才进入 PENDING_APPROVAL；失败保持 VALIDATED。"""

        proposal = self._required_proposal(proposal_id)
        if proposal.state is not ProposalState.VALIDATED:
            raise TelegramApprovalError("proposal is not ready for Telegram delivery")
        if _utc(occurred_at_utc) >= _expires_at(proposal):
            raise TelegramApprovalError("proposal expired before Telegram delivery")
        message = await self._bot.send_message(
            chat_id,
            self._renderer.overview(proposal),
            reply_markup=self._renderer.keyboard(proposal.proposal_id),
        )
        digest = hashlib.sha256(f"{proposal.proposal_id}:{message.message_id}".encode()).hexdigest()
        try:
            pending = self._store.request_approval(
                proposal.proposal_id,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=f"telegram:send:{digest}",
            )
        except ProposalStoreError as error:
            # HTTP 与 SQLite 无法组成同一事务；尽力撤下按钮，且绝不伪造 PENDING 状态。
            with suppress(TelegramApprovalError):
                await self._bot.edit_message_text(
                    message,
                    "该候选未能进入审批状态，已禁止操作。",
                    reply_markup=None,
                )
            raise TelegramApprovalError("proposal could not enter Telegram approval") from error
        return PublishedProposal(proposal=pending, message=message)

    async def handle_callback(self, callback: VerifiedTelegramCallback) -> StoredProposal:
        """消费 R3-03 已验证回调；先 ACK，再展示详情或记录单次决定。"""

        await self._bot.answer_callback_query(callback.query_id)
        proposal = self._required_proposal(callback.proposal_id)
        if proposal.state is ProposalState.PENDING_APPROVAL and (
            _utc(callback.occurred_at_utc) >= _expires_at(proposal)
        ):
            return await self._expire(
                proposal,
                callback.message,
                occurred_at_utc=callback.occurred_at_utc,
            )
        if proposal.state in {
            ProposalState.APPROVED,
            ProposalState.REJECTED,
            ProposalState.EXPIRED,
        }:
            await self._bot.edit_message_text(
                callback.message,
                self._renderer.terminal(proposal),
                reply_markup=None,
            )
            return proposal
        if proposal.state is not ProposalState.PENDING_APPROVAL:
            raise TelegramApprovalError("proposal is not awaiting Telegram approval")
        if callback.action is TelegramCallbackAction.DETAIL:
            await self._bot.edit_message_text(
                callback.message,
                self._renderer.detail(proposal),
                reply_markup=self._renderer.keyboard(proposal.proposal_id),
            )
            return proposal
        try:
            decided = self._store.decide(
                proposal.proposal_id,
                approved=callback.action is TelegramCallbackAction.APPROVE,
                occurred_at_utc=callback.occurred_at_utc,
                user_id_sha256=callback.user_id_sha256,
                chat_id_sha256=callback.chat_id_sha256,
                nonce_sha256=callback.nonce_sha256,
                idempotency_key=callback.idempotency_key,
            )
        except ProposalStoreError as error:
            raise TelegramApprovalError("Telegram decision was rejected") from error
        await self._bot.edit_message_text(
            callback.message,
            self._renderer.terminal(decided),
            reply_markup=None,
        )
        return decided

    async def expire(
        self,
        proposal_id: str,
        message: TelegramMessageRef,
        *,
        occurred_at_utc: datetime,
    ) -> StoredProposal:
        """供调度器关闭到期消息；到期前调用会由 Store fail-closed。"""

        proposal = self._required_proposal(proposal_id)
        if proposal.state is ProposalState.EXPIRED:
            await self._bot.edit_message_text(
                message,
                self._renderer.terminal(proposal),
                reply_markup=None,
            )
            return proposal
        if proposal.state is not ProposalState.PENDING_APPROVAL:
            raise TelegramApprovalError("proposal is not awaiting Telegram approval")
        return await self._expire(proposal, message, occurred_at_utc=occurred_at_utc)

    async def _expire(
        self,
        proposal: StoredProposal,
        message: TelegramMessageRef,
        *,
        occurred_at_utc: datetime,
    ) -> StoredProposal:
        digest = hashlib.sha256(
            f"{proposal.proposal_id}:{proposal.document['expires_at_utc']}".encode()
        ).hexdigest()
        try:
            expired = self._store.expire(
                proposal.proposal_id,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=f"telegram:expire:{digest}",
            )
        except ProposalStoreError as error:
            raise TelegramApprovalError("Telegram proposal expiry was rejected") from error
        await self._bot.edit_message_text(
            message,
            self._renderer.terminal(expired),
            reply_markup=None,
        )
        return expired

    def _required_proposal(self, proposal_id: str) -> StoredProposal:
        try:
            proposal = self._store.get(proposal_id)
        except ProposalStoreError as error:
            raise TelegramApprovalError("proposal could not be read") from error
        if proposal is None:
            raise TelegramApprovalError("proposal does not exist")
        return proposal


def _validate_chat_id(chat_id: int) -> None:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise ValueError("Telegram chat id must be an integer")


def _validate_message_text(text: str) -> None:
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise ValueError("Telegram message text must contain 1 to 4096 characters")


def _action(document: JsonObject) -> JsonObject:
    action = document.get("action")
    if not isinstance(action, dict):
        raise TelegramApprovalError("proposal action is invalid")
    return action


def _text(value: object, *, limit: int = 240) -> str:
    if value is None:
        return "无"
    normalized = " ".join(str(value).split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _list_text(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "无"
    return ", ".join(_text(item, limit=160) for item in value)


def _entry_text(value: object) -> str:
    if value is None:
        return "无"
    if not isinstance(value, dict):
        return _text(value)
    return f"{_text(value.get('min'))} - {_text(value.get('max'))}"


def _bounded_message(value: str) -> str:
    if len(value) <= MAX_MESSAGE_CHARS:
        return value
    return f"{value[: MAX_MESSAGE_CHARS - 1]}…"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Telegram timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _expires_at(proposal: StoredProposal) -> datetime:
    value = proposal.document.get("expires_at_utc")
    if not isinstance(value, str):
        raise TelegramApprovalError("proposal expiry is invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as error:
        raise TelegramApprovalError("proposal expiry is invalid") from error
