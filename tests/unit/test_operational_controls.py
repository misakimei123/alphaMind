from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from telegram import Chat, Message, Update, User

from alphamind.ai.cli import main as ai_main
from alphamind.approval import TelegramSecurityPolicy
from alphamind.operations import (
    OperationalControlAction,
    OperationalControlError,
    OperationalControlStore,
)
from alphamind.operations.telegram import (
    OperationalCommandError,
    TelegramOperationalCommandProcessor,
)
from alphamind.risk import SnapshotReadResult

PROJECT_ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 7, 22, 6, 0, tzinfo=UTC)
USER_ID = 12001
CHAT_ID = -10012001
USER_HASH = "a" * 64
CHAT_HASH = "b" * 64


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Any | None]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Any | None = None,
    ) -> object:
        self.sent.append((chat_id, text, reply_markup))
        return object()


def _update(command: str, *, update_id: int = 1, user_id: int = USER_ID) -> Update:
    user = User(id=user_id, first_name="Fixture", is_bot=False)
    chat = Chat(id=CHAT_ID, type="private")
    message = Message(
        message_id=update_id,
        date=NOW,
        chat=chat,
        from_user=user,
        text=command,
    )
    return Update(update_id=update_id, message=message)


def _policy() -> TelegramSecurityPolicy:
    return TelegramSecurityPolicy(
        allowed_user_ids=(USER_ID,),
        allowed_chat_ids=(CHAT_ID,),
        callback_secret=b"s" * 32,
    )


def _risk(*, entry_allowed: bool) -> SnapshotReadResult:
    return SnapshotReadResult(
        snapshot={"snapshot_id": "risk-fixture"} if entry_allowed else None,
        entry_allowed=entry_allowed,
        close_only=not entry_allowed,
        kill_switch=False,
        safe_exit_allowed=True,
        reason_codes=("risk_checks_passed",),
    )


def _copy_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "configs", root / "configs")
    shutil.copytree(PROJECT_ROOT / "data" / "schemas", root / "data" / "schemas")
    shutil.copytree(PROJECT_ROOT / "prompts", root / "prompts")
    return root


def test_control_store_persists_idempotent_transitions_and_safe_exit(tmp_path: Path) -> None:
    path = tmp_path / "controls.sqlite"
    with OperationalControlStore(path) as store:
        initial = store.current()
        assert initial.reason_codes == ("NORMAL_OPERATION",)

        stopped = store.apply(
            OperationalControlAction.STOP_ENTRIES,
            occurred_at_utc=NOW,
            actor_user_id_sha256=USER_HASH,
            actor_chat_id_sha256=CHAT_HASH,
            idempotency_key="telegram:command:stop",
        )
        replayed = store.apply(
            OperationalControlAction.STOP_ENTRIES,
            occurred_at_utc=NOW,
            actor_user_id_sha256=USER_HASH,
            actor_chat_id_sha256=CHAT_HASH,
            idempotency_key="telegram:command:stop",
        )

    with OperationalControlStore(path) as reopened:
        assert reopened.current() == stopped == replayed
        assert stopped.entry_stopped
        assert stopped.safe_exit_allowed


def test_emergency_cannot_be_cleared_by_normal_resume_commands(tmp_path: Path) -> None:
    with OperationalControlStore(tmp_path / "controls.sqlite") as store:
        emergency = store.apply(
            OperationalControlAction.EMERGENCY,
            occurred_at_utc=NOW,
            actor_user_id_sha256=USER_HASH,
            actor_chat_id_sha256=CHAT_HASH,
            idempotency_key="telegram:command:emergency",
        )
        assert emergency.ai_paused and emergency.entry_stopped
        assert emergency.manual_review_required and emergency.cancel_pending_entries
        assert emergency.safe_exit_allowed

        with pytest.raises(OperationalControlError, match="manual review"):
            store.apply(
                OperationalControlAction.RESUME_AI,
                occurred_at_utc=NOW,
                actor_user_id_sha256=USER_HASH,
                actor_chat_id_sha256=CHAT_HASH,
                idempotency_key="telegram:command:resume-ai",
            )
        with pytest.raises(OperationalControlError, match="manual review"):
            store.apply(
                OperationalControlAction.RESUME_ENTRIES,
                occurred_at_utc=NOW,
                actor_user_id_sha256=USER_HASH,
                actor_chat_id_sha256=CHAT_HASH,
                idempotency_key="telegram:command:resume-entries",
                risk_entry_allowed=True,
            )


