from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphamind.ai import (
    DecisionJournal,
    DecisionJournalEntry,
    DecisionJournalError,
    DecisionOutcome,
)

NOW = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)
CYCLE_ID = "cycle-20260721T083000Z-0123abcd"


def _sha256(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decision(action: str = "HOLD") -> dict[str, object]:
    return {
        "schema_version": 1,
        "cycle_id": CYCLE_ID,
        "actions": [
            {
                "schema_version": 2,
                "cycle_id": CYCLE_ID,
                "action_id": "act-20260721T083000Z-0123456789ab",
                "action": action,
            }
        ],
        "decision_summary": "Frozen test decision.",
        "global_risks": ["Fixture only."],
    }


def _entry(
    *,
    outcome: DecisionOutcome = DecisionOutcome.HOLD,
    action: str = "HOLD",
    error_code: str | None = None,
) -> DecisionJournalEntry:
    decision = None if outcome is DecisionOutcome.MODEL_ERROR else _decision(action)
    return DecisionJournalEntry(
        cycle_id=CYCLE_ID,
        recorded_at_utc=NOW,
        outcome=outcome,
        environment="dry_run",
        profile_id="openai_terra_trade_decision_v2",
        model_id="gpt-5.6-terra",
        prompt_id="alphamind_trade_decision",
        prompt_version=2,
        prompt_sha256="a" * 64,
        config_sha256="b" * 64,
        input_sha256="c" * 64,
        schema_versions={
            "decision-context.schema.yaml": 2,
            "model-decision.schema.yaml": 1,
            "trade-action.schema.yaml": 2,
        },
        decision_sha256=_sha256(decision) if decision is not None else None,
        decision=decision,
        error_code=error_code,
        response_id="resp_fixture",
        request_id="req_fixture",
        validation={"accepted_action_ids": []},
        usage={"attempts": 1, "accounted_cost_usd": "0.001000000"},
    )


def test_journal_persists_hold_with_complete_version_and_hash_binding(tmp_path: Path) -> None:
    journal = DecisionJournal(tmp_path / "ai-decisions.sqlite")

    assert journal.append(_entry()) is True
    stored = journal.get(CYCLE_ID)

    assert stored is not None
    assert stored.outcome is DecisionOutcome.HOLD
    assert stored.input_sha256 == "c" * 64
    assert stored.document["model"] == {
        "model_id": "gpt-5.6-terra",
        "profile_id": "openai_terra_trade_decision_v2",
    }
    assert stored.document["prompt"]["version"] == 2
    assert stored.document["schema_versions"]["decision-context.schema.yaml"] == 2
    assert stored.document["runtime_authority"] is False
    assert stored.document["contains_secrets"] is False


def test_journal_persists_candidates_and_safe_model_errors(tmp_path: Path) -> None:
    journal = DecisionJournal(tmp_path / "candidates.sqlite")
    candidate = _entry(outcome=DecisionOutcome.CANDIDATE_ACTIONS, action="OPEN")
    error = replace(
        _entry(outcome=DecisionOutcome.MODEL_ERROR, error_code="timeout"),
        cycle_id="cycle-20260721T090000Z-fedcba98",
        response_id=None,
        request_id=None,
        validation=None,
        usage={"attempts": 2, "accounted_cost_usd": "0.002000000"},
    )

    journal.append(candidate)
    journal.append(error)

    recent = journal.recent()
    assert [record.outcome for record in recent] == [
        DecisionOutcome.MODEL_ERROR,
        DecisionOutcome.CANDIDATE_ACTIONS,
    ]
    error_document = recent[0].document
    assert error_document["decision"] is None
    assert error_document["error_code"] == "timeout"
    assert "exception" not in json.dumps(error_document).lower()


def test_journal_is_idempotent_but_rejects_cycle_overwrite(tmp_path: Path) -> None:
    journal = DecisionJournal(tmp_path / "immutable.sqlite")
    entry = _entry()

    assert journal.append(entry) is True
    assert journal.append(entry) is False

    with pytest.raises(DecisionJournalError, match="content conflict"):
        journal.append(_entry(outcome=DecisionOutcome.CANDIDATE_ACTIONS, action="OPEN"))


def test_journal_detects_tampered_content_on_read(tmp_path: Path) -> None:
    path = tmp_path / "tampered.sqlite"
    journal = DecisionJournal(path)
    journal.append(_entry())
    journal.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE ai_decision_journal SET record_json = ? WHERE cycle_id = ?",
            ("{}", CYCLE_ID),
        )

    restarted = DecisionJournal(path)
    with pytest.raises(DecisionJournalError, match="content hash mismatch"):
        restarted.get(CYCLE_ID)


def test_journal_rejects_mislabeled_or_unbound_records(tmp_path: Path) -> None:
    journal = DecisionJournal(tmp_path / "invalid.sqlite")

    with pytest.raises(DecisionJournalError, match="outcome does not match"):
        journal.append(replace(_entry(), outcome=DecisionOutcome.CANDIDATE_ACTIONS))
    with pytest.raises(DecisionJournalError, match="hash binding"):
        journal.append(replace(_entry(), input_sha256="not-a-hash"))
