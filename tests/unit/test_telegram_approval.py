from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
import yaml
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError

from alphamind.ai import DecisionJournal, DecisionJournalEntry, DecisionOutcome
from alphamind.approval import (
    ProposalAuthorization,
    ProposalMessageRenderer,
    ProposalState,
    ProposalStore,
    StoredProposal,
    TelegramApprovalAdapter,
    TelegramApprovalError,
    TelegramBotClient,
    TelegramCallbackAction,
    TelegramMessageRef,
    VerifiedTelegramCallback,
    callback_data,
)
from alphamind.config import load_effective_config

PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "contracts" / "model-decision.valid.yaml"
RECORDED_AT = datetime(2026, 7, 18, 12, 0, 5, tzinfo=UTC)
NOW = datetime(2026, 7, 18, 12, 0, 10, tzinfo=UTC)
USER_HASH = "e" * 64
CHAT_HASH = "f" * 64
NONCE_HASH = "1" * 64
BOT_TOKEN = f"123456:{'a' * 32}"


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ingest(tmp_path: Path) -> tuple[ProposalStore, StoredProposal]:
    decision = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(decision, dict)
    action = cast(list[dict[str, object]], decision["actions"])[0]
    journal = DecisionJournal(tmp_path / "ai-decisions.sqlite")
    journal.append(
        DecisionJournalEntry(
            cycle_id=str(decision["cycle_id"]),
            recorded_at_utc=RECORDED_AT,
            outcome=DecisionOutcome.CANDIDATE_ACTIONS,
            environment="dry_run",
            profile_id="openai_terra_trade_decision_v2",
            model_id="gpt-5.6-terra",
            prompt_id="alphamind_trade_decision",
            prompt_version=2,
            prompt_sha256="a" * 64,
            config_sha256="b" * 64,
            input_sha256="c" * 64,
            schema_versions={
                "news-item.schema.yaml": 1,
                "decision-context.schema.yaml": 2,
                "model-decision.schema.yaml": 1,
                "trade-action.schema.yaml": 2,
            },
            decision_sha256=_canonical_sha256(decision),
            decision=decision,
            error_code=None,
            response_id="resp_fixture",
            request_id="req_fixture",
            validation={"accepted_action_ids": [action["action_id"]]},
            usage={"attempts": 1, "accounted_cost_usd": "0.001000000"},
        )
    )
    record = journal.get(str(decision["cycle_id"]))
    assert record is not None
    journal.close()
    store = ProposalStore(
        load_effective_config(PROJECT_ROOT, environ={}),
        tmp_path / "proposals.sqlite",
    )
    proposals = store.ingest_decision(
        record,
        {
            str(action["action_id"]): ProposalAuthorization(
                NONCE_HASH,
                (USER_HASH,),
                (CHAT_HASH,),
            )
        },
        now_utc=NOW,
    )
    return store, proposals[0]


class RecordingBotClient:
    def __init__(self, *, send_error: bool = False) -> None:
        self.send_error = send_error
        self.sent: list[tuple[int, str, InlineKeyboardMarkup]] = []
        self.edited: list[tuple[TelegramMessageRef, str, InlineKeyboardMarkup | None]] = []
        self.answered: list[str] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup,
    ) -> TelegramMessageRef:
        if self.send_error:
            raise TelegramApprovalError("fixture send failed")
        self.sent.append((chat_id, text, reply_markup))
        return TelegramMessageRef(chat_id, 701)

    async def edit_message_text(
        self,
        message: TelegramMessageRef,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> None:
        self.edited.append((message, text, reply_markup))

    async def answer_callback_query(self, query_id: str) -> None:
        self.answered.append(query_id)


def _callback(
    proposal: StoredProposal,
    action: TelegramCallbackAction,
    *,
    occurred_at: datetime,
    suffix: str,
) -> VerifiedTelegramCallback:
    return VerifiedTelegramCallback(
        query_id=f"query-{suffix}",
        action=action,
        proposal_id=proposal.proposal_id,
        message=TelegramMessageRef(-100123456, 701),
        occurred_at_utc=occurred_at,
        user_id_sha256=USER_HASH,
        chat_id_sha256=CHAT_HASH,
        nonce_sha256=NONCE_HASH,
        idempotency_key=f"telegram:callback:{suffix}",
    )


def test_renderer_uses_python_telegram_bot_keyboard_and_bounded_callback_data(
    tmp_path: Path,
) -> None:
    store, proposal = _ingest(tmp_path)
    renderer = ProposalMessageRenderer()

    text = renderer.detail(proposal)
    keyboard = renderer.keyboard(proposal.proposal_id)
    payloads = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert isinstance(keyboard, InlineKeyboardMarkup)
    assert "SOL" in text
    assert "仅供审批，不代表已下单" in text
    assert "执行前仍需重新校验" in text
    assert all(isinstance(value, str) and len(value.encode("utf-8")) <= 64 for value in payloads)
    assert NONCE_HASH not in "".join(cast(list[str], payloads))
    assert callback_data(TelegramCallbackAction.APPROVE, proposal.proposal_id).startswith(
        "approve:proposal-"
    )
    store.close()


def test_python_telegram_bot_client_awaits_async_bot_methods_and_redacts_errors() -> None:
    bot = AsyncMock(spec=Bot)
    bot.send_message.return_value = SimpleNamespace(message_id=701)
    bot.edit_message_text.return_value = True
    bot.answer_callback_query.return_value = True
    client = TelegramBotClient(BOT_TOKEN, bot=cast(Bot, bot))
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("批准", callback_data="approve:x")]])

    async def exercise() -> None:
        message = await client.send_message(-100123456, "fixture", reply_markup=keyboard)
        await client.edit_message_text(message, "updated", reply_markup=None)
        await client.answer_callback_query("query-01")

    asyncio.run(exercise())

    assert bot.send_message.await_count == 1
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None
    assert bot.answer_callback_query.await_args.kwargs["callback_query_id"] == "query-01"

    bot.send_message.side_effect = NetworkError(f"upstream leaked {BOT_TOKEN}")
    with pytest.raises(TelegramApprovalError) as captured:
        asyncio.run(client.send_message(-100123456, "fixture", reply_markup=keyboard))
    assert BOT_TOKEN not in str(captured.value)