def test_tampered_control_history_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "controls.sqlite"
    with OperationalControlStore(path) as store:
        store.apply(
            OperationalControlAction.PAUSE_AI,
            occurred_at_utc=NOW,
            actor_user_id_sha256=USER_HASH,
            actor_chat_id_sha256=CHAT_HASH,
            idempotency_key="telegram:command:pause",
        )
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE operational_control_event SET snapshot_json = ?",
        ('{"schema_version":1}',),
    )
    connection.commit()
    connection.close()

    with (
        OperationalControlStore(path) as reopened,
        pytest.raises(OperationalControlError, match="history is invalid"),
    ):
        reopened.current()


def test_idempotent_replay_also_verifies_complete_history(tmp_path: Path) -> None:
    path = tmp_path / "controls.sqlite"
    with OperationalControlStore(path) as store:
        store.apply(
            OperationalControlAction.PAUSE_AI,
            occurred_at_utc=NOW,
            actor_user_id_sha256=USER_HASH,
            actor_chat_id_sha256=CHAT_HASH,
            idempotency_key="telegram:command:pause",
        )
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE operational_control_event SET snapshot_sha256 = ?",
        ("0" * 64,),
    )
    connection.commit()
    connection.close()

    with (
        OperationalControlStore(path) as reopened,
        pytest.raises(OperationalControlError, match="transition failed"),
    ):
        reopened.apply(
            OperationalControlAction.PAUSE_AI,
            occurred_at_utc=NOW,
            actor_user_id_sha256=USER_HASH,
            actor_chat_id_sha256=CHAT_HASH,
            idempotency_key="telegram:command:pause",
        )


def test_authorized_telegram_commands_persist_before_safe_receipt(tmp_path: Path) -> None:
    bot = RecordingBot()
    with OperationalControlStore(tmp_path / "controls.sqlite") as store:
        processor = TelegramOperationalCommandProcessor(bot, store, _policy())
        snapshot = asyncio.run(processor.handle_update(_update("/emergency"), occurred_at_utc=NOW))

        assert snapshot.emergency
        assert store.current() == snapshot
        assert bot.sent[0][0] == CHAT_ID
        assert "交易指令：未发送" in bot.sent[0][1]
        assert str(USER_ID) not in bot.sent[0][1]


def test_telegram_rejects_unauthorized_and_unsafe_entry_resume(tmp_path: Path) -> None:
    bot = RecordingBot()
    with OperationalControlStore(tmp_path / "controls.sqlite") as store:
        processor = TelegramOperationalCommandProcessor(
            bot,
            store,
            _policy(),
            risk_reader=lambda: _risk(entry_allowed=False),
        )
        with pytest.raises(OperationalCommandError, match="UNAUTHORIZED"):
            asyncio.run(
                processor.handle_update(
                    _update("/pause_ai", user_id=USER_ID + 1),
                    occurred_at_utc=NOW,
                )
            )
        with pytest.raises(OperationalCommandError, match="RISK_ENTRY_NOT_ALLOWED"):
            asyncio.run(processor.handle_update(_update("/resume_entries"), occurred_at_utc=NOW))
        assert store.current().revision == 0
        assert bot.sent == []


def test_ai_cli_pause_blocks_provider_before_network(tmp_path: Path, capsys: Any) -> None:
    root = _copy_project(tmp_path)
    path = root / "user_data" / "state" / "operational-controls.sqlite"
    with OperationalControlStore(path) as store:
        store.apply(
            OperationalControlAction.PAUSE_AI,
            occurred_at_utc=NOW,
            actor_user_id_sha256=USER_HASH,
            actor_chat_id_sha256=CHAT_HASH,
            idempotency_key="test:pause-ai",
        )

    exit_code = ai_main(
        [
            "--project-root",
            str(root),
            "--context",
            str(PROJECT_ROOT / "tests" / "fixtures" / "contracts" / "decision-context.valid.yaml"),
        ],
        environ={},
        client=object(),  # type: ignore[arg-type]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert exit_code == 4
    assert output["status"] == "paused"
    assert output["network_request_sent"] is False
    assert output["operational_control"]["ai_paused"] is True
    assert captured.err == ""
