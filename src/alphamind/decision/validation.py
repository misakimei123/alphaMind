"""R2-04 Action 业务校验与逐动作拒绝报告。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from alphamind.config import EffectiveConfig, MarketKind
from alphamind.decision.contracts import (
    BoundDecisionContext,
    BoundModelDecision,
    DecisionContractBinder,
)
from alphamind.market.capabilities import MarketCapability

JsonObject = dict[str, Any]


class ActionValidationStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ActionRejectionCode(StrEnum):
    APPROVAL_TTL_EXCEEDED = "approval_ttl_exceeded"
    NEWS_REQUIRED = "news_required"
    NEWS_ASSET_MISMATCH = "news_asset_mismatch"
    SIDE_NOT_ALLOWED = "side_not_allowed"
    LEVERAGE_OUT_OF_RANGE = "leverage_out_of_range"
    MARKET_RULES_UNAVAILABLE = "market_rules_unavailable"
    ENTRY_RANGE_INVALID = "entry_range_invalid"
    ENTRY_PRICE_DRIFT_EXCEEDED = "entry_price_drift_exceeded"
    PRICE_NOT_TICK_ALIGNED = "price_not_tick_aligned"
    STOP_LOSS_INVALID = "stop_loss_invalid"
    TAKE_PROFIT_INVALID = "take_profit_invalid"
    TAKE_PROFIT_ORDER_INVALID = "take_profit_order_invalid"
    POSITION_ALREADY_EXISTS = "position_already_exists"
    POSITION_NOT_FOUND = "position_not_found"
    POSITION_SIDE_MISMATCH = "position_side_mismatch"
    POSITION_PNL_UNAVAILABLE = "position_pnl_unavailable"
    ADD_TO_LOSING_POSITION_DISABLED = "add_to_losing_position_disabled"
    TARGET_REFERENCE_MISMATCH = "target_reference_mismatch"
    TARGET_SIDE_MISMATCH = "target_side_mismatch"
    TARGET_NOT_PROTECTION = "target_not_protection"
    PROTECTION_WOULD_LOOSEN = "protection_would_loosen"
    UNEXPECTED_PROTECTION_FIELDS = "unexpected_protection_fields"


@dataclass(frozen=True, slots=True)
class ActionValidationResult:
    action_id: str
    status: ActionValidationStatus
    rejection_codes: tuple[ActionRejectionCode, ...]

    def to_safe_dict(self) -> JsonObject:
        return {
            "action_id": self.action_id,
            "status": self.status.value,
            "rejection_codes": [code.value for code in self.rejection_codes],
        }


@dataclass(frozen=True, slots=True)
class DecisionValidationReport:
    context_sha256: str
    source_decision_sha256: str
    accepted_decision: BoundModelDecision | None
    action_results: tuple[ActionValidationResult, ...]

    @property
    def accepted_action_ids(self) -> tuple[str, ...]:
        return tuple(
            result.action_id
            for result in self.action_results
            if result.status is ActionValidationStatus.ACCEPTED
        )

    @property
    def rejected_action_ids(self) -> tuple[str, ...]:
        return tuple(
            result.action_id
            for result in self.action_results
            if result.status is ActionValidationStatus.REJECTED
        )

    @property
    def approval_candidates(self) -> tuple[JsonObject, ...]:
        """只有通过全部业务校验的动作才能交给未来 R3 Proposal Store。"""

        if self.accepted_decision is None:
            return ()
        return tuple(
            json.loads(json.dumps(action, ensure_ascii=False))
            for action in self.accepted_decision.document["actions"]
        )

    def to_safe_dict(self) -> JsonObject:
        return {
            "context_sha256": self.context_sha256,
            "source_decision_sha256": self.source_decision_sha256,
            "accepted_action_ids": list(self.accepted_action_ids),
            "rejected_action_ids": list(self.rejected_action_ids),
            "actions": [result.to_safe_dict() for result in self.action_results],
        }


def _decimal(value: object) -> Decimal:
    # 输入已经通过 schema/binder；这里仅转成 Decimal 执行业务比较，不接受 float。
    return Decimal(str(value))


def _is_tick_aligned(value: Decimal, tick: Decimal) -> bool:
    return value % tick == 0


class ActionBusinessValidator:
    """在已绑定 Context 上逐动作聚合确定性拒绝原因。"""

    def __init__(
        self,
        effective: EffectiveConfig,
        *,
        binder: DecisionContractBinder | None = None,
    ) -> None:
        self.effective = effective
        self.binder = binder or DecisionContractBinder(effective)
        self.registry = effective.instrument_registry
        self.capability = effective.market_capability_snapshot
        risk = effective.runtime["risk"]
        approval = effective.runtime["approval"]
        self.maximum_price_drift = _decimal(risk["maximum_approval_price_drift_fraction"])
        self.news_required_for_risk_increase = bool(risk["news_required_for_risk_increase"])
        self.allow_add_to_losing_position = bool(risk["allow_add_to_losing_position"])
        self.maximum_ttl_seconds = int(approval["ttl_minutes"]) * 60

    def validate(
        self,
        context: BoundDecisionContext,
        decision: BoundModelDecision,
    ) -> DecisionValidationReport:
        if decision.context_sha256 != context.sha256:
            raise ValueError("decision is not bound to this context")

        context_document = context.document
        decision_document = decision.document
        instrument_rows = {
            str(row["instrument_id"]): row for row in context_document["instruments"]
        }
        news_rows = {str(row["news_id"]): row for row in context_document["news_items"]}
        order_rows = {str(row["order_id"]): row for row in context_document["open_orders"]}
        results: list[ActionValidationResult] = []
        accepted_actions: list[JsonObject] = []

        for raw_action in decision_document["actions"]:
            action = dict(raw_action)
            rejection_set = self._validate_action(
                action,
                instrument_rows=instrument_rows,
                news_rows=news_rows,
                order_rows=order_rows,
            )
            rejection_codes = tuple(code for code in ActionRejectionCode if code in rejection_set)
            status = (
                ActionValidationStatus.REJECTED
                if rejection_codes
                else ActionValidationStatus.ACCEPTED
            )
            results.append(
                ActionValidationResult(
                    action_id=str(action["action_id"]),
                    status=status,
                    rejection_codes=rejection_codes,
                )
            )
            if status is ActionValidationStatus.ACCEPTED:
                accepted_actions.append(action)

        accepted_decision: BoundModelDecision | None = None
        if accepted_actions:
            accepted_document = dict(decision_document)
            accepted_document["actions"] = accepted_actions
            accepted_decision = self.binder.bind_model_decision(context, accepted_document)

        return DecisionValidationReport(
            context_sha256=context.sha256,
            source_decision_sha256=decision.sha256,
            accepted_decision=accepted_decision,
            action_results=tuple(results),
        )

    def _validate_action(
        self,
        action: JsonObject,
        *,
        instrument_rows: dict[str, JsonObject],
        news_rows: dict[str, JsonObject],
        order_rows: dict[str, JsonObject],
    ) -> set[ActionRejectionCode]:
        rejected: set[ActionRejectionCode] = set()
        instrument_id = str(action["instrument_id"])
        action_name = str(action["action"])
        side = str(action["side"])
        market = MarketKind.SPOT if action["market"] == "spot" else MarketKind.FUTURES
        market_name = "spot" if market is MarketKind.SPOT else "futures"
        instrument = self.registry.get(instrument_id)
        row = instrument_rows[instrument_id]
        market_row = row[market_name]
        assert instrument is not None and isinstance(market_row, dict)
        pair = str(market_row["pair"])
        capability = self.capability.capability_for_pair(pair, market)

        if int(action["valid_for_seconds"]) > self.maximum_ttl_seconds:
            rejected.add(ActionRejectionCode.APPROVAL_TTL_EXCEEDED)

        self._validate_news(action, news_rows=news_rows, rejected=rejected)

        risk_increasing = action_name in {"OPEN", "ADD"}
        if risk_increasing and market is MarketKind.FUTURES:
            allowed_side = (
                instrument.futures.allow_long if side == "long" else instrument.futures.allow_short
            )
            if not allowed_side:
                rejected.add(ActionRejectionCode.SIDE_NOT_ALLOWED)
            leverage = _decimal(action["requested_leverage"])
            if (
                capability is None
                or capability.effective_max_leverage is None
                or leverage < Decimal("1")
                or leverage > capability.effective_max_leverage
            ):
                rejected.add(ActionRejectionCode.LEVERAGE_OUT_OF_RANGE)

        position, position_side, position_pnl = self._position(market, market_row)
        if action_name == "OPEN" and position:
            rejected.add(ActionRejectionCode.POSITION_ALREADY_EXISTS)
        if action_name in {"ADD", "REDUCE", "CLOSE", "REPLACE_PROTECTION"}:
            if not position:
                rejected.add(ActionRejectionCode.POSITION_NOT_FOUND)
            elif position_side != side:
                rejected.add(ActionRejectionCode.POSITION_SIDE_MISMATCH)

        if action_name == "ADD" and position and not self.allow_add_to_losing_position:
            if position_pnl is None:
                rejected.add(ActionRejectionCode.POSITION_PNL_UNAVAILABLE)
            elif position_pnl < 0:
                rejected.add(ActionRejectionCode.ADD_TO_LOSING_POSITION_DISABLED)

        if action_name in {"OPEN", "ADD"}:
            self._validate_entry_prices(
                action,
                market_row=market_row,
                capability=capability,
                rejected=rejected,
            )
        elif action_name in {"REDUCE", "CLOSE"}:
            if action["stop_loss"] is not None or action["take_profit"]:
                rejected.add(ActionRejectionCode.UNEXPECTED_PROTECTION_FIELDS)
        elif action_name == "CANCEL_ORDER":
            target = order_rows[str(action["target_reference_id"])]
            if target["side"] != side:
                rejected.add(ActionRejectionCode.TARGET_SIDE_MISMATCH)
        elif action_name == "REPLACE_PROTECTION" and position:
            self._validate_replacement(
                action,
                market_row=market_row,
                capability=capability,
                order_rows=order_rows,
                rejected=rejected,
            )

        return rejected

    def _validate_news(
        self,
        action: JsonObject,
        *,
        news_rows: dict[str, JsonObject],
        rejected: set[ActionRejectionCode],
    ) -> None:
        instrument_id = str(action["instrument_id"])
        references = [str(value) for value in action["news_refs"]]
        if (
            action["action"] in {"OPEN", "ADD"}
            and self.news_required_for_risk_increase
            and not references
        ):
            rejected.add(ActionRejectionCode.NEWS_REQUIRED)
        for news_id in references:
            assets = {str(value) for value in news_rows[news_id]["assets"]}
            if instrument_id not in assets and "MARKET" not in assets:
                rejected.add(ActionRejectionCode.NEWS_ASSET_MISMATCH)

    @staticmethod
    def _position(
        market: MarketKind,
        market_row: JsonObject,
    ) -> tuple[bool, str | None, Decimal | None]:
        if market is MarketKind.SPOT:
            exists = (
                _decimal(market_row["base_balance"]) > 0
                or _decimal(market_row["position_value_quote"]) > 0
            )
            # 当前 DecisionContext 没有现货成本基线，不能判断 ADD 是否在亏损仓位上。
            return exists, "long" if exists else None, None
        position = market_row["position"]
        if not isinstance(position, dict):
            return False, None, None
        return True, str(position["side"]), _decimal(position["unrealized_pnl"])

    def _validate_entry_prices(
        self,
        action: JsonObject,
        *,
        market_row: JsonObject,
        capability: MarketCapability | None,
        rejected: set[ActionRejectionCode],
    ) -> None:
        entry = action["entry"]
        assert isinstance(entry, dict)
        entry_min = _decimal(entry["min"])
        entry_max = _decimal(entry["max"])
        stop_loss = _decimal(action["stop_loss"])
        take_profit = tuple(_decimal(value) for value in action["take_profit"])
        reference = _decimal(
            market_row["last_price"] if action["market"] == "spot" else market_row["mark_price"]
        )
        if entry_min > entry_max:
            rejected.add(ActionRejectionCode.ENTRY_RANGE_INVALID)
        if any(
            abs(value - reference) / reference > self.maximum_price_drift
            for value in (entry_min, entry_max)
        ):
            rejected.add(ActionRejectionCode.ENTRY_PRICE_DRIFT_EXCEEDED)

        self._validate_tick(
            (entry_min, entry_max, stop_loss, *take_profit),
            capability=capability,
            rejected=rejected,
        )
        self._validate_price_geometry(
            side=str(action["side"]),
            lower=entry_min,
            upper=entry_max,
            stop_loss=stop_loss,
            take_profit=take_profit,
            rejected=rejected,
        )

    def _validate_replacement(
        self,
        action: JsonObject,
        *,
        market_row: JsonObject,
        capability: MarketCapability | None,
        order_rows: dict[str, JsonObject],
        rejected: set[ActionRejectionCode],
    ) -> None:
        target_id = str(action["target_reference_id"])
        position = market_row.get("position")
        target_order = order_rows.get(target_id)
        old_stop: Decimal | None = None
        if isinstance(position, dict) and position["position_id"] == target_id:
            if position["stop_loss"] is not None:
                old_stop = _decimal(position["stop_loss"])
        elif target_order is not None:
            if (
                target_order["instrument_id"] != action["instrument_id"]
                or target_order["market"] != action["market"]
            ):
                rejected.add(ActionRejectionCode.TARGET_REFERENCE_MISMATCH)
            if target_order["intent"] not in {"STOP_LOSS", "TAKE_PROFIT"}:
                rejected.add(ActionRejectionCode.TARGET_NOT_PROTECTION)
            if target_order["side"] != action["side"]:
                rejected.add(ActionRejectionCode.TARGET_SIDE_MISMATCH)
            if target_order["intent"] == "STOP_LOSS" and target_order["price"] is not None:
                old_stop = _decimal(target_order["price"])
        else:
            rejected.add(ActionRejectionCode.TARGET_NOT_PROTECTION)

        reference = _decimal(
            market_row["last_price"] if action["market"] == "spot" else market_row["mark_price"]
        )
        stop_loss = _decimal(action["stop_loss"])
        take_profit = tuple(_decimal(value) for value in action["take_profit"])
        self._validate_tick(
            (stop_loss, *take_profit),
            capability=capability,
            rejected=rejected,
        )
        self._validate_price_geometry(
            side=str(action["side"]),
            lower=reference,
            upper=reference,
            stop_loss=stop_loss,
            take_profit=take_profit,
            rejected=rejected,
        )
        if old_stop is not None:
            loosens_long = action["side"] == "long" and stop_loss < old_stop
            loosens_short = action["side"] == "short" and stop_loss > old_stop
            if loosens_long or loosens_short:
                rejected.add(ActionRejectionCode.PROTECTION_WOULD_LOOSEN)

    @staticmethod
    def _validate_tick(
        values: tuple[Decimal, ...],
        *,
        capability: MarketCapability | None,
        rejected: set[ActionRejectionCode],
    ) -> None:
        price_tick = capability.price_tick if capability is not None else None
        if price_tick is None or price_tick <= 0:
            rejected.add(ActionRejectionCode.MARKET_RULES_UNAVAILABLE)
            return
        if any(not _is_tick_aligned(value, price_tick) for value in values):
            rejected.add(ActionRejectionCode.PRICE_NOT_TICK_ALIGNED)

    @staticmethod
    def _validate_price_geometry(
        *,
        side: str,
        lower: Decimal,
        upper: Decimal,
        stop_loss: Decimal,
        take_profit: tuple[Decimal, ...],
        rejected: set[ActionRejectionCode],
    ) -> None:
        if side == "long":
            if stop_loss >= lower:
                rejected.add(ActionRejectionCode.STOP_LOSS_INVALID)
            if any(target <= upper for target in take_profit):
                rejected.add(ActionRejectionCode.TAKE_PROFIT_INVALID)
            if take_profit != tuple(sorted(take_profit)):
                rejected.add(ActionRejectionCode.TAKE_PROFIT_ORDER_INVALID)
        else:
            if stop_loss <= upper:
                rejected.add(ActionRejectionCode.STOP_LOSS_INVALID)
            if any(target >= lower for target in take_profit):
                rejected.add(ActionRejectionCode.TAKE_PROFIT_INVALID)
            if take_profit != tuple(sorted(take_profit, reverse=True)):
                rejected.add(ActionRejectionCode.TAKE_PROFIT_ORDER_INVALID)
