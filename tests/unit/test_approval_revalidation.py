from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import yaml

from alphamind.ai import (
    DecisionJournal,
    DecisionJournalEntry,
    DecisionOutcome,
    StoredDecisionRecord,
)
from alphamind.approval import (
    ActionRevalidator,
    ProposalAuthorization,
    ProposalState,
    ProposalStore,
    RevalidationCoordinator,
    RevalidationReasonCode,
)
from alphamind.config import EffectiveConfig, load_effective_config
from alphamind.decision import BoundDecisionContext, DecisionContractBinder
from alphamind.operations import OperationalControlSnapshot
from alphamind.risk import SnapshotReadResult

PROJECT_ROOT = Path(__file__).parents[2]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "contracts"
RECORDED_AT = datetime(2026, 7, 18, 12, 0, 5, tzinfo=UTC)
NOW = datetime(2026, 7, 18, 12, 0, 10, tzinfo=UTC)
USER_HASH = "e" * 64
CHAT_HASH = "f" * 64
NONCE_HASH = "1" * 64


def _yaml(name: str) -> dict[str, object]:
    value = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _decision_record(tmp_path: Path, decision: dict[str, object]) -> StoredDecisionRecord:
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
            validation={"accepted_action_ids": [decision["actions"][0]["action_id"]]},
            usage={"attempts": 1, "accounted_cost_usd": "0.001000000"},
        )
    )
    stored = journal.get(str(decision["cycle_id"]))
    assert stored is not None
    journal.close()
    return stored


def _approved(
    tmp_path: Path,
    effective: EffectiveConfig,
    decision: dict[str, object] | None = None,
) -> tuple[ProposalStore, str]:
    selected = decision or _yaml("model-decision.valid.yaml")
    record = _decision_record(tmp_path, selected)
    action = cast(list[dict[str, object]], selected["actions"])[0]
    action_id = str(action["action_id"])
    store = ProposalStore(effective, tmp_path / "proposals.sqlite")
    proposal = store.ingest_decision(
        record,
        {
            action_id: ProposalAuthorization(
                NONCE_HASH,
                (USER_HASH,),
                (CHAT_HASH,),
            )
        },
        now_utc=NOW,
    )[0]
    store.request_approval(
        proposal.proposal_id,
        occurred_at_utc=NOW + timedelta(seconds=1),
        idempotency_key="telegram:send:revalidation-fixture",
    )
    store.decide(
        proposal.proposal_id,
        approved=True,
        occurred_at_utc=NOW + timedelta(seconds=2),
        user_id_sha256=USER_HASH,
        chat_id_sha256=CHAT_HASH,
        nonce_sha256=NONCE_HASH,
        idempotency_key="telegram:callback:revalidation-fixture",
    )
    return store, proposal.proposal_id


def _bound_context(
    effective: EffectiveConfig,
    document: dict[str, object] | None = None,
    *,
    now_utc: datetime = NOW + timedelta(seconds=3),
) -> BoundDecisionContext:
    selected = document or _yaml("decision-context.valid.yaml")
    selected["config_sha256"] = effective.effective_sha256
    selected["instrument_registry_sha256"] = effective.instrument_registry.source_sha256
    selected["generated_at_utc"] = now_utc.isoformat().replace("+00:00", "Z")
    selected["as_of_utc"] = now_utc.isoformat().replace("+00:00", "Z")
    return DecisionContractBinder(effective).bind_context(selected, now_utc=now_utc)


def _risk(
    context: BoundDecisionContext,
    *,
    entry_allowed: bool = True,
    positions: list[dict[str, object]] | None = None,
) -> SnapshotReadResult:
    document = context.document
    account = cast(dict[str, object], document["account"])
    state = "ENTRY_ALLOWED" if entry_allowed else "CLOSE_ONLY"
    snapshot = {
        "snapshot_id": document["risk_snapshot_id"],
        "accounting": {
            "nav": account["nav"],
            "positions": positions or [],
        },
        "open_orders": deepcopy(document["open_orders"]),
        "exposure": {
            "available_balance_quote": account["spot_available_quote"],
            "available_margin_quote": account["futures_available_margin"],
        },
        "decision": {"state": state},
    }
    return SnapshotReadResult(
        snapshot=snapshot,
        entry_allowed=entry_allowed,
        close_only=not entry_allowed,
        kill_switch=False,
        safe_exit_allowed=True,
        reason_codes=("risk_checks_passed" if entry_allowed else "daily_loss_limit_reached",),
    )


