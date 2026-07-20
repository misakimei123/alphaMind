from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from alphamind.config import EffectiveConfig, load_effective_config
from alphamind.decision import (
    ActionBusinessValidator,
    ActionRejectionCode,
    ActionValidationStatus,
    BoundDecisionContext,
    DecisionContractBinder,
    DecisionValidationReport,
)

PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "contracts"
NOW = datetime(2026, 7, 18, 12, 0, 5, tzinfo=UTC)


def _yaml(name: str) -> dict[str, object]:
    document = yaml.safe_load((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _effective_context(
    document: dict[str, object] | None = None,
) -> tuple[EffectiveConfig, DecisionContractBinder, BoundDecisionContext]:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    context_document = document or _yaml("decision-context.valid.yaml")
    context_document["config_sha256"] = effective.effective_sha256
    context_document["instrument_registry_sha256"] = effective.instrument_registry.source_sha256
    binder = DecisionContractBinder(effective)
    return effective, binder, binder.bind_context(context_document, now_utc=NOW)


def _position(*, unrealized_pnl: str = "6.10", stop_loss: str = "150") -> dict[str, object]:
    return {
        "position_id": "position-sol-long",
        "side": "long",
        "quantity": "1",
        "entry_price": "150",
        "leverage": "2",
        "liquidation_price": "75",
        "unrealized_pnl": unrealized_pnl,
        "stop_loss": stop_loss,
        "take_profit": "165",
    }


def _codes(report: DecisionValidationReport) -> tuple[ActionRejectionCode, ...]:
    results = report.action_results
    assert len(results) == 1
    return results[0].rejection_codes


def test_valid_and_rejected_actions_are_reported_and_filtered_per_action() -> None:
    effective, binder, context = _effective_context()
    decision_document = _yaml("model-decision.valid.yaml")
    rejected = deepcopy(decision_document["actions"][0])
    rejected["action_id"] = "act-20260718T120000Z-deadbeef0001"
    rejected["requested_leverage"] = "3"
    decision_document["actions"].append(rejected)
    decision = binder.bind_model_decision(context, decision_document)

    report = ActionBusinessValidator(effective, binder=binder).validate(context, decision)

    assert [result.status for result in report.action_results] == [
        ActionValidationStatus.ACCEPTED,
        ActionValidationStatus.REJECTED,
    ]
    assert report.accepted_action_ids == ("act-20260718T120000Z-0123456789ab",)
    assert report.rejected_action_ids == ("act-20260718T120000Z-deadbeef0001",)
    assert report.action_results[1].rejection_codes == (ActionRejectionCode.LEVERAGE_OUT_OF_RANGE,)
    assert report.accepted_decision is not None
    assert report.accepted_decision.context_sha256 == context.sha256
    assert report.accepted_decision.action_ids == report.accepted_action_ids
    assert [item["action_id"] for item in report.approval_candidates] == list(
        report.accepted_action_ids
    )


def test_decision_cannot_be_reused_with_a_different_same_cycle_context() -> None:
    effective, binder, context = _effective_context()
    decision = binder.bind_model_decision(context, _yaml("model-decision.valid.yaml"))
    changed_document = context.document
    changed_document["instruments"][0]["futures"]["mark_price"] = "156.20"
    changed = binder.bind_context(changed_document, now_utc=NOW)

    with pytest.raises(ValueError, match="not bound to this context"):
        ActionBusinessValidator(effective, binder=binder).validate(changed, decision)


def test_entry_price_rejections_are_aggregated_in_stable_code_order() -> None:
    effective, binder, context = _effective_context()
    decision_document = _yaml("model-decision.valid.yaml")
    action = decision_document["actions"][0]
    action["entry"] = {"min": "160.001", "max": "159.999"}
    action["stop_loss"] = "161.001"
    action["take_profit"] = ["159.001", "158.001"]
    decision = binder.bind_model_decision(context, decision_document)

    report = ActionBusinessValidator(effective).validate(context, decision)

    assert _codes(report) == (
        ActionRejectionCode.ENTRY_RANGE_INVALID,
        ActionRejectionCode.ENTRY_PRICE_DRIFT_EXCEEDED,
        ActionRejectionCode.PRICE_NOT_TICK_ALIGNED,
        ActionRejectionCode.STOP_LOSS_INVALID,
        ActionRejectionCode.TAKE_PROFIT_INVALID,
        ActionRejectionCode.TAKE_PROFIT_ORDER_INVALID,
    )


def test_risk_increase_requires_relevant_news_and_respects_approval_ttl() -> None:
    effective, binder, context = _effective_context()
    decision_document = _yaml("model-decision.valid.yaml")
    action = decision_document["actions"][0]
    action["news_refs"] = []
    action["reason_codes"] = ["TREND"]
    action["valid_for_seconds"] = 601
    decision = binder.bind_model_decision(context, decision_document)

    report = ActionBusinessValidator(effective).validate(context, decision)

    assert _codes(report) == (
        ActionRejectionCode.APPROVAL_TTL_EXCEEDED,
        ActionRejectionCode.NEWS_REQUIRED,
    )


def test_news_reference_must_apply_to_the_action_instrument() -> None:
    context_document = _yaml("decision-context.valid.yaml")
    btc = deepcopy(context_document["instruments"][0])
    btc["instrument_id"] = "BTC"
    btc["spot"]["pair"] = "BTC/USDT"
    btc["futures"]["pair"] = "BTC/USDT:USDT"
    context_document["instruments"].append(btc)
    context_document["news_items"][0]["assets"] = ["BTC"]
    effective, binder, context = _effective_context(context_document)
    decision = binder.bind_model_decision(context, _yaml("model-decision.valid.yaml"))

    report = ActionBusinessValidator(effective).validate(context, decision)

    assert _codes(report) == (ActionRejectionCode.NEWS_ASSET_MISMATCH,)


def test_add_to_losing_position_is_rejected_by_runtime_policy() -> None:
    context_document = _yaml("decision-context.valid.yaml")
    context_document["instruments"][0]["futures"]["position"] = _position(unrealized_pnl="-0.01")
    effective, binder, context = _effective_context(context_document)
    decision_document = _yaml("model-decision.valid.yaml")
    decision_document["actions"][0]["action"] = "ADD"
    decision = binder.bind_model_decision(context, decision_document)

    report = ActionBusinessValidator(effective).validate(context, decision)

    assert _codes(report) == (ActionRejectionCode.ADD_TO_LOSING_POSITION_DISABLED,)


def test_position_and_action_shape_fail_closed_for_reduce() -> None:
    effective, binder, context = _effective_context()
    decision_document = _yaml("model-decision.valid.yaml")
    action = decision_document["actions"][0]
    action.update(
        {
            "action": "REDUCE",
            "entry": None,
            "reduce_fraction": "0.5",
            "target_reference_id": None,
        }
    )
    decision = binder.bind_model_decision(context, decision_document)

    report = ActionBusinessValidator(effective).validate(context, decision)

    assert _codes(report) == (
        ActionRejectionCode.POSITION_NOT_FOUND,
        ActionRejectionCode.UNEXPECTED_PROTECTION_FIELDS,
    )


def test_replace_protection_cannot_loosen_existing_stop() -> None:
    context_document = _yaml("decision-context.valid.yaml")
    context_document["instruments"][0]["futures"]["position"] = _position()
    effective, binder, context = _effective_context(context_document)
    decision_document = _yaml("model-decision.valid.yaml")
    action = decision_document["actions"][0]
    action.update(
        {
            "action": "REPLACE_PROTECTION",
            "order_preference": "none",
            "entry": None,
            "stop_loss": "149.00",
            "take_profit": ["170.00"],
            "target_reference_id": "position-sol-long",
        }
    )
    decision = binder.bind_model_decision(context, decision_document)

    report = ActionBusinessValidator(effective).validate(context, decision)

    assert _codes(report) == (ActionRejectionCode.PROTECTION_WOULD_LOOSEN,)
