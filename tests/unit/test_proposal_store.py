from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import yaml

from alphamind.ai import (
    DecisionJournal,
    DecisionJournalEntry,
    DecisionOutcome,
    StoredDecisionRecord,
)
from alphamind.approval import (
    ProposalAuthorization,
    ProposalState,
    ProposalStore,
    ProposalStoreError,
    StoredProposal,
)
from alphamind.config import load_effective_config

PROJECT_ROOT = Path(__file__).parents[2]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "contracts"
RECORDED_AT = datetime(2026, 7, 18, 12, 0, 5, tzinfo=UTC)
NOW = datetime(2026, 7, 18, 12, 0, 10, tzinfo=UTC)
USER_HASH = "e" * 64
CHAT_HASH = "f" * 64
NONCE_HASH = "1" * 64


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decision() -> dict[str, object]:
    value = yaml.safe_load((FIXTURES / "model-decision.valid.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _decision_record(
    tmp_path: Path, decision: dict[str, object] | None = None
) -> StoredDecisionRecord:
    document = decision or _decision()
    journal = DecisionJournal(tmp_path / "ai-decisions.sqlite")
    journal.append(
        DecisionJournalEntry(
            cycle_id=str(document["cycle_id"]),
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
            decision_sha256=_canonical_sha256(document),
            decision=document,
            error_code=None,
            response_id="resp_fixture",
            request_id="req_fixture",
            validation={
                "accepted_action_ids": [
                    cast(list[dict[str, object]], document["actions"])[0]["action_id"]
                ]
            },
            usage={"attempts": 1, "accounted_cost_usd": "0.001000000"},
        )
    )
    stored = journal.get(str(document["cycle_id"]))
    assert stored is not None
    journal.close()
    return stored


def _authorization() -> ProposalAuthorization:
    return ProposalAuthorization(NONCE_HASH, (USER_HASH,), (CHAT_HASH,))


def _store(tmp_path: Path) -> ProposalStore:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    return ProposalStore(effective, tmp_path / "proposals.sqlite")


def _ingest(tmp_path: Path) -> tuple[ProposalStore, StoredProposal]:
    record = _decision_record(tmp_path)
    decision = cast(dict[str, object], record.document["decision"])
    action_id = str(cast(list[dict[str, object]], decision["actions"])[0]["action_id"])
    store = _store(tmp_path)
    proposals = store.ingest_decision(
        record,
        {action_id: _authorization()},
        now_utc=NOW,
    )
    assert len(proposals) == 1
    return store, proposals[0]


def test_ingest_creates_validated_proposal_bound_to_journal_record(tmp_path: Path) -> None:
    record = _decision_record(tmp_path)
    stored_decision = cast(dict[str, object], record.document["decision"])
    action = cast(list[dict[str, object]], stored_decision["actions"])[0]
    action_id = str(action["action_id"])
    store = _store(tmp_path)

    proposals = store.ingest_decision(
        record,
        {action_id: _authorization()},
        now_utc=NOW,
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    document = proposal.document
    assert proposal.proposal_id == "proposal-20260718T120000Z-0123456789ab"
    assert proposal.source_record_sha256 == record.record_sha256
    assert proposal.state is ProposalState.VALIDATED
    assert document["action"] == action
    assert document["action_sha256"] == _canonical_sha256(action)
    assert document["expires_at_utc"] == "2026-07-18T12:10:05.000000Z"
    assert [event["event_type"] for event in document["events"]] == [
        "CREATED",
        "VALIDATION_PASSED",
    ]
    assert document["authorization"]["decided_by"] is None
    assert "runtime" not in json.dumps(document).lower()


def test_ingest_is_idempotent_and_ignores_hold_actions(tmp_path: Path) -> None:
    decision = _decision()
    actions = cast(list[dict[str, object]], decision["actions"])
    hold = dict(actions[0])
    hold["action_id"] = "act-20260718T120000Z-abcdefabcdef"
    hold["action"] = "HOLD"
    hold["order_preference"] = "none"
    hold["entry"] = None
    hold["stop_loss"] = None
    hold["take_profit"] = []
    hold["requested_leverage"] = "1"
    hold["news_refs"] = []
    actions.append(hold)
    record = _decision_record(tmp_path, decision)
    action_id = str(actions[0]["action_id"])
    store = _store(tmp_path)

    first = store.ingest_decision(record, {action_id: _authorization()}, now_utc=NOW)
    second = store.ingest_decision(
        record,
        {action_id: _authorization()},
        now_utc=NOW + timedelta(seconds=1),
    )

    assert [item.proposal_id for item in first] == [item.proposal_id for item in second]
    assert len(first[0].document["events"]) == 2


def test_approval_state_machine_is_idempotent_and_allows_one_user_decision(
    tmp_path: Path,
) -> None:
    store, proposal = _ingest(tmp_path)
    pending = store.request_approval(
        proposal.proposal_id,
        occurred_at_utc=NOW + timedelta(seconds=1),
        idempotency_key="telegram:send:fixture-01",
    )
    approved_at = NOW + timedelta(seconds=2)
    approved = store.decide(
        proposal.proposal_id,
        approved=True,
        occurred_at_utc=approved_at,
        user_id_sha256=USER_HASH,
        chat_id_sha256=CHAT_HASH,
        nonce_sha256=NONCE_HASH,
        idempotency_key="telegram:callback:fixture-01",
    )
    repeated = store.decide(
        proposal.proposal_id,
        approved=True,
        occurred_at_utc=approved_at,
        user_id_sha256=USER_HASH,
        chat_id_sha256=CHAT_HASH,
        nonce_sha256=NONCE_HASH,
        idempotency_key="telegram:callback:fixture-01",
    )

    assert pending.state is ProposalState.PENDING_APPROVAL
    assert approved.state is ProposalState.APPROVED
    assert repeated.record_sha256 == approved.record_sha256
    assert approved.document["authorization"]["decided_by"] == {
        "user_id_sha256": USER_HASH,
        "chat_id_sha256": CHAT_HASH,
        "decided_at_utc": "2026-07-18T12:00:12.000000Z",
    }
    assert [event["event_type"] for event in approved.document["events"]] == [
        "CREATED",
        "VALIDATION_PASSED",
        "APPROVAL_REQUESTED",
        "USER_APPROVED",
    ]
    record = _decision_record(tmp_path)
    reingested = store.ingest_decision(
        record,
        {proposal.action_id: _authorization()},
        now_utc=NOW + timedelta(seconds=3),
    )
    assert reingested[0].record_sha256 == approved.record_sha256
    with pytest.raises(ProposalStoreError, match="idempotency conflict"):
        store.decide(
            proposal.proposal_id,
            approved=False,
            occurred_at_utc=approved_at,
            user_id_sha256=USER_HASH,
            chat_id_sha256=CHAT_HASH,
            nonce_sha256=NONCE_HASH,
            idempotency_key="telegram:callback:fixture-01",
        )
    with pytest.raises(ProposalStoreError, match="transition is not allowed"):
        store.decide(
            proposal.proposal_id,
            approved=False,
            occurred_at_utc=approved_at + timedelta(seconds=1),
            user_id_sha256=USER_HASH,
            chat_id_sha256=CHAT_HASH,
            nonce_sha256=NONCE_HASH,
            idempotency_key="telegram:callback:fixture-02",
        )


@pytest.mark.parametrize(
    ("user_hash", "chat_hash", "nonce_hash", "message"),
    [
        ("d" * 64, CHAT_HASH, NONCE_HASH, "not authorized"),
        (USER_HASH, "d" * 64, NONCE_HASH, "not authorized"),
        (USER_HASH, CHAT_HASH, "2" * 64, "nonce hash mismatch"),
    ],
)
def test_user_decision_rejects_unauthorized_or_wrong_nonce(
    tmp_path: Path,
    user_hash: str,
    chat_hash: str,
    nonce_hash: str,
    message: str,
) -> None:
    store, proposal = _ingest(tmp_path)
    store.request_approval(
        proposal.proposal_id,
        occurred_at_utc=NOW + timedelta(seconds=1),
        idempotency_key="telegram:send:fixture-01",
    )

    with pytest.raises(ProposalStoreError, match=message):
        store.decide(
            proposal.proposal_id,
            approved=True,
            occurred_at_utc=NOW + timedelta(seconds=2),
            user_id_sha256=user_hash,
            chat_id_sha256=chat_hash,
            nonce_sha256=nonce_hash,
            idempotency_key="telegram:callback:fixture-01",
        )
    current = store.get(proposal.proposal_id)
    assert current is not None
    assert current.state is ProposalState.PENDING_APPROVAL


def test_expiry_is_fail_closed_and_cannot_happen_early(tmp_path: Path) -> None:
    store, proposal = _ingest(tmp_path)
    store.request_approval(
        proposal.proposal_id,
        occurred_at_utc=NOW + timedelta(seconds=1),
        idempotency_key="telegram:send:fixture-01",
    )

    with pytest.raises(ProposalStoreError, match="before its TTL"):
        store.expire(
            proposal.proposal_id,
            occurred_at_utc=NOW + timedelta(minutes=1),
            idempotency_key="proposal:expire:early",
        )
    expired = store.expire(
        proposal.proposal_id,
        occurred_at_utc=RECORDED_AT + timedelta(minutes=10),
        idempotency_key="proposal:expire:fixture-01",
    )

    assert expired.state is ProposalState.EXPIRED
    assert store.pending() == ()
    with pytest.raises(ProposalStoreError, match="TTL has expired"):
        other_store, other = _ingest(tmp_path / "other")
        other_store.request_approval(
            other.proposal_id,
            occurred_at_utc=NOW + timedelta(seconds=1),
            idempotency_key="telegram:send:other",
        )
        other_store.decide(
            other.proposal_id,
            approved=True,
            occurred_at_utc=RECORDED_AT + timedelta(minutes=10),
            user_id_sha256=USER_HASH,
            chat_id_sha256=CHAT_HASH,
            nonce_sha256=NONCE_HASH,
            idempotency_key="telegram:callback:expired",
        )


def test_restart_reads_state_and_detects_event_tampering(tmp_path: Path) -> None:
    path = tmp_path / "proposals.sqlite"
    store, proposal = _ingest(tmp_path)
    store.request_approval(
        proposal.proposal_id,
        occurred_at_utc=NOW + timedelta(seconds=1),
        idempotency_key="telegram:send:fixture-01",
    )
    store.close()
    restarted = ProposalStore(load_effective_config(PROJECT_ROOT, environ={}), path)
    current = restarted.get(proposal.proposal_id)
    assert current is not None
    assert current.state is ProposalState.PENDING_APPROVAL
    restarted.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE proposal_event SET event_json = '{}' WHERE proposal_id = ? AND sequence = 0",
            (proposal.proposal_id,),
        )
    tampered = ProposalStore(load_effective_config(PROJECT_ROOT, environ={}), path)
    with pytest.raises(ProposalStoreError, match="event hash mismatch"):
        tampered.get(proposal.proposal_id)


def test_store_rejects_non_candidate_and_authorization_mismatch(tmp_path: Path) -> None:
    record = _decision_record(tmp_path)
    store = _store(tmp_path)

    with pytest.raises(ProposalStoreError, match="does not match candidate actions"):
        store.ingest_decision(record, {}, now_utc=NOW)
    hold_record = record.__class__(
        cycle_id=record.cycle_id,
        outcome=DecisionOutcome.HOLD,
        recorded_at_utc=record.recorded_at_utc,
        input_sha256=record.input_sha256,
        record_sha256=record.record_sha256,
        document=record.document,
    )
    with pytest.raises(ProposalStoreError, match="only candidate"):
        store.ingest_decision(hold_record, {}, now_utc=NOW)