def _process(
    store: ProposalStore,
    proposal_id: str,
    effective: EffectiveConfig,
    context: BoundDecisionContext,
    risk: SnapshotReadResult,
    *,
    now_utc: datetime = NOW + timedelta(seconds=3),
    control_reader: Callable[[], OperationalControlSnapshot] | None = None,
):
    return RevalidationCoordinator(
        store,
        ActionRevalidator(effective, control_reader=control_reader),
    ).process(
        proposal_id,
        context,
        risk,
        now_utc=now_utc,
    )


def test_operational_entry_stop_cancels_risk_increase_before_execution(tmp_path: Path) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    store, proposal_id = _approved(tmp_path, effective)
    context = _bound_context(effective)
    stopped = OperationalControlSnapshot(entry_stopped=True)

    outcome = _process(
        store,
        proposal_id,
        effective,
        context,
        _risk(context),
        control_reader=lambda: stopped,
    )

    assert outcome.proposal.state is ProposalState.CANCELLED
    assert outcome.proposal.document["execution"] is None
    assert outcome.report is not None
    assert RevalidationReasonCode.OPERATIONAL_ENTRY_STOPPED in outcome.report.reason_codes
    store.close()


def test_unreadable_operational_control_cancels_risk_increase(tmp_path: Path) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    store, proposal_id = _approved(tmp_path, effective)
    context = _bound_context(effective)

    def unavailable() -> OperationalControlSnapshot:
        raise RuntimeError("sensitive control storage detail")

    outcome = _process(
        store,
        proposal_id,
        effective,
        context,
        _risk(context),
        control_reader=unavailable,
    )

    assert outcome.proposal.state is ProposalState.CANCELLED
    assert outcome.report is not None
    assert RevalidationReasonCode.OPERATIONAL_CONTROL_UNAVAILABLE in outcome.report.reason_codes
    assert "sensitive control storage detail" not in json.dumps(outcome.report.to_safe_dict())
    store.close()


def test_approved_action_revalidates_and_queues_exactly_once(tmp_path: Path) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    store, proposal_id = _approved(tmp_path, effective)
    context = _bound_context(effective)

    first = _process(store, proposal_id, effective, context, _risk(context))
    repeated = _process(store, proposal_id, effective, context, _risk(context))

    assert first.report is not None and first.report.passed
    assert first.proposal.state is ProposalState.QUEUED
    assert not first.replayed
    assert repeated.replayed
    assert repeated.proposal.record_sha256 == first.proposal.record_sha256
    execution = first.proposal.document["execution"]
    assert execution["risk_snapshot_id"] == context.document["risk_snapshot_id"]
    assert execution["context_sha256"] == context.sha256
    assert execution["order_ids"] == []
    assert [event["event_type"] for event in first.proposal.document["events"]][-2:] == [
        "REVALIDATION_STARTED",
        "EXECUTION_QUEUED",
    ]
    store.close()


def test_price_outside_approved_range_cancels_without_execution(tmp_path: Path) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    store, proposal_id = _approved(tmp_path, effective)
    document = _yaml("decision-context.valid.yaml")
    cast(dict[str, object], cast(list[dict[str, object]], document["instruments"])[0]["futures"])[
        "mark_price"
    ] = "157.00"
    context = _bound_context(effective, document)

    outcome = _process(store, proposal_id, effective, context, _risk(context))

    assert outcome.proposal.state is ProposalState.CANCELLED
    assert outcome.proposal.document["execution"] is None
    assert outcome.report is not None
    assert RevalidationReasonCode.PRICE_OUTSIDE_APPROVED_RANGE in outcome.report.reason_codes
    store.close()


def test_position_balance_and_open_order_are_rechecked(tmp_path: Path) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    store, proposal_id = _approved(tmp_path, effective)
    document = _yaml("decision-context.valid.yaml")
    account = cast(dict[str, object], document["account"])
    account["futures_available_margin"] = "1"
    futures = cast(
        dict[str, object],
        cast(list[dict[str, object]], document["instruments"])[0]["futures"],
    )
    futures["position"] = {
        "position_id": "position-sol-long",
        "side": "long",
        "quantity": "1",
        "entry_price": "150",
        "leverage": "2",
        "liquidation_price": "75",
        "unrealized_pnl": "6",
        "stop_loss": "150",
        "take_profit": "165",
    }
    order = {
        "order_id": "order-sol-open",
        "instrument_id": "SOL",
        "market": "linear_perpetual",
        "side": "long",
        "intent": "OPEN",
        "price": "156.00",
        "quantity": "1",
        "filled_quantity": "0",
        "reduce_only": False,
        "status": "NEW",
    }
    cast(list[dict[str, object]], document["open_orders"]).append(order)
    context = _bound_context(effective, document)
    risk = _risk(
        context,
        positions=[{"instrument_id": "SOL", "market": "futures", "side": "long"}],
    )

    outcome = _process(store, proposal_id, effective, context, risk)

    assert outcome.report is not None
    assert outcome.report.reason_codes == (
        RevalidationReasonCode.POSITION_ALREADY_EXISTS,
        RevalidationReasonCode.AVAILABLE_BALANCE_INSUFFICIENT,
        RevalidationReasonCode.OPEN_ORDER_CONFLICT,
    )
    assert outcome.proposal.state is ProposalState.CANCELLED
    store.close()


