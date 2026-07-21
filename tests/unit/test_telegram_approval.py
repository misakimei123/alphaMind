from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
import yaml
from telegram import (
    Bot,
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
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
    TelegramCallbackAuthenticator,
    TelegramCallbackCodec,
    TelegramCallbackProcessor,
    TelegramMessageRef,
    TelegramSecurityError,
    TelegramSecurityErrorCode,
    TelegramSecurityPolicy,
    VerifiedTelegramCallback,
    callback_data,
    telegram_id_sha256,
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
CALLBACK_CODEC = TelegramCallbackCodec(b"s" * 32)
ACTION_ID = "act-20260718T120000Z-0123456789ab"
USER_ID = 1001
CHAT_ID = -100123456
CALLBACK_SECRET = "S" * 32


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ingest(
    tmp_path: Path,
    authorization: ProposalAuthorization | None = None,
) -> tuple[ProposalStore, StoredProposal]:
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
            str(action["action_id"]): authorization
            or ProposalAuthorization(NONCE_HASH, (USER_HASH,), (CHAT_HASH,))
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


def _policy(
    *,
    user_ids: str = str(USER_ID),
    chat_ids: str = str(CHAT_ID),
    callback_secret: str = CALLBACK_SECRET,
    nonce_factory: Callable[[int], bytes] | None = None,
) -> TelegramSecurityPolicy:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    approval = cast(dict[str, object], effective.runtime["approval"])
    environ = {
        str(approval["allowed_user_ids_env"]): user_ids,
        str(approval["allowed_chat_ids_env"]): chat_ids,
        str(approval["callback_secret_env"]): callback_secret,
    }
    if nonce_factory is None:
        return TelegramSecurityPolicy.from_config(effective, environ)
    return TelegramSecurityPolicy.from_config(
        effective,
        environ,
        nonce_factory=nonce_factory,
    )


def _raw_update(
    data: object,
    *,
    user_id: int = USER_ID,
    chat_id: int = CHAT_ID,
    query_id: str = "query-raw-01",
    update_id: int = 9001,
    accessible: bool = True,
) -> Update:
    user = User(id=user_id, first_name="Fixture", is_bot=False)
    message = None
    inline_message_id = "inline-fixture"
    if accessible:
        chat = Chat(id=chat_id, type="private")
        message = Message(
            message_id=701,
            date=NOW,
            chat=chat,
            from_user=user,
        )
        inline_message_id = None
    query = CallbackQuery(
        id=query_id,
        from_user=user,
        chat_instance="fixture-chat-instance",
        message=message,
        data=data,
        inline_message_id=inline_message_id,
    )
    return Update(update_id=update_id, callback_query=query)


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


def _secure_ingest(
    tmp_path: Path,
) -> tuple[TelegramSecurityPolicy, ProposalStore, StoredProposal]:
    policy = _policy(nonce_factory=lambda size: b"n" * size)
    authorization = policy.create_authorizations((ACTION_ID,))[ACTION_ID]
    store, proposal = _ingest(tmp_path, authorization)
    return policy, store, proposal


def test_renderer_uses_python_telegram_bot_keyboard_and_bounded_callback_data(
    tmp_path: Path,
) -> None:
    store, proposal = _ingest(tmp_path)
    renderer = ProposalMessageRenderer(CALLBACK_CODEC)

    text = renderer.detail(proposal)
    keyboard = renderer.keyboard(proposal, CHAT_HASH)
    payloads = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert isinstance(keyboard, InlineKeyboardMarkup)
    assert "SOL" in text
    assert "仅供审批，不代表已下单" in text
    assert "执行前仍需重新校验" in text
    assert all(isinstance(value, str) and len(value.encode("utf-8")) <= 64 for value in payloads)
    assert NONCE_HASH not in "".join(cast(list[str], payloads))
    assert callback_data(
        CALLBACK_CODEC,
        TelegramCallbackAction.APPROVE,
        proposal,
        CHAT_HASH,
    ).startswith("v1:a:")
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
    adapter = TelegramApprovalAdapter(store, cast(TelegramBotClient, bot), CALLBACK_CODEC)

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
    adapter = TelegramApprovalAdapter(store, cast(TelegramBotClient, bot), CALLBACK_CODEC)

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
    adapter = TelegramApprovalAdapter(store, cast(TelegramBotClient, bot), CALLBACK_CODEC)

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


def test_security_policy_loads_hashed_allowlists_and_unique_nonce_per_action() -> None:
    counter = 0

    def nonce_factory(size: int) -> bytes:
        nonlocal counter
        counter += 1
        return bytes([counter]) * size

    policy = _policy(
        user_ids=f"{USER_ID},1002",
        chat_ids=f"{CHAT_ID},1002",
        nonce_factory=nonce_factory,
    )
    authorizations = policy.create_authorizations((ACTION_ID, "act-second"))

    assert policy.allowed_user_id_sha256 == tuple(
        sorted((telegram_id_sha256(USER_ID), telegram_id_sha256(1002)))
    )
    assert policy.allowed_chat_id_sha256 == tuple(
        sorted((telegram_id_sha256(CHAT_ID), telegram_id_sha256(1002)))
    )
    assert len({item.nonce_sha256 for item in authorizations.values()}) == 2
    assert all(len(item.nonce_sha256) == 64 for item in authorizations.values())


@pytest.mark.parametrize(
    ("user_ids", "chat_ids", "secret"),
    [
        ("", str(CHAT_ID), CALLBACK_SECRET),
        (f"{USER_ID},{USER_ID}", str(CHAT_ID), CALLBACK_SECRET),
        (str(USER_ID), "0", CALLBACK_SECRET),
        (str(USER_ID), str(CHAT_ID), "short"),
        (f" {USER_ID}", str(CHAT_ID), CALLBACK_SECRET),
    ],
)
def test_security_policy_rejects_invalid_environment_without_echoing_values(
    user_ids: str,
    chat_ids: str,
    secret: str,
) -> None:
    with pytest.raises(TelegramSecurityError) as captured:
        _policy(user_ids=user_ids, chat_ids=chat_ids, callback_secret=secret)

    assert captured.value.code is TelegramSecurityErrorCode.INVALID_ENVIRONMENT
    assert not user_ids or user_ids not in str(captured.value)
    assert not chat_ids or chat_ids not in str(captured.value)
    assert not secret or secret not in str(captured.value)


def test_signed_callback_is_bounded_and_contains_no_raw_authorization_material(
    tmp_path: Path,
) -> None:
    policy, store, proposal = _secure_ingest(tmp_path)
    keyboard = ProposalMessageRenderer(policy.callback_codec).keyboard(
        proposal,
        telegram_id_sha256(CHAT_ID),
    )
    payloads = [
        cast(str, button.callback_data) for row in keyboard.inline_keyboard for button in row
    ]
    serialized = json.dumps(proposal.document, sort_keys=True)

    assert all(value.startswith("v1:") and len(value.encode()) <= 64 for value in payloads)
    assert all(str(USER_ID) not in value and str(CHAT_ID) not in value for value in payloads)
    assert CALLBACK_SECRET not in "".join(payloads)
    assert str(USER_ID) not in serialized
    assert str(CHAT_ID) not in serialized
    assert CALLBACK_SECRET not in serialized
    assert policy.callback_codec.route(payloads[1]).proposal_id == proposal.proposal_id
    store.close()


def test_raw_callback_approves_once_and_duplicate_click_returns_existing_state(
    tmp_path: Path,
) -> None:
    policy, store, proposal = _secure_ingest(tmp_path)
    bot = RecordingBotClient()
    typed_bot = cast(TelegramBotClient, bot)
    adapter = TelegramApprovalAdapter(store, typed_bot, policy.callback_codec)
    authenticator = TelegramCallbackAuthenticator(store, policy)
    processor = TelegramCallbackProcessor(typed_bot, authenticator, adapter)

    async def exercise() -> tuple[StoredProposal, StoredProposal]:
        await adapter.publish(
            proposal.proposal_id,
            chat_id=CHAT_ID,
            occurred_at_utc=NOW + timedelta(seconds=1),
        )
        approve_data = cast(str, bot.sent[0][2].inline_keyboard[1][0].callback_data)
        first = await processor.handle_update(
            _raw_update(approve_data, query_id="query-authorized-01"),
            occurred_at_utc=NOW + timedelta(seconds=2),
        )
        repeated = await processor.handle_update(
            _raw_update(
                approve_data,
                query_id="query-authorized-02",
                update_id=9002,
            ),
            occurred_at_utc=NOW + timedelta(seconds=3),
        )
        return first, repeated

    first, repeated = asyncio.run(exercise())

    assert first.state is ProposalState.APPROVED
    assert repeated.record_sha256 == first.record_sha256
    assert len(repeated.document["events"]) == 4
    assert bot.answered == ["query-authorized-01", "query-authorized-02"]
    assert all(item[2] is None for item in bot.edited)
    assert "query-authorized-01" not in json.dumps(repeated.document)
    store.close()


@pytest.mark.parametrize(
    ("user_id", "chat_id", "expected_code"),
    [
        (2002, CHAT_ID, TelegramSecurityErrorCode.UNAUTHORIZED),
        (USER_ID, -100999999, TelegramSecurityErrorCode.CALLBACK_REJECTED),
    ],
)
def test_unauthorized_raw_callback_is_acked_without_state_change(
    tmp_path: Path,
    user_id: int,
    chat_id: int,
    expected_code: TelegramSecurityErrorCode,
) -> None:
    policy, store, proposal = _secure_ingest(tmp_path)
    bot = RecordingBotClient()
    typed_bot = cast(TelegramBotClient, bot)
    adapter = TelegramApprovalAdapter(store, typed_bot, policy.callback_codec)
    processor = TelegramCallbackProcessor(
        typed_bot,
        TelegramCallbackAuthenticator(store, policy),
        adapter,
    )

    async def exercise() -> None:
        await adapter.publish(
            proposal.proposal_id,
            chat_id=CHAT_ID,
            occurred_at_utc=NOW + timedelta(seconds=1),
        )
        approve_data = bot.sent[0][2].inline_keyboard[1][0].callback_data
        with pytest.raises(TelegramSecurityError) as captured:
            await processor.handle_update(
                _raw_update(approve_data, user_id=user_id, chat_id=chat_id),
                occurred_at_utc=NOW + timedelta(seconds=2),
            )
        assert captured.value.code is expected_code

    asyncio.run(exercise())

    current = store.get(proposal.proposal_id)
    assert current is not None and current.state is ProposalState.PENDING_APPROVAL
    assert bot.answered == ["query-raw-01"]
    assert bot.edited == []
    store.close()


def test_signed_button_is_bound_to_original_chat_even_when_both_chats_are_allowed(
    tmp_path: Path,
) -> None:
    other_chat_id = -100999999
    policy = _policy(
        chat_ids=f"{CHAT_ID},{other_chat_id}",
        nonce_factory=lambda size: b"n" * size,
    )
    authorization = policy.create_authorizations((ACTION_ID,))[ACTION_ID]
    store, proposal = _ingest(tmp_path, authorization)
    bot = RecordingBotClient()
    typed_bot = cast(TelegramBotClient, bot)
    adapter = TelegramApprovalAdapter(store, typed_bot, policy.callback_codec)
    processor = TelegramCallbackProcessor(
        typed_bot,
        TelegramCallbackAuthenticator(store, policy),
        adapter,
    )

    async def exercise() -> None:
        await adapter.publish(
            proposal.proposal_id,
            chat_id=CHAT_ID,
            occurred_at_utc=NOW + timedelta(seconds=1),
        )
        approve_data = bot.sent[0][2].inline_keyboard[1][0].callback_data
        with pytest.raises(TelegramSecurityError) as captured:
            await processor.handle_update(
                _raw_update(approve_data, chat_id=other_chat_id),
                occurred_at_utc=NOW + timedelta(seconds=2),
            )
        assert captured.value.code is TelegramSecurityErrorCode.CALLBACK_REJECTED

    asyncio.run(exercise())

    current = store.get(proposal.proposal_id)
    assert current is not None and current.state is ProposalState.PENDING_APPROVAL
    assert bot.answered == ["query-raw-01"]
    store.close()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("tampered", TelegramSecurityErrorCode.CALLBACK_REJECTED),
        ("object", TelegramSecurityErrorCode.CALLBACK_REJECTED),
        ("inaccessible", TelegramSecurityErrorCode.INVALID_UPDATE),
    ],
)
def test_malformed_tampered_or_inaccessible_callback_fails_closed_after_ack(
    tmp_path: Path,
    mutation: str,
    expected_code: TelegramSecurityErrorCode,
) -> None:
    policy, store, proposal = _secure_ingest(tmp_path)
    bot = RecordingBotClient()
    typed_bot = cast(TelegramBotClient, bot)
    adapter = TelegramApprovalAdapter(store, typed_bot, policy.callback_codec)
    processor = TelegramCallbackProcessor(
        typed_bot,
        TelegramCallbackAuthenticator(store, policy),
        adapter,
    )

    async def exercise() -> None:
        await adapter.publish(
            proposal.proposal_id,
            chat_id=CHAT_ID,
            occurred_at_utc=NOW + timedelta(seconds=1),
        )
        original = cast(str, bot.sent[0][2].inline_keyboard[1][0].callback_data)
        data: object = original
        accessible = True
        if mutation == "tampered":
            data = f"{original[:-1]}{'A' if original[-1] != 'A' else 'B'}"
        elif mutation == "object":
            data = object()
        else:
            accessible = False
        with pytest.raises(TelegramSecurityError) as captured:
            await processor.handle_update(
                _raw_update(data, accessible=accessible),
                occurred_at_utc=NOW + timedelta(seconds=2),
            )
        assert captured.value.code is expected_code

    asyncio.run(exercise())

    current = store.get(proposal.proposal_id)
    assert current is not None and current.state is ProposalState.PENDING_APPROVAL
    assert bot.answered == ["query-raw-01"]
    store.close()


