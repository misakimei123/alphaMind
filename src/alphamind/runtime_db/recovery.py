"""冻结恢复顺序的 fail-closed 决策，不创建第二套订单状态机。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from alphamind.runtime_db.sqlite import RuntimeDatabaseInspection


class RecoveryPhase(StrEnum):
    EXCHANGE_FACTS_REQUIRED = "exchange_facts_required"
    RUNTIME_RESTORE_REQUIRED = "runtime_restore_required"
    FREQTRADE_RECONCILE_REQUIRED = "freqtrade_reconcile_required"
    SAFE_DISPOSITION = "safe_disposition"
    RUNTIME_READY_AUDIT_DEGRADED = "runtime_ready_audit_degraded"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    phase: RecoveryPhase
    entry_allowed: bool
    safe_exit_allowed: bool
    audit_backfill_allowed: bool
    alert_required: bool
    reason_codes: tuple[str, ...]


def evaluate_recovery(
    inspection: RuntimeDatabaseInspection,
    *,
    exchange_facts_available: bool,
    freqtrade_reconciled: bool,
    safe_disposition_complete: bool,
    audit_available: bool,
) -> RecoveryDecision:
    """强制 Exchange -> Runtime DB -> reconcile -> safe disposition -> Audit 顺序。"""

    if not exchange_facts_available:
        return RecoveryDecision(
            RecoveryPhase.EXCHANGE_FACTS_REQUIRED,
            False,
            False,
            False,
            True,
            ("exchange_facts_unavailable",),
        )
    if not inspection.healthy:
        return RecoveryDecision(
            RecoveryPhase.RUNTIME_RESTORE_REQUIRED,
            False,
            False,
            False,
            True,
            inspection.reason_codes,
        )
    if not freqtrade_reconciled:
        return RecoveryDecision(
            RecoveryPhase.FREQTRADE_RECONCILE_REQUIRED,
            False,
            True,
            False,
            True,
            ("runtime_exchange_reconcile_required",),
        )
    if not safe_disposition_complete:
        return RecoveryDecision(
            RecoveryPhase.SAFE_DISPOSITION,
            False,
            True,
            False,
            True,
            ("safe_disposition_pending",),
        )
    if not audit_available:
        # Audit DB 不得反向阻塞止损或恢复；outbox 背压仍由 P3-03 独立限制新风险。
        return RecoveryDecision(
            RecoveryPhase.RUNTIME_READY_AUDIT_DEGRADED,
            True,
            True,
            False,
            True,
            ("audit_backfill_pending",),
        )
    return RecoveryDecision(RecoveryPhase.READY, True, True, True, False, ())
