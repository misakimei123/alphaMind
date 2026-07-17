"""构造满足 ADR-0007 的不可变 AuditEvent envelope。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

MAX_EVENT_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class AuditExecutionContext:
    environment: str
    evidence_layer: str
    credentials_profile: str
    trade_write_permitted: bool
    production_write_path_verified: bool


@dataclass(frozen=True, slots=True)
class AuditProvenance:
    project_commit: str
    strategy_id: str
    strategy_version: str
    strategy_config_sha256: str
    runtime_lock_sha256: str


def canonical_json_bytes(value: object) -> bytes:
    """返回项目事件子集的 JCS 表示；事件只使用整数、字符串、布尔和容器。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("audit decimal values must be finite")
    return format(value, "f")


def build_risk_decision_event(
    *,
    event_id: uuid.UUID,
    producer_instance_id: str,
    producer_sequence: int,
    occurred_at: datetime,
    recorded_at: datetime,
    execution_context: AuditExecutionContext,
    provenance: AuditProvenance,
    risk_snapshot_id: str,
    pair: str,
    approved_quantity: Decimal,
    approved_stake: Decimal,
    reference_rate: Decimal,
    limiting_cap: str,
) -> dict[str, Any]:
    """构造已批准的风险决策；该事实不代表交易所已接受或成交订单。"""

    if producer_sequence < 0:
        raise ValueError("producer_sequence must be nonnegative")
    if recorded_at < occurred_at:
        recorded_at = occurred_at
    payload: dict[str, object] = {
        "decision": "approved",
        "pair": pair,
        "side": "long",
        "approved_quantity": _decimal_text(approved_quantity),
        "approved_stake": _decimal_text(approved_stake),
        "reference_rate": _decimal_text(reference_rate),
        "limiting_cap": limiting_cap,
        # 该事件只证明风险层批准，不声明 Runtime DB、订单或成交事实。
        "order_submitted": False,
    }
    event: dict[str, Any] = {
        "schema_version": 1,
        "event_id": str(event_id),
        "event_type": "risk_decision",
        "event_version": 1,
        "occurred_at_utc": _utc_text(occurred_at),
        "recorded_at_utc": _utc_text(recorded_at),
        "producer": {
            "component": "freqtrade_strategy",
            "instance_id": producer_instance_id,
            "sequence": producer_sequence,
        },
        "execution_context": {
            "environment": execution_context.environment,
            "evidence_layer": execution_context.evidence_layer,
            "credentials_profile": execution_context.credentials_profile,
            "trade_write_permitted": execution_context.trade_write_permitted,
            "production_write_path_verified": execution_context.production_write_path_verified,
        },
        "provenance": {
            "project_commit": provenance.project_commit,
            "strategy_id": provenance.strategy_id,
            "strategy_version": provenance.strategy_version,
            "strategy_config_sha256": provenance.strategy_config_sha256,
            "runtime_lock_sha256": provenance.runtime_lock_sha256,
            "risk_snapshot_id": risk_snapshot_id,
            "experiment_id": None,
        },
        "runtime_links": [],
        "reason_codes": ["risk_approved", f"limited_by_{limiting_cap}"],
        "payload_schema": "alphamind/audit/risk-decision/v1",
        "payload": payload,
        "payload_sha256": _sha256(payload),
        "event_content_sha256": "",
        "runtime_authority": False,
        "contains_secrets": False,
    }
    content_view = dict(event)
    content_view.pop("event_content_sha256")
    event["event_content_sha256"] = _sha256(content_view)
    if len(canonical_json_bytes(event)) > MAX_EVENT_BYTES:
        raise ValueError("canonical audit event exceeds 16 KiB")
    return event