def test_current_allowlist_cannot_retroactively_expand_existing_proposal(
    tmp_path: Path,
) -> None:
    original_policy, store, proposal = _secure_ingest(tmp_path)
    expanded_policy = _policy(user_ids="2002", nonce_factory=lambda size: b"x" * size)
    bot = RecordingBotClient()
    typed_bot = cast(TelegramBotClient, bot)
    adapter = TelegramApprovalAdapter(store, typed_bot, original_policy.callback_codec)
    processor = TelegramCallbackProcessor(
        typed_bot,
        TelegramCallbackAuthenticator(store, expanded_policy),
        adapter,
    )

    async def exercise() -> None:
        await adapter.publish(
            proposal.proposal_id,
            chat_id=CHAT_ID,
            occurred_at_utc=NOW + timedelta(seconds=1),
        )
        approve_data = bot.sent[0][2].inline_keyboard[1][0].callback_data
        with pytest.raises(TelegramSecurityError) as captured:
            await processor.handle_update(
                _raw_update(approve_data, user_id=2002),
                occurred_at_utc=NOW + timedelta(seconds=2),
            )
        assert captured.value.code is TelegramSecurityErrorCode.UNAUTHORIZED

    asyncio.run(exercise())

    current = store.get(proposal.proposal_id)
    assert current is not None and current.state is ProposalState.PENDING_APPROVAL
    store.close()


def test_raw_callback_at_ttl_expires_instead_of_approving(tmp_path: Path) -> None:
    policy, store, proposal = _secure_ingest(tmp_path)
    bot = RecordingBotClient()
    typed_bot = cast(TelegramBotClient, bot)
    adapter = TelegramApprovalAdapter(store, typed_bot, policy.callback_codec)
    processor = TelegramCallbackProcessor(
        typed_bot,
        TelegramCallbackAuthenticator(store, policy),
        adapter,
    )

    async def exercise() -> StoredProposal:
        await adapter.publish(
            proposal.proposal_id,
            chat_id=CHAT_ID,
            occurred_at_utc=NOW + timedelta(seconds=1),
        )
        approve_data = bot.sent[0][2].inline_keyboard[1][0].callback_data
        return await processor.handle_update(
            _raw_update(approve_data),
            occurred_at_utc=RECORDED_AT + timedelta(minutes=10),
        )

    expired = asyncio.run(exercise())

    assert expired.state is ProposalState.EXPIRED
    assert bot.answered == ["query-raw-01"]
    assert bot.edited[-1][2] is None
    store.close()