@pytest.mark.parametrize(
    ("action", "expected_state"),
    [
        (TelegramCallbackAction.APPROVE, ProposalState.APPROVED),
        (TelegramCallbackAction.REJECT, ProposalState.REJECTED),
    ],
)
def test_publish_detail_and_decision_update_message_and_store_once(
    tmp_path: Path,
    action: TelegramCallbackAction,
    expected_state: ProposalState,
) -> None:
    store, proposal = _ingest(tmp_path)
    bot = RecordingBotClient()
    adapter = TelegramApprovalAdapter(store, cast(TelegramBotClient, bot))

    async def exercise() -> StoredProposal:
        published = await adapter.publish(
            proposal.proposal_id,
            chat_id=-100123456,
            occurred_at_utc=NOW + timedelta(seconds=1),
        )
        detail = await adapter.handle_callback(
            _callback(
                published.proposal,
                TelegramCallbackAction.DETAIL,
                occurred_at=NOW + timedelta(seconds=2),
                suffix="detail",
            )
        )
        assert detail.state is ProposalState.PENDING_APPROVAL
        return await adapter.handle_callback(
            _callback(
                published.proposal,
                action,
                occurred_at=NOW + timedelta(seconds=3),
                suffix=action.value,
            )
        )

    decided = asyncio.run(exercise())

    assert decided.state is expected_state
    assert len(bot.sent) == 1
    assert bot.answered == ["query-detail", f"query-{action.value}"]
    assert bot.edited[0][2] is not None
    assert bot.edited[-1][2] is None
    assert "尚未执行" in bot.edited[-1][1] if expected_state is ProposalState.APPROVED else True
    assert len(decided.document["events"]) == 4
    store.close()


def test_expired_callback_acks_then_expires_and_removes_buttons(tmp_path: Path) -> None:
    store, proposal = _ingest(tmp_path)
    bot = RecordingBotClient()
    adapter = TelegramApprovalAdapter(store, cast(TelegramBotClient, bot))

    async def exercise() -> StoredProposal:
        published = await adapter.publish(
            proposal.proposal_id,
            chat_id=-100123456,
            occurred_at_utc=NOW + timedelta(seconds=1),
        )
        return await adapter.handle_callback(
            _callback(
                published.proposal,
                TelegramCallbackAction.APPROVE,
                occurred_at=RECORDED_AT + timedelta(minutes=10),
                suffix="expired",
            )
        )

    expired = asyncio.run(exercise())

    assert expired.state is ProposalState.EXPIRED
    assert bot.answered == ["query-expired"]
    assert bot.edited[-1][2] is None
    assert "已过期" in bot.edited[-1][1]
    store.close()


def test_delivery_failure_does_not_request_approval(tmp_path: Path) -> None:
    store, proposal = _ingest(tmp_path)
    bot = RecordingBotClient(send_error=True)
    adapter = TelegramApprovalAdapter(store, cast(TelegramBotClient, bot))

    with pytest.raises(TelegramApprovalError, match="fixture send failed"):
        asyncio.run(
            adapter.publish(
                proposal.proposal_id,
                chat_id=-100123456,
                occurred_at_utc=NOW + timedelta(seconds=1),
            )
        )

    current = store.get(proposal.proposal_id)
    assert current is not None
    assert current.state is ProposalState.VALIDATED
    store.close()
