"""R3-03 Telegram 白名单、nonce、callback 签名与重复点击安全边界。"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from enum import StrEnum

from telegram import Update

from alphamind.approval.store import (
    ProposalAuthorization,
    ProposalStore,
    ProposalStoreError,
    StoredProposal,
)
from alphamind.approval.telegram import (
    TelegramApprovalAdapter,
    TelegramBotClient,
    TelegramCallbackCodec,
    TelegramCallbackDataError,
    TelegramMessageRef,
    VerifiedTelegramCallback,
    telegram_id_sha256,
)
from alphamind.config import EffectiveConfig

MAX_ALLOWED_IDENTITIES = 20


class TelegramSecurityErrorCode(StrEnum):
    INVALID_ENVIRONMENT = "INVALID_ENVIRONMENT"
    INVALID_UPDATE = "INVALID_UPDATE"
    CALLBACK_REJECTED = "CALLBACK_REJECTED"
    UNAUTHORIZED = "UNAUTHORIZED"


class TelegramSecurityError(RuntimeError):
    """Telegram 安全边界 fail-closed；消息不包含原始 ID、secret 或 callback data。"""

    def __init__(self, code: TelegramSecurityErrorCode) -> None:
        self.code = code
        super().__init__(f"Telegram security check failed: {code.value}")


class TelegramSecurityPolicy:
    """只在内存中持有 callback secret；白名单进入业务层前即转换为 hash。"""

    def __init__(
        self,
        *,
        allowed_user_ids: tuple[int, ...],
        allowed_chat_ids: tuple[int, ...],
        callback_secret: bytes,
        nonce_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if (
            not allowed_user_ids
            or len(allowed_user_ids) > MAX_ALLOWED_IDENTITIES
            or len(set(allowed_user_ids)) != len(allowed_user_ids)
            or any(value <= 0 for value in allowed_user_ids)
        ):
            raise ValueError("Telegram user allowlist is invalid")
        if (
            not allowed_chat_ids
            or len(allowed_chat_ids) > MAX_ALLOWED_IDENTITIES
            or len(set(allowed_chat_ids)) != len(allowed_chat_ids)
        ):
            raise ValueError("Telegram chat allowlist is invalid")
        self.allowed_user_id_sha256 = tuple(
            sorted(telegram_id_sha256(value) for value in allowed_user_ids)
        )
        self.allowed_chat_id_sha256 = tuple(
            sorted(telegram_id_sha256(value) for value in allowed_chat_ids)
        )
        self.callback_codec = TelegramCallbackCodec(callback_secret)
        self._nonce_factory = nonce_factory

    @classmethod
    def from_config(
        cls,
        effective: EffectiveConfig,
        environ: Mapping[str, str],
        *,
        nonce_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> TelegramSecurityPolicy:
        """按配置中声明的 env 名读取安全材料，不让通用配置快照持有真实值。"""

        try:
            approval = effective.runtime["approval"]
            assert isinstance(approval, dict)
            user_env = str(approval["allowed_user_ids_env"])
            chat_env = str(approval["allowed_chat_ids_env"])
            secret_env = str(approval["callback_secret_env"])
            raw_users = environ[user_env]
            raw_chats = environ[chat_env]
            raw_secret = environ[secret_env]
            allowed_users = _parse_id_list(raw_users, users=True)
            allowed_chats = _parse_id_list(raw_chats, users=False)
            callback_secret = _parse_callback_secret(raw_secret)
            return cls(
                allowed_user_ids=allowed_users,
                allowed_chat_ids=allowed_chats,
                callback_secret=callback_secret,
                nonce_factory=nonce_factory,
            )
        except (AssertionError, KeyError, TypeError, ValueError):
            raise TelegramSecurityError(TelegramSecurityErrorCode.INVALID_ENVIRONMENT) from None

    def create_authorizations(self, action_ids: Iterable[str]) -> dict[str, ProposalAuthorization]:
        """为每个 Action 生成独立随机 nonce；同批重复 nonce 立即拒绝。"""

        result: dict[str, ProposalAuthorization] = {}
        nonce_hashes: set[str] = set()
        for action_id in action_ids:
            if not action_id or action_id in result:
                raise ValueError("Telegram authorization action ids are invalid")
            nonce = self._nonce_factory(32)
            if not isinstance(nonce, bytes) or len(nonce) != 32:
                raise TelegramSecurityError(TelegramSecurityErrorCode.INVALID_ENVIRONMENT)
            nonce_sha256 = hashlib.sha256(nonce).hexdigest()
            if nonce_sha256 in nonce_hashes:
                raise TelegramSecurityError(TelegramSecurityErrorCode.INVALID_ENVIRONMENT)
            nonce_hashes.add(nonce_sha256)
            result[action_id] = ProposalAuthorization(
                nonce_sha256=nonce_sha256,
                allowed_user_id_sha256=self.allowed_user_id_sha256,
                allowed_chat_id_sha256=self.allowed_chat_id_sha256,
            )
        return result


class TelegramCallbackAuthenticator:
    """把 python-telegram-bot Update 转换为受签名与双重白名单约束的内部事实。"""

    def __init__(
        self,
        store: ProposalStore,
        policy: TelegramSecurityPolicy,
    ) -> None:
        self._store = store
        self._policy = policy

    def verify(
        self,
        update: Update,
        *,
        occurred_at_utc: datetime,
    ) -> VerifiedTelegramCallback:
        query = update.callback_query
        if query is None:
            raise TelegramSecurityError(TelegramSecurityErrorCode.INVALID_UPDATE)
        try:
            route = self._policy.callback_codec.route(query.data)
            proposal = self._store.get(route.proposal_id)
            if proposal is None:
                raise TelegramCallbackDataError("proposal does not exist")
        except (ProposalStoreError, TelegramCallbackDataError):
            raise TelegramSecurityError(TelegramSecurityErrorCode.CALLBACK_REJECTED) from None

        # effective_message 会把 InaccessibleMessage 归一为 None；
        # inline callback 同样不被审批链接受。
        user = update.effective_user
        chat = update.effective_chat
        message = update.effective_message
        if user is None or chat is None or message is None:
            raise TelegramSecurityError(TelegramSecurityErrorCode.INVALID_UPDATE)
        try:
            user_hash = telegram_id_sha256(user.id)
            chat_hash = telegram_id_sha256(chat.id)
            authorization = _authorization(proposal)
            self._policy.callback_codec.verify(route, proposal, chat_hash)
        except (TelegramCallbackDataError, TypeError, ValueError):
            raise TelegramSecurityError(TelegramSecurityErrorCode.CALLBACK_REJECTED) from None

        # 当前环境白名单与 Proposal 创建时快照必须同时允许，配置扩权不能追溯授权旧 Proposal。
        if (
            user_hash not in self._policy.allowed_user_id_sha256
            or chat_hash not in self._policy.allowed_chat_id_sha256
            or user_hash not in authorization["allowed_user_id_sha256"]
            or chat_hash not in authorization["allowed_chat_id_sha256"]
        ):
            raise TelegramSecurityError(TelegramSecurityErrorCode.UNAUTHORIZED)

        nonce_sha256 = proposal.document.get("nonce_sha256")
        if not isinstance(nonce_sha256, str):
            raise TelegramSecurityError(TelegramSecurityErrorCode.CALLBACK_REJECTED)
        idempotency_digest = hashlib.sha256(query.id.encode()).hexdigest()
        try:
            return VerifiedTelegramCallback(
                query_id=query.id,
                action=route.action,
                proposal_id=proposal.proposal_id,
                message=TelegramMessageRef(chat.id, message.message_id),
                occurred_at_utc=occurred_at_utc,
                user_id_sha256=user_hash,
                chat_id_sha256=chat_hash,
                nonce_sha256=nonce_sha256,
                idempotency_key=f"telegram:callback:{idempotency_digest}",
            )
        except ValueError:
            raise TelegramSecurityError(TelegramSecurityErrorCode.CALLBACK_REJECTED) from None


class TelegramCallbackProcessor:
    """原始 Update 唯一入口：所有 callback 先 ACK，认证失败时不触碰 Proposal 状态。"""

    def __init__(
        self,
        bot: TelegramBotClient,
        authenticator: TelegramCallbackAuthenticator,
        adapter: TelegramApprovalAdapter,
    ) -> None:
        self._bot = bot
        self._authenticator = authenticator
        self._adapter = adapter

    async def handle_update(
        self,
        update: Update,
        *,
        occurred_at_utc: datetime,
    ) -> StoredProposal:
        query = update.callback_query
        if query is None:
            raise TelegramSecurityError(TelegramSecurityErrorCode.INVALID_UPDATE)
        # Telegram 客户端在 ACK 前持续显示进度条；ACK 失败时不记录任何用户决定。
        await self._bot.answer_callback_query(query.id)
        callback = self._authenticator.verify(update, occurred_at_utc=occurred_at_utc)
        return await self._adapter.apply_verified_callback(callback)


def _parse_id_list(raw: str, *, users: bool) -> tuple[int, ...]:
    if raw != raw.strip() or not raw:
        raise ValueError("Telegram allowlist is invalid")
    parts = raw.split(",")
    if (
        not parts
        or len(parts) > MAX_ALLOWED_IDENTITIES
        or any(not part or part != part.strip() for part in parts)
    ):
        raise ValueError("Telegram allowlist is invalid")
    values = tuple(int(part) for part in parts)
    if len(set(values)) != len(values):
        raise ValueError("Telegram allowlist is invalid")
    for value in values:
        telegram_id_sha256(value)
        if users and value <= 0:
            raise ValueError("Telegram user allowlist is invalid")
    return values


def _parse_callback_secret(raw: str) -> bytes:
    encoded = raw.encode()
    if (
        raw != raw.strip()
        or any(ord(character) < 33 for character in raw)
        or not 32 <= len(encoded) <= 256
    ):
        raise ValueError("Telegram callback secret is invalid")
    return encoded


def _authorization(proposal: StoredProposal) -> dict[str, list[str]]:
    value = proposal.document.get("authorization")
    if not isinstance(value, dict):
        raise ValueError("proposal authorization is invalid")
    users = value.get("allowed_user_id_sha256")
    chats = value.get("allowed_chat_id_sha256")
    if not isinstance(users, list) or not isinstance(chats, list):
        raise ValueError("proposal authorization is invalid")
    if not all(isinstance(item, str) for item in (*users, *chats)):
        raise ValueError("proposal authorization is invalid")
    return {
        "allowed_user_id_sha256": users,
        "allowed_chat_id_sha256": chats,
    }
