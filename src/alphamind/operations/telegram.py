"""R3-06 Telegram 运行控制命令入口。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from telegram import Update

from alphamind.approval.security import TelegramSecurityPolicy
from alphamind.approval.telegram import telegram_id_sha256
from alphamind.operations.control import (
    OperationalControlAction,
    OperationalControlError,
    OperationalControlSnapshot,
    OperationalControlStore,
)
from alphamind.risk import SnapshotReadResult


class OperationalCommandError(RuntimeError):
    """Telegram 控制命令未通过格式、身份或状态校验。"""


class OperationalCommandBot(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Any | None = None,
    ) -> object: ...


COMMAND_ACTIONS = {
    "/pause_ai": OperationalControlAction.PAUSE_AI,
    "/resume_ai": OperationalControlAction.RESUME_AI,
    "/stop_entries": OperationalControlAction.STOP_ENTRIES,
    "/resume_entries": OperationalControlAction.RESUME_ENTRIES,
    "/emergency": OperationalControlAction.EMERGENCY,
}


class TelegramOperationalCommandProcessor:
    """只接受双重白名单中的普通文本消息，提交状态后再发送回执。"""

    def __init__(
        self,
        bot: OperationalCommandBot,
        store: OperationalControlStore,
        policy: TelegramSecurityPolicy,
        *,
        risk_reader: Callable[[], SnapshotReadResult] | None = None,
    ) -> None:
        self._bot = bot
        self._store = store
        self._policy = policy
        self._risk_reader = risk_reader

    async def handle_update(
        self,
        update: Update,
        *,
        occurred_at_utc: datetime,
    ) -> OperationalControlSnapshot:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        # edited_message、channel_post、callback 和 inline update 均不能改变运行控制状态。
        if (
            update.message is None
            or message is None
            or message is not update.message
            or user is None
            or chat is None
            or not isinstance(message.text, str)
        ):
            raise OperationalCommandError("INVALID_UPDATE")
        command = message.text
        if command != command.strip() or (command != "/status" and command not in COMMAND_ACTIONS):
            raise OperationalCommandError("UNSUPPORTED_COMMAND")

        try:
            user_hash = telegram_id_sha256(user.id)
            chat_hash = telegram_id_sha256(chat.id)
        except ValueError as error:
            raise OperationalCommandError("INVALID_UPDATE") from error
        if (
            user_hash not in self._policy.allowed_user_id_sha256
            or chat_hash not in self._policy.allowed_chat_id_sha256
        ):
            raise OperationalCommandError("UNAUTHORIZED")

        if command == "/status":
            snapshot = self._store.current()
        else:
            action = COMMAND_ACTIONS[command]
            risk_entry_allowed: bool | None = None
            if action is OperationalControlAction.RESUME_ENTRIES:
                if self._risk_reader is None:
                    raise OperationalCommandError("RISK_SNAPSHOT_UNAVAILABLE")
                try:
                    risk = self._risk_reader()
                except Exception as error:
                    raise OperationalCommandError("RISK_SNAPSHOT_UNAVAILABLE") from error
                if (
                    risk.snapshot is None
                    or not risk.entry_allowed
                    or risk.close_only
                    or risk.kill_switch
                ):
                    raise OperationalCommandError("RISK_ENTRY_NOT_ALLOWED")
                risk_entry_allowed = True
            digest = hashlib.sha256(str(update.update_id).encode()).hexdigest()
            try:
                snapshot = self._store.apply(
                    action,
                    occurred_at_utc=occurred_at_utc,
                    actor_user_id_sha256=user_hash,
                    actor_chat_id_sha256=chat_hash,
                    idempotency_key=f"telegram:command:{digest}",
                    risk_entry_allowed=risk_entry_allowed,
                )
            except OperationalControlError as error:
                raise OperationalCommandError("CONTROL_TRANSITION_REJECTED") from error

        await self._bot.send_message(chat.id, _render(command, snapshot), reply_markup=None)
        return snapshot


def _render(command: str, snapshot: OperationalControlSnapshot) -> str:
    state = ", ".join(snapshot.reason_codes)
    if command == "/emergency":
        prefix = "紧急模式已持久化：AI 已暂停，新开仓已停止，待处理开仓需取消并人工复核。"
    elif command == "/status":
        prefix = "当前运行控制状态。"
    else:
        prefix = "运行控制命令已持久化。"
    # R3-06 不拥有订单写接口，避免回执暗示已经平仓或撤单。
    return (
        f"{prefix}\n状态：{state}\n"
        f"安全退出：{'允许' if snapshot.safe_exit_allowed else '禁止'}\n"
        "交易指令：未发送"
    )