def test_risk_and_context_snapshot_mismatch_fail_closed(tmp_path: Path) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    store, proposal_id = _approved(tmp_path, effective)
    document = _yaml("decision-context.valid.yaml")
    account = cast(dict[str, object], document["account"])
    account["risk_state"] = "CLOSE_ONLY"
    document["allowed_actions"] = ["HOLD", "REDUCE", "CLOSE", "CANCEL_ORDER"]
    context = _bound_context(effective, document)
    risk = _risk(context, entry_allowed=False)
    assert risk.snapshot is not None
    risk.snapshot["snapshot_id"] = "risk-20260718T120001Z-deadbeef0001"

    outcome = _process(store, proposal_id, effective, context, risk)

    assert outcome.report is not None
    assert outcome.report.reason_codes == (
        RevalidationReasonCode.SNAPSHOT_CONTEXT_MISMATCH,
        RevalidationReasonCode.ACTION_NOT_ALLOWED,
        RevalidationReasonCode.RISK_ENTRY_BLOCKED,
    )
    assert outcome.proposal.state is ProposalState.CANCELLED
    store.close()


def test_current_market_leverage_and_price_tick_rules_are_rechecked(tmp_path: Path) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    decision = _yaml("model-decision.valid.yaml")
    action = cast(list[dict[str, object]], decision["actions"])[0]
    action["requested_leverage"] = "3"
    action["stop_loss"] = "150.001"
    store, proposal_id = _approved(tmp_path, effective, decision)
    context = _bound_context(effective)

    outcome = _process(store, proposal_id, effective, context, _risk(context))

    assert outcome.report is not None
    assert outcome.report.reason_codes == (
        RevalidationReasonCode.LEVERAGE_OUT_OF_RANGE,
        RevalidationReasonCode.PRICE_NOT_TICK_ALIGNED,
    )
    assert outcome.proposal.state is ProposalState.CANCELLED
    store.close()


def test_missing_risk_snapshot_and_expired_proposal_never_queue(tmp_path: Path) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    store, proposal_id = _approved(tmp_path, effective)
    expired_at = RECORDED_AT + timedelta(minutes=10)
    context = _bound_context(effective, now_utc=expired_at)
    unavailable = SnapshotReadResult(
        snapshot=None,
        entry_allowed=False,
        close_only=True,
        kill_switch=False,
        safe_exit_allowed=True,
        reason_codes=("snapshot_missing",),
    )

    outcome = _process(
        store,
        proposal_id,
        effective,
        context,
        unavailable,
        now_utc=expired_at,
    )

    assert outcome.report is not None
    assert outcome.report.reason_codes == (
        RevalidationReasonCode.PROPOSAL_EXPIRED,
        RevalidationReasonCode.RISK_SNAPSHOT_UNAVAILABLE,
    )
    assert outcome.proposal.state is ProposalState.EXPIRED
    assert outcome.proposal.document["execution"] is None
    store.close()


def test_existing_r3_01_sqlite_store_is_migrated_without_a_second_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-proposals.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE proposal (
            proposal_id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            action_id TEXT NOT NULL UNIQUE,
            source_record_sha256 TEXT NOT NULL,
            action_sha256 TEXT NOT NULL,
            action_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN (
                    'DRAFT', 'VALIDATED', 'PENDING_APPROVAL',
                    'APPROVED', 'REJECTED', 'EXPIRED'
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
        CREATE TABLE proposal_event (
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
    )
    connection.close()

    effective = load_effective_config(PROJECT_ROOT, environ={})
    ProposalStore(effective, path).close()
    migrated = sqlite3.connect(path)
    sql = migrated.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'proposal'"
    ).fetchone()[0]
    foreign_key = migrated.execute("PRAGMA foreign_key_list(proposal_event)").fetchone()
    migrated.close()

    assert "REVALIDATING" in sql
    assert "QUEUED" in sql
    assert foreign_key[2] == "proposal"
