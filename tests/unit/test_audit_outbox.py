import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest
import yaml

from alphamind.audit import (
    AuditBackpressureError,
    AuditExecutionContext,
    AuditOutbox,
    AuditProvenance,
    AuditWriter,
    ClaimedEvent,
    DeliveryResult,
    OutboxLimits,
    SQLiteAuditSink,
    build_risk_decision_event,
    canonical_json_bytes,
)

PROJECT_ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)


def audit_event(sequence: int = 0, *, event_id: uuid.UUID | None = None) -> dict[str, object]:
    return build_risk_decision_event(
        event_id=event_id or uuid.uuid4(),
        producer_instance_id="freqtrade-test",
        producer_sequence=sequence,
        occurred_at=NOW,
        recorded_at=NOW,
        execution_context=AuditExecutionContext(
            "dry_run", "freqtrade_dry_run", "dry_run", False, False
        ),
        provenance=AuditProvenance(
            "1" * 40,
            "donchian_trend",
            "0.3.0",
            "2" * 64,
            "3" * 64,
        ),
        risk_snapshot_id="risk-20260717T080000Z-abcdef123456",
        pair="BTC/USDT",
        approved_quantity=Decimal("0.001"),
        approved_stake=Decimal("100"),
        reference_rate=Decimal("100000"),
        limiting_cap="risk_budget",
    )


def test_risk_decision_event_has_valid_schema_and_hashes(tmp_path: Path) -> None:
    schema = yaml.safe_load(
        (PROJECT_ROOT / "data/schemas/audit-event.schema.yaml").read_text(encoding="utf-8")
    )
    event = audit_event()

    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.validate(event)
    payload_hash = hashlib.sha256(canonical_json_bytes(event["payload"])).hexdigest()
    content_view = dict(event)
    content_view.pop("event_content_sha256")
    content_hash = hashlib.sha256(canonical_json_bytes(content_view)).hexdigest()
    assert event["payload_sha256"] == payload_hash
    assert event["event_content_sha256"] == content_hash
    assert event["runtime_authority"] is False
    assert event["contains_secrets"] is False
    assert len(canonical_json_bytes(event)) < 16 * 1024

    tampered = dict(event)
    tampered["payload"] = {"decision": "tampered"}
    outbox = AuditOutbox(tmp_path / "tampered-audit.sqlite")
    with pytest.raises(ValueError, match="payload hash mismatch"):
        outbox.append(tampered, now=NOW)
    outbox.close()


def test_outbox_enforces_wal_full_sync_idempotency_and_reserved_capacity(tmp_path: Path) -> None:
    limits = OutboxLimits(logical_capacity=4, entry_stop_pending=2)
    outbox = AuditOutbox(tmp_path / "outbox.sqlite", limits=limits)
    first = audit_event(0)
    second = audit_event(1)
    assert outbox.append(first, now=NOW) is True
    assert outbox.append(first, now=NOW) is False
    assert outbox.append(second, now=NOW) is True

    with pytest.raises(AuditBackpressureError, match="entry stop threshold"):
        outbox.append(audit_event(2), now=NOW)

    # 冻结的最后容量只能给 exit/Kill/reconcile/operator 等安全事件。
    assert outbox.append(audit_event(2), event_class="SAFETY", now=NOW) is True
    assert outbox.append(audit_event(3), event_class="SAFETY", now=NOW) is True
    with pytest.raises(AuditBackpressureError, match="logical capacity"):
        outbox.append(audit_event(4), event_class="SAFETY", now=NOW)
    # outbox 已满时，相同 ID+hash 的重放仍是零增长幂等成功。
    assert outbox.append(first, now=NOW) is False

    connection = sqlite3.connect(tmp_path / "outbox.sqlite")
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    connection.close()
    outbox.close()


def test_outbox_oldest_age_threshold_fails_entry_closed(tmp_path: Path) -> None:
    limits = OutboxLimits(
        logical_capacity=4,
        entry_stop_pending=3,
        entry_stop_oldest_seconds=1,
    )
    outbox = AuditOutbox(tmp_path / "outbox.sqlite", limits=limits)
    outbox.append(audit_event(0), now=NOW)

    metrics = outbox.metrics(now=NOW + timedelta(seconds=2))
    assert metrics.entry_backpressure is True
    with pytest.raises(AuditBackpressureError, match="entry stop threshold"):
        outbox.append(audit_event(1), now=NOW + timedelta(seconds=2))
    outbox.close()


