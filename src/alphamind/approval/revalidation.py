"""R3-04：已批准 Action 的执行前确定性重新校验与一次性排队。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from alphamind.approval.store import ProposalState, ProposalStore, StoredProposal
from alphamind.config import EffectiveConfig, MarketKind
from alphamind.decision import BoundDecisionContext
from alphamind.market import MarketCapability
from alphamind.risk import SnapshotReadResult

JsonObject = dict[str, Any]


class RevalidationReasonCode(StrEnum):
    PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
    RISK_SNAPSHOT_UNAVAILABLE = "RISK_SNAPSHOT_UNAVAILABLE"
    SNAPSHOT_CONTEXT_MISMATCH = "SNAPSHOT_CONTEXT_MISMATCH"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    RISK_ENTRY_BLOCKED = "RISK_ENTRY_BLOCKED"
    OPERATIONAL_ENTRY_STOPPED = "OPERATIONAL_ENTRY_STOPPED"
    OPERATIONAL_CONTROL_UNAVAILABLE = "OPERATIONAL_CONTROL_UNAVAILABLE"
    SAFE_EXIT_BLOCKED = "SAFE_EXIT_BLOCKED"
    MARKET_RULES_UNAVAILABLE = "MARKET_RULES_UNAVAILABLE"
    LEVERAGE_OUT_OF_RANGE = "LEVERAGE_OUT_OF_RANGE"
    PRICE_OUTSIDE_APPROVED_RANGE = "PRICE_OUTSIDE_APPROVED_RANGE"
    PRICE_NOT_TICK_ALIGNED = "PRICE_NOT_TICK_ALIGNED"
    POSITION_ALREADY_EXISTS = "POSITION_ALREADY_EXISTS"
    POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
    POSITION_SIDE_MISMATCH = "POSITION_SIDE_MISMATCH"
    AVAILABLE_BALANCE_INSUFFICIENT = "AVAILABLE_BALANCE_INSUFFICIENT"
    OPEN_ORDER_CONFLICT = "OPEN_ORDER_CONFLICT"
    TARGET_REFERENCE_MISMATCH = "TARGET_REFERENCE_MISMATCH"
    PROTECTION_WOULD_LOOSEN = "PROTECTION_WOULD_LOOSEN"
    REVALIDATION_INPUT_INVALID = "REVALIDATION_INPUT_INVALID"


@dataclass(frozen=True, slots=True)
class RevalidationReport:
    proposal_id: str
    action_id: str
    context_sha256: str
    risk_snapshot_id: str | None
    market_capability_snapshot_sha256: str
    reason_codes: tuple[RevalidationReasonCode, ...]

    @property
    def passed(self) -> bool:
        return not self.reason_codes

    def to_safe_dict(self) -> JsonObject:
        return {
            "proposal_id": self.proposal_id,
            "action_id": self.action_id,
            "context_sha256": self.context_sha256,
            "risk_snapshot_id": self.risk_snapshot_id,
            "market_capability_snapshot_sha256": self.market_capability_snapshot_sha256,
            "passed": self.passed,
            "reason_codes": [code.value for code in self.reason_codes],
        }


@dataclass(frozen=True, slots=True)
class RevalidationOutcome:
    proposal: StoredProposal
    report: RevalidationReport | None
    replayed: bool


def _decimal(value: object) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("revalidation decimal must be finite")
    return parsed


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("revalidation timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("revalidation timestamp must use UTC")
    return parsed


def _market_name(action: JsonObject) -> tuple[MarketKind, str]:
    if action["market"] == "spot":
        return MarketKind.SPOT, "spot"
    return MarketKind.FUTURES, "futures"


def _order_market(value: object) -> str:
    return "linear_perpetual" if value == "futures" else str(value)


class OperationalControlView(Protocol):
    entry_stopped: bool
    emergency: bool


class ActionRevalidator:
    """重新读取的可信快照必须全部一致；任何缺失或漂移均 fail-closed。"""

    def __init__(
        self,
        effective: EffectiveConfig,
        *,
        control_reader: Callable[[], OperationalControlView] | None = None,
    ) -> None:
        self.effective = effective
        self.registry = effective.instrument_registry
        self.capabilities = effective.market_capability_snapshot
        self.control_reader = control_reader

    def revalidate(
        self,
        proposal: StoredProposal,
        context: BoundDecisionContext,
        risk: SnapshotReadResult,
        *,
        now_utc: datetime,
    ) -> RevalidationReport:
        if now_utc.tzinfo is None or now_utc.utcoffset() != timedelta(0):
            raise ValueError("now_utc must use UTC")
        if proposal.state is not ProposalState.REVALIDATING:
            raise ValueError("proposal must be REVALIDATING")

        reasons: set[RevalidationReasonCode] = set()
        document = proposal.document
        action = document["action"]
        risk_snapshot_id: str | None = None
        if now_utc >= _parse_utc(document["expires_at_utc"]):
            reasons.add(RevalidationReasonCode.PROPOSAL_EXPIRED)
        if risk.snapshot is None:
            reasons.add(RevalidationReasonCode.RISK_SNAPSHOT_UNAVAILABLE)
        else:
            raw_snapshot_id = risk.snapshot.get("snapshot_id")
            risk_snapshot_id = str(raw_snapshot_id) if isinstance(raw_snapshot_id, str) else None

        try:
            if not isinstance(action, dict):
                raise TypeError("proposal action must be an object")
            self._evaluate(
                action,
                context=context,
                risk=risk,
                reasons=reasons,
            )
        except (ArithmeticError, InvalidOperation, KeyError, TypeError, ValueError):
            reasons.add(RevalidationReasonCode.REVALIDATION_INPUT_INVALID)

        ordered = tuple(code for code in RevalidationReasonCode if code in reasons)
        return RevalidationReport(
            proposal_id=proposal.proposal_id,
            action_id=proposal.action_id,
            context_sha256=context.sha256,
            risk_snapshot_id=risk_snapshot_id,
            market_capability_snapshot_sha256=self.capabilities.source_sha256,
            reason_codes=ordered,
        )

    def _evaluate(
        self,
        action: JsonObject,
        *,
        context: BoundDecisionContext,
        risk: SnapshotReadResult,
        reasons: set[RevalidationReasonCode],
    ) -> None:
        context_document = context.document
        snapshot = risk.snapshot
        if snapshot is None:
            return
        risk_snapshot_id = snapshot["snapshot_id"]
        if context_document["risk_snapshot_id"] != risk_snapshot_id:
            reasons.add(RevalidationReasonCode.SNAPSHOT_CONTEXT_MISMATCH)

        account = context_document["account"]
        accounting = snapshot["accounting"]
        exposure = snapshot["exposure"]
        decision = snapshot["decision"]
        assert isinstance(account, dict)
        assert isinstance(accounting, dict)
        assert isinstance(exposure, dict)
        assert isinstance(decision, dict)
        if (
            _decimal(account["nav"]) != _decimal(accounting["nav"])
            or _decimal(account["spot_available_quote"])
            != _decimal(exposure["available_balance_quote"])
            or _decimal(account["futures_available_margin"])
            != _decimal(exposure["available_margin_quote"])
            or account["risk_state"] != decision["state"]
        ):
            reasons.add(RevalidationReasonCode.SNAPSHOT_CONTEXT_MISMATCH)

        context_order_ids = {str(item["order_id"]) for item in context_document["open_orders"]}
        snapshot_order_ids = {str(item["order_id"]) for item in snapshot["open_orders"]}
        if context_order_ids != snapshot_order_ids:
            reasons.add(RevalidationReasonCode.SNAPSHOT_CONTEXT_MISMATCH)

        action_name = str(action["action"])
        if action_name not in context_document["allowed_actions"]:
            reasons.add(RevalidationReasonCode.ACTION_NOT_ALLOWED)
        risk_increasing = action_name in {"OPEN", "ADD"}
        if risk_increasing and not risk.entry_allowed:
            reasons.add(RevalidationReasonCode.RISK_ENTRY_BLOCKED)
        if risk_increasing and self.control_reader is not None:
            try:
                control = self.control_reader()
                if control.entry_stopped or control.emergency:
                    reasons.add(RevalidationReasonCode.OPERATIONAL_ENTRY_STOPPED)
            except Exception:
                # 控制状态不可读时禁止扩大风险；安全退出不依赖该读取结果。
                reasons.add(RevalidationReasonCode.OPERATIONAL_CONTROL_UNAVAILABLE)
        if not risk_increasing and not risk.safe_exit_allowed:
            reasons.add(RevalidationReasonCode.SAFE_EXIT_BLOCKED)

        instrument_id = str(action["instrument_id"])
        instrument_row = next(
            row for row in context_document["instruments"] if row["instrument_id"] == instrument_id
        )
        market, market_key = _market_name(action)
        market_row = instrument_row[market_key]
        if not isinstance(market_row, dict):
            reasons.add(RevalidationReasonCode.MARKET_RULES_UNAVAILABLE)
            return
        pair = str(market_row["pair"])
        capability = self.capabilities.capability_for_pair(pair, market)
        if not self._market_rules_available(capability):
            reasons.add(RevalidationReasonCode.MARKET_RULES_UNAVAILABLE)
            return
        assert capability is not None

        if market is MarketKind.FUTURES:
            leverage = _decimal(action["requested_leverage"])
            if (
                capability.effective_max_leverage is None
                or leverage < Decimal("1")
                or leverage > capability.effective_max_leverage
            ):
                reasons.add(RevalidationReasonCode.LEVERAGE_OUT_OF_RANGE)

        self._validate_prices(action, market_row, capability, reasons)
        self._validate_position(action, market_row, snapshot, reasons)
        self._validate_balance(action, account, capability, reasons)
        self._validate_orders(action, context_document["open_orders"], market_row, reasons)

    @staticmethod
    def _market_rules_available(capability: MarketCapability | None) -> bool:
        if capability is None or not capability.available:
            return False
        required = (
            capability.price_tick,
            capability.quantity_step,
            capability.minimum_quantity,
            capability.minimum_notional,
        )
        return all(value is not None and value > 0 for value in required)

    @staticmethod
    def _validate_prices(
        action: JsonObject,
        market_row: JsonObject,
        capability: MarketCapability,
        reasons: set[RevalidationReasonCode],
    ) -> None:
        values: list[Decimal] = []
        entry = action["entry"]
        if isinstance(entry, dict):
            lower = _decimal(entry["min"])
            upper = _decimal(entry["max"])
            values.extend((lower, upper))
            reference = _decimal(
                market_row["last_price"] if action["market"] == "spot" else market_row["mark_price"]
            )
            if reference < lower or reference > upper:
                reasons.add(RevalidationReasonCode.PRICE_OUTSIDE_APPROVED_RANGE)
        if action["stop_loss"] is not None:
            values.append(_decimal(action["stop_loss"]))
        values.extend(_decimal(value) for value in action["take_profit"])
        assert capability.price_tick is not None
        if any(value % capability.price_tick != 0 for value in values):
            reasons.add(RevalidationReasonCode.PRICE_NOT_TICK_ALIGNED)

    @staticmethod
    def _validate_position(
        action: JsonObject,
        market_row: JsonObject,
        snapshot: JsonObject,
        reasons: set[RevalidationReasonCode],
    ) -> None:
        action_name = str(action["action"])
        if action["market"] == "spot":
            exists = (
                _decimal(market_row["base_balance"]) > 0
                or _decimal(market_row["position_value_quote"]) > 0
            )
            side: str | None = "long" if exists else None
        else:
            position = market_row["position"]
            exists = isinstance(position, dict)
            side = str(position["side"]) if isinstance(position, dict) else None

        snapshot_positions = snapshot["accounting"]["positions"]
        snapshot_match = any(
            item["instrument_id"] == action["instrument_id"]
            and _order_market(item["market"]) == action["market"]
            for item in snapshot_positions
        )
        if snapshot_match != exists:
            reasons.add(RevalidationReasonCode.SNAPSHOT_CONTEXT_MISMATCH)
        if action_name == "OPEN" and exists:
            reasons.add(RevalidationReasonCode.POSITION_ALREADY_EXISTS)
        if action_name in {"ADD", "REDUCE", "CLOSE", "REPLACE_PROTECTION"}:
            if not exists:
                reasons.add(RevalidationReasonCode.POSITION_NOT_FOUND)
            elif side != action["side"]:
                reasons.add(RevalidationReasonCode.POSITION_SIDE_MISMATCH)

    @staticmethod
    def _validate_balance(
        action: JsonObject,
        account: JsonObject,
        capability: MarketCapability,
        reasons: set[RevalidationReasonCode],
    ) -> None:
        if action["action"] not in {"OPEN", "ADD"}:
            return
        assert capability.minimum_notional is not None
        leverage = _decimal(action["requested_leverage"])
        available = _decimal(
            account["spot_available_quote"]
            if action["market"] == "spot"
            else account["futures_available_margin"]
        )
        required = capability.minimum_notional / leverage
        if available < required:
            reasons.add(RevalidationReasonCode.AVAILABLE_BALANCE_INSUFFICIENT)

    @staticmethod
    def _validate_orders(
        action: JsonObject,
        open_orders: list[JsonObject],
        market_row: JsonObject,
        reasons: set[RevalidationReasonCode],
    ) -> None:
        action_name = str(action["action"])
        relevant = [
            order
            for order in open_orders
            if order["instrument_id"] == action["instrument_id"]
            and order["market"] == action["market"]
            and order["side"] == action["side"]
        ]
        conflicting_intents = (
            {"OPEN", "ADD"} if action_name in {"OPEN", "ADD"} else {"REDUCE", "CLOSE"}
        )
        if action_name in {"OPEN", "ADD", "REDUCE", "CLOSE"} and any(
            order["intent"] in conflicting_intents for order in relevant
        ):
            reasons.add(RevalidationReasonCode.OPEN_ORDER_CONFLICT)

        if action_name == "CANCEL_ORDER":
            target = next(
                (
                    order
                    for order in open_orders
                    if order["order_id"] == action["target_reference_id"]
                ),
                None,
            )
            if target is None or target not in relevant:
                reasons.add(RevalidationReasonCode.TARGET_REFERENCE_MISMATCH)

        if action_name == "REPLACE_PROTECTION":
            target_id = action["target_reference_id"]
            position = market_row.get("position")
            target = next((order for order in relevant if order["order_id"] == target_id), None)
            position_matches = isinstance(position, dict) and position["position_id"] == target_id
            if not position_matches and (
                target is None or target["intent"] not in {"STOP_LOSS", "TAKE_PROFIT"}
            ):
                reasons.add(RevalidationReasonCode.TARGET_REFERENCE_MISMATCH)
            if (
                target is not None
                and target["intent"] == "STOP_LOSS"
                and target["price"] is not None
            ):
                old_stop = _decimal(target["price"])
                new_stop = _decimal(action["stop_loss"])
                loosens = (action["side"] == "long" and new_stop < old_stop) or (
                    action["side"] == "short" and new_stop > old_stop
                )
                if loosens:
                    reasons.add(RevalidationReasonCode.PROTECTION_WOULD_LOOSEN)


class RevalidationCoordinator:
    """把一次重新校验原子投影成 Store 状态；不调用任何交易写接口。"""

    def __init__(self, store: ProposalStore, revalidator: ActionRevalidator) -> None:
        self.store = store
        self.revalidator = revalidator

    def process(
        self,
        proposal_id: str,
        context: BoundDecisionContext,
        risk: SnapshotReadResult,
        *,
        now_utc: datetime,
    ) -> RevalidationOutcome:
        proposal = self.store.get(proposal_id)
        if proposal is None:
            raise ValueError("proposal does not exist")
        if proposal.state in {ProposalState.QUEUED, ProposalState.CANCELLED, ProposalState.EXPIRED}:
            return RevalidationOutcome(proposal=proposal, report=None, replayed=True)
        if proposal.state is ProposalState.APPROVED:
            proposal = self.store.start_revalidation(
                proposal_id,
                occurred_at_utc=now_utc,
                idempotency_key=f"revalidation:start:{proposal_id}",
            )
        if proposal.state is not ProposalState.REVALIDATING:
            raise ValueError("proposal is not eligible for revalidation")

        report = self.revalidator.revalidate(proposal, context, risk, now_utc=now_utc)
        if report.passed:
            suffix = proposal.proposal_id.removeprefix("proposal-")
            occurred_at = (
                now_utc.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            )
            execution = {
                "execution_id": f"exec-{suffix}",
                "revalidated_at_utc": occurred_at,
                "context_sha256": report.context_sha256,
                "risk_snapshot_id": report.risk_snapshot_id,
                "market_capability_snapshot_sha256": (report.market_capability_snapshot_sha256),
                "submitted_at_utc": occurred_at,
                "finished_at_utc": None,
                "order_ids": [],
                "reason_codes": ["REVALIDATION_PASSED"],
            }
            proposal = self.store.finish_revalidation(
                proposal_id,
                passed=True,
                expired=False,
                occurred_at_utc=now_utc,
                idempotency_key=f"revalidation:result:{proposal_id}",
                reason_codes=("REVALIDATION_PASSED",),
                execution=execution,
            )
        else:
            reason_codes = tuple(code.value for code in report.reason_codes)
            proposal = self.store.finish_revalidation(
                proposal_id,
                passed=False,
                expired=RevalidationReasonCode.PROPOSAL_EXPIRED in report.reason_codes,
                occurred_at_utc=now_utc,
                idempotency_key=f"revalidation:result:{proposal_id}",
                reason_codes=reason_codes,
            )
        return RevalidationOutcome(proposal=proposal, report=report, replayed=False)
