from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from alphamind.approval import (
    NotificationFact,
    NotificationKind,
    NotificationMessageRenderer,
    TelegramApprovalError,
    TelegramMessageRef,
    TelegramNotificationOutbox,
    TelegramNotificationWorker,
)
from alphamind.approval.notifications import NotificationBot, NotificationOutboxError

NOW = datetime(2026, 7, 18, 12, 0, 10, tzinfo=UTC)
PROPOSAL_ID = "proposal-20260718T120000Z-123456789abc"
EXECUTION_ID = "exec-20260718T120000Z-123456789abc"


class RecordingNotificationBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[int, str]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: None = None,
    ) -> TelegramMessageRef:
        if self.fail:
            raise TelegramApprovalError("secret-token provider body must not persist")
        self.messages.append((chat_id, text))
        return TelegramMessageRef(chat_id=chat_id, message_id=len(self.messages))


def _execution_fact(
    kind: NotificationKind = NotificationKind.EXECUTION_PARTIALLY_FILLED,
    *,
    source_event_id: str = "exec-20260718T120000Z-123456789abc:partial:1",
) -> NotificationFact:
    requested = Decimal("2")
    filled = Decimal("0.75")
    average = Decimal("156.20")
    if kind is NotificationKind.EXECUTION_SUCCEEDED:
        filled = requested
    elif kind in {
        NotificationKind.EXECUTION_NOT_EXECUTED,
        NotificationKind.EXECUTION_FAILED,
    }:
        filled = Decimal("0")
        average = None
    return NotificationFact(
        source_event_id=source_event_id,
        kind=kind,
        occurred_at_utc=NOW,
        reason_codes=(
            "PARTIAL_FILL_CONFIRMED"
            if kind is NotificationKind.EXECUTION_PARTIALLY_FILLED
            else kind.value,
        ),
        proposal_id=PROPOSAL_ID,
        execution_id=EXECUTION_ID,
        instrument_id="SOL",
        market="linear_perpetual",
        action="OPEN",
        requested_quantity=requested,
        filled_quantity=filled,
        average_fill_price=average,
        order_ids=("order-fixture-001",),
    )


def test_partial_fill_renderer_exposes_remaining_risk_without_claiming_finality() -> None:
    fact = _execution_fact()

    message = NotificationMessageRenderer().render(fact)

    assert "部分成交" in message
    assert "累计成交: 0.75" in message
    assert "可能仍有实际风险敞口" in message
    assert fact.notification_id in message
    assert len(message) <= 4096


def test_notification_fact_rejects_impossible_execution_and_risk_claims() -> None:
    with pytest.raises(ValueError, match="partial fill quantities"):
        NotificationFact(
            source_event_id="exec-fixture:partial:invalid",
            kind=NotificationKind.EXECUTION_PARTIALLY_FILLED,
            occurred_at_utc=NOW,
            reason_codes=("PARTIAL_FILL_CONFIRMED",),
            proposal_id=PROPOSAL_ID,
            execution_id=EXECUTION_ID,
            instrument_id="SOL",
            market="linear_perpetual",
            action="OPEN",
            requested_quantity=Decimal("1"),
            filled_quantity=Decimal("1"),
            average_fill_price=Decimal("156"),
        )

    with pytest.raises(ValueError, match="cannot claim execution"):
        NotificationFact(
            source_event_id="risk-20260718T120000Z-123456789abc:alert",
            kind=NotificationKind.RISK_ALERT,
            occurred_at_utc=NOW,
            reason_codes=("DAILY_LOSS_LIMIT_REACHED",),
            execution_id=EXECUTION_ID,
            risk_state="CLOSE_ONLY",
        )

    with pytest.raises(ValueError, match="unsupported"):
        NotificationFact(
            source_event_id="exec-fixture:unsupported:market",
            kind=NotificationKind.EXECUTION_NOT_EXECUTED,
            occurred_at_utc=NOW,
            reason_codes=("EXECUTION_NOT_EXECUTED",),
            proposal_id=PROPOSAL_ID,
            execution_id=EXECUTION_ID,
            instrument_id="SOL",
            market="unknown",
            action="OPEN",
            filled_quantity=Decimal("0"),
        )