def test_writer_is_idempotent_and_audit_db_has_no_runtime_tables(tmp_path: Path) -> None:
    outbox = AuditOutbox(tmp_path / "outbox.sqlite")
    sink = SQLiteAuditSink(tmp_path / "audit.sqlite")
    event = audit_event()
    outbox.append(event, now=NOW)
    writer = AuditWriter(outbox, sink)

    result = writer.run_once(now=NOW)
    assert (result.claimed, result.delivered, result.retried) == (1, 1, 0)
    assert sink.count() == 1
    duplicate = ClaimedEvent(
        str(event["event_id"]),
        str(event["event_content_sha256"]),
        json.dumps(event),
        0,
    )
    assert (
        sink.deliver(duplicate, persisted_at_utc="2026-07-17T08:01:00Z")
        is DeliveryResult.DUPLICATE_SAME_CONTENT
    )
    assert sink.count() == 1

    tables = {
        row[0]
        for row in sqlite3.connect(tmp_path / "audit.sqlite")
        .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        .fetchall()
    }
    assert tables == {"audit_event"}

    cleanup = writer.run_once(now=NOW + timedelta(days=8))
    assert cleanup.cleaned == 1
    with pytest.raises(KeyError):
        outbox.state_for_test(str(event["event_id"]))
    sink.close()
    outbox.close()


def test_writer_restart_reclaims_expired_lease_and_recovers_sink_outage(tmp_path: Path) -> None:
    outbox_path = tmp_path / "outbox.sqlite"
    audit_path = tmp_path / "audit.sqlite"
    outbox = AuditOutbox(outbox_path)
    first = audit_event(0)
    outbox.append(first, now=NOW)
    assert len(outbox.claim_batch(now=NOW)) == 1
    outbox.close()

    restarted = AuditOutbox(outbox_path)
    assert restarted.claim_batch(now=NOW + timedelta(seconds=59)) == []
    reclaimed = restarted.claim_batch(now=NOW + timedelta(seconds=61))
    assert [event.event_id for event in reclaimed] == [first["event_id"]]
    restarted.mark_failed(
        str(first["event_id"]),
        now=NOW + timedelta(seconds=61),
        error_class="simulated_restart",
        retry_at=NOW + timedelta(seconds=62),
    )

    second = audit_event(1)
    restarted.append(second, now=NOW + timedelta(seconds=62))
    unavailable_sink = SQLiteAuditSink(audit_path)
    unavailable_sink.close()
    failed_run = AuditWriter(restarted, unavailable_sink).run_once(now=NOW + timedelta(seconds=62))
    assert failed_run.retried == 2
    assert restarted.state_for_test(str(second["event_id"]))["state"] == "PENDING"

    recovered_sink = SQLiteAuditSink(audit_path)
    recovered = AuditWriter(restarted, recovered_sink).run_once(now=NOW + timedelta(seconds=65))
    assert recovered.delivered == 2
    assert recovered_sink.count() == 2
    recovered_sink.close()
    restarted.close()


def test_content_conflict_and_twenty_failures_go_to_dead_letter(tmp_path: Path) -> None:
    outbox = AuditOutbox(tmp_path / "outbox.sqlite")
    sink = SQLiteAuditSink(tmp_path / "audit.sqlite")
    conflict = audit_event(0)
    outbox.append(conflict, now=NOW)
    foreign = ClaimedEvent(str(conflict["event_id"]), "f" * 64, "{}", 0)
    assert sink.deliver(foreign, persisted_at_utc="2026-07-17T08:00:00Z") is DeliveryResult.INSERTED
    conflict_result = AuditWriter(outbox, sink).run_once(now=NOW)
    assert conflict_result.dead_lettered == 1
    assert outbox.state_for_test(str(conflict["event_id"]))["state"] == "DEAD_LETTER"

    repeatedly_failing = audit_event(1)
    outbox.append(repeatedly_failing, now=NOW)
    sink.close()
    writer = AuditWriter(outbox, sink)
    current = NOW
    for _ in range(20):
        result = writer.run_once(now=current)
        assert result.claimed == 1
        current += timedelta(seconds=61)
    state = outbox.state_for_test(str(repeatedly_failing["event_id"]))
    assert state["state"] == "DEAD_LETTER"
    assert state["attempt_count"] == 20
    assert len(outbox.attempts_for_test(str(repeatedly_failing["event_id"]))) == 20
    outbox.close()
