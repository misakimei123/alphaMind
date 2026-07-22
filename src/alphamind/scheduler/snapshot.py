"""从有效配置、市场能力和 RiskSnapshot 构建只读周期观测。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from alphamind.config import EffectiveConfig, MarketKind
from alphamind.operations import OperationalControlStore
from alphamind.risk import SnapshotReadResult, load_risk_snapshot
from alphamind.scheduler.core import CycleInvocation, JsonObject


def build_read_only_snapshot_handler(
    effective: EffectiveConfig,
) -> Callable[[CycleInvocation], JsonObject]:
    capability = effective.market_capability_snapshot
    registry = effective.instrument_registry
    runtime = effective.runtime
    project_root = effective.project_root
    spot_pairs = capability.available_pairs(MarketKind.SPOT)
    futures_pairs = capability.available_pairs(MarketKind.FUTURES)
    maximum_futures_leverage: dict[str, Decimal] = {}
    for pair in futures_pairs:
        item = capability.capability_for_pair(pair, MarketKind.FUTURES)
        if item is not None and item.effective_max_leverage is not None:
            maximum_futures_leverage[pair] = item.effective_max_leverage

    def observe(invocation: CycleInvocation) -> JsonObject:
        control_path = project_root / runtime["operations"]["control_store_path"]
        with OperationalControlStore(control_path) as control_store:
            operational_control = control_store.current()
        risk_path = project_root / runtime["risk"]["snapshot_path"]
        risk = load_risk_snapshot(
            risk_path,
            now_utc=invocation.started_at_utc,
            allowed_pairs=spot_pairs,
            allowed_futures_pairs=futures_pairs,
            expected_registry_sha256=registry.source_sha256,
            expected_capability_sha256=capability.source_sha256,
            maximum_futures_leverage=maximum_futures_leverage,
        )
        if risk.snapshot is not None:
            generated_at = datetime.fromisoformat(
                str(risk.snapshot["generated_at_utc"]).replace("Z", "+00:00")
            )
            maximum_age = timedelta(seconds=runtime["risk"]["risk_snapshot_max_age_seconds"])
            if invocation.started_at_utc - generated_at > maximum_age:
                risk = SnapshotReadResult(
                    snapshot=None,
                    entry_allowed=False,
                    close_only=True,
                    kill_switch=False,
                    safe_exit_allowed=True,
                    reason_codes=("snapshot_stale_for_cycle",),
                )
        instances: dict[str, dict[str, Any]] = {}
        for market in (MarketKind.SPOT, MarketKind.FUTURES):
            section = runtime["execution"][market.value]
            instances[market.value] = {
                "enabled": section["enabled"],
                "bot_identity": section["bot_identity"],
                "freqtrade_config_path": section["freqtrade_config_path"],
                "runtime_db_path": section["runtime_db_path"],
            }
        effective_entry_allowed = risk.entry_allowed and not operational_control.entry_stopped
        effective_kill_switch = risk.kill_switch or operational_control.emergency
        effective_close_only = risk.close_only or operational_control.entry_stopped
        risk_reason_codes = list(risk.reason_codes)
        if operational_control.emergency:
            risk_reason_codes.append("operational_emergency")
        elif operational_control.entry_stopped:
            risk_reason_codes.append("operational_entry_stopped")
        return {
            "read_only": True,
            "environment": runtime["environment"],
            "execution_ready": effective.execution_ready,
            "operational_control": operational_control.to_safe_dict(),
            "market_capability": {
                "snapshot_id": capability.snapshot_id,
                "source_sha256": capability.source_sha256,
                "fetched_at_utc": capability.fetched_at_utc.isoformat().replace("+00:00", "Z"),
                "available_spot_pairs": list(spot_pairs),
                "available_futures_pairs": list(futures_pairs),
                "instruments": [item.to_dict() for item in capability.instruments],
            },
            "runtime_instances": instances,
            "risk_snapshot": {
                "path": str(Path(runtime["risk"]["snapshot_path"])),
                "available": risk.snapshot is not None,
                "entry_allowed": effective_entry_allowed,
                "close_only": effective_close_only,
                "kill_switch": effective_kill_switch,
                "safe_exit_allowed": risk.safe_exit_allowed,
                "reason_codes": risk_reason_codes,
                "snapshot": risk.snapshot,
            },
            "deferred_to_later_stages": [
                "news_collection",
                "model_decision",
                "telegram_approval",
                "trade_execution",
            ],
        }

    return observe