def test_outbox_is_durable_idempotent_and_rejects_same_source_with_different_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telegram-notifications.sqlite"
    first = _execution_fact()
    outbox = TelegramNotificationOutbox(path)

    assert outbox.enqueue(first)
    assert not outbox.enqueue(first)
    outbox.close()

    restarted = TelegramNotificationOutbox(path)
    claimed = restarted.claim_batch(now_utc=NOW)
    assert [item.fact.notification_id for item in claimed] == [first.notification_id]
    restarted.mark_failed(
        first.notification_id,
        now_utc=NOW,
        error_code="TELEGRAM_DELIVERY_FAILED",
    )
    conflicting = _execution_fact(NotificationKind.EXECUTION_FAILED)
    with pytest.raises(NotificationOutboxError, match="content conflict"):
        restarted.enqueue(conflicting)
    restarted.close()


def test_worker_delivers_each_persisted_fact_once_in_normal_operation(tmp_path: Path) -> None:
    outbox = TelegramNotificationOutbox(tmp_path / "notifications.sqlite")
    fact = _execution_fact(NotificationKind.EXECUTION_SUCCEEDED)
    assert outbox.enqueue(fact)
    bot = RecordingNotificationBot()
    worker = TelegramNotificationWorker(outbox, cast(NotificationBot, bot))

    first = asyncio.run(worker.run_once(chat_id=-100123456, now_utc=NOW))
    repeated = asyncio.run(worker.run_once(chat_id=-100123456, now_utc=NOW + timedelta(seconds=1)))

    assert (first.claimed, first.delivered, first.retried) == (1, 1, 0)
    assert (repeated.claimed, repeated.delivered, repeated.retried) == (0, 0, 0)
    assert len(bot.messages) == 1
    state = outbox.state_for_test(fact.notification_id)
    assert state["state"] == "DELIVERED"
    assert state["telegram_message_id"] == 1
    outbox.close()


def test_outbox_rejects_tampered_persisted_fact(tmp_path: Path) -> None:
    path = tmp_path / "notifications.sqlite"
    fact = _execution_fact()
    outbox = TelegramNotificationOutbox(path)
    assert outbox.enqueue(fact)
    outbox.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE telegram_notification_outbox SET fact_json = ? WHERE notification_id = ?",
        ('{"schema_version":1}', fact.notification_id),
    )
    connection.commit()
    connection.close()

    restarted = TelegramNotificationOutbox(path)
    with pytest.raises(NotificationOutboxError, match="claimed notification is invalid"):
        restarted.claim_batch(now_utc=NOW)
    restarted.close()


def test_worker_retries_with_stable_error_code_and_risk_alert_has_no_execution_claim(
    tmp_path: Path,
) -> None:
    outbox = TelegramNotificationOutbox(tmp_path / "notifications.sqlite")
    fact = NotificationFact(
        source_event_id="risk-20260718T120000Z-123456789abc:alert",
        kind=NotificationKind.RISK_ALERT,
        occurred_at_utc=NOW,
        reason_codes=("DAILY_LOSS_LIMIT_REACHED",),
        risk_state="CLOSE_ONLY",
    )
    assert outbox.enqueue(fact)
    bot = RecordingNotificationBot(fail=True)
    worker = TelegramNotificationWorker(outbox, cast(NotificationBot, bot))

    result = asyncio.run(worker.run_once(chat_id=-100123456, now_utc=NOW))

    assert (result.claimed, result.delivered, result.retried) == (1, 0, 1)
    state = outbox.state_for_test(fact.notification_id)
    assert state["state"] == "PENDING"
    assert state["last_error_code"] == "TELEGRAM_DELIVERY_FAILED"
    assert "secret-token" not in str(state)
    message = NotificationMessageRenderer().render(fact)
    assert "风险状态: CLOSE_ONLY" in message
    assert "Execution:" not in message
    outbox.close()
