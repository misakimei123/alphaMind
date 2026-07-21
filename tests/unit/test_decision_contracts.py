from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
import yaml

from alphamind.config import load_effective_config
from alphamind.decision import (
    SUPPORTED_SCHEMA_VERSIONS,
    ContractErrorCode,
    ContractValidationError,
    DecisionContractBinder,
)

PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "contracts"
NOW = datetime(2026, 7, 18, 12, 0, 5, tzinfo=UTC)


def _yaml(name: str) -> dict[str, object]:
    document = yaml.safe_load((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _binder_and_context() -> tuple[DecisionContractBinder, dict[str, object]]:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    context = _yaml("decision-context.valid.yaml")
    context["config_sha256"] = effective.effective_sha256
    context["instrument_registry_sha256"] = effective.instrument_registry.source_sha256
    return DecisionContractBinder(effective), context


def _assert_error(
    code: ContractErrorCode,
    operation: object,
) -> ContractValidationError:
    assert callable(operation)
    with pytest.raises(ContractValidationError) as raised:
        operation()
    assert raised.value.code is code
    return raised.value


def test_supported_versions_are_explicit_and_valid_fixture_chain_binds() -> None:
    assert SUPPORTED_SCHEMA_VERSIONS == {
        "news-item.schema.yaml": 1,
        "decision-context.schema.yaml": 2,
        "model-decision.schema.yaml": 1,
        "trade-action.schema.yaml": 2,
    }
    binder, context = _binder_and_context()
    decision = _yaml("model-decision.valid.yaml")

    chain = binder.bind_chain(context, decision, now_utc=NOW)

    assert chain.context.cycle_id == "cycle-20260718T120000Z-0123abcd"
    assert chain.context.instrument_ids == ("SOL",)
    assert chain.context.news_ids == ("news-20260718T115500Z-0123456789ab",)
    assert chain.decision.action_ids == ("act-20260718T120000Z-0123456789ab",)
    assert len(chain.context.sha256) == len(chain.decision.sha256) == 64
    context["cycle_id"] = "cycle-20260718T120000Z-deadbeef"
    decision["decision_summary"] = "mutated after binding"
    assert chain.context.cycle_id == "cycle-20260718T120000Z-0123abcd"
    assert chain.decision.document["decision_summary"] != "mutated after binding"
    exposed = chain.context.document
    exposed["cycle_id"] = "cycle-20260718T120000Z-deadbeef"
    assert chain.context.document["cycle_id"] == "cycle-20260718T120000Z-0123abcd"


def test_standalone_news_binding_is_ready_for_configured_adapters() -> None:
    binder, _ = _binder_and_context()
    news = _yaml("news-item.valid.yaml")

    bound = binder.bind_news_item(news, as_of_utc=NOW)

    assert bound.news_id == "news-20260718T115500Z-0123456789ab"
    assert bound.assets == ("SOL",)
    assert len(bound.sha256) == 64
    news["title"] = "mutated"
    assert bound.document["title"] != "mutated"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("display_name", "Spoofed source"),
        ("source_type", "media"),
        ("trust_tier", "secondary"),
    ],
)
def test_news_source_identity_is_bound_to_effective_config(field: str, value: str) -> None:
    binder, _ = _binder_and_context()
    news = _yaml("news-item.valid.yaml")
    news["source"][field] = value

    error = _assert_error(
        ContractErrorCode.CONFIG_MISMATCH,
        lambda: binder.bind_news_item(news, as_of_utc=NOW),
    )
    assert error.location == f"news.source.{field}"


@pytest.mark.parametrize(("field", "value"), [("language", "zh-CN"), ("category", "macro")])
def test_news_language_and_category_are_bound_to_source_config(field: str, value: str) -> None:
    binder, _ = _binder_and_context()
    news = _yaml("news-item.valid.yaml")
    news[field] = value

    error = _assert_error(
        ContractErrorCode.CONFIG_MISMATCH,
        lambda: binder.bind_news_item(news, as_of_utc=NOW),
    )
    assert error.location == f"news.{field}"


@pytest.mark.parametrize("target", ["context", "news", "decision", "action"])
def test_unsupported_versions_are_rejected_without_silent_upgrade(target: str) -> None:
    binder, context = _binder_and_context()
    decision = _yaml("model-decision.valid.yaml")
    if target == "context":
        context["schema_version"] = 1
        operation = partial(binder.bind_context, context, now_utc=NOW)
    elif target == "news":
        context["news_items"][0]["schema_version"] = 2
        operation = partial(binder.bind_context, context, now_utc=NOW)
    elif target == "decision":
        bound = binder.bind_context(context, now_utc=NOW)
        decision["schema_version"] = 2
        operation = partial(binder.bind_model_decision, bound, decision)
    else:
        bound = binder.bind_context(context, now_utc=NOW)
        decision["actions"][0]["schema_version"] = 1
        operation = partial(binder.bind_model_decision, bound, decision)

    error = _assert_error(ContractErrorCode.UNSUPPORTED_SCHEMA_VERSION, operation)
    assert error.location.endswith("schema_version")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", "demo"),
        ("config_sha256", "e" * 64),
        ("instrument_registry_sha256", "f" * 64),
    ],
)
def test_context_must_bind_to_effective_runtime_configuration(field: str, value: str) -> None:
    binder, context = _binder_and_context()
    context[field] = value

    error = _assert_error(
        ContractErrorCode.CONFIG_MISMATCH,
        lambda: binder.bind_context(context, now_utc=NOW),
    )
    assert error.location == f"context.{field}"


def test_context_rejects_duplicate_ids_unknown_pairs_and_risk_increase() -> None:
    binder, context = _binder_and_context()
    duplicate = deepcopy(context["instruments"][0])
    duplicate["observed_at_utc"] = "2026-07-18T11:59:59Z"
    context["instruments"].append(duplicate)
    _assert_error(
        ContractErrorCode.DUPLICATE_VALUE,
        lambda: binder.bind_context(context, now_utc=NOW),
    )

    binder, context = _binder_and_context()
    context["instruments"][0]["spot"]["pair"] = "BTC/USDT"
    _assert_error(
        ContractErrorCode.MARKET_UNAVAILABLE,
        lambda: binder.bind_context(context, now_utc=NOW),
    )

    binder, context = _binder_and_context()
    context["account"]["risk_state"] = "CLOSE_ONLY"
    _assert_error(
        ContractErrorCode.ACTION_NOT_ALLOWED,
        lambda: binder.bind_context(context, now_utc=NOW),
    )


def test_context_position_ids_are_unique_across_instruments() -> None:
    binder, context = _binder_and_context()
    position = {
        "position_id": "shared-position-id",
        "side": "long",
        "quantity": "1",
        "entry_price": "150",
        "leverage": "2",
        "liquidation_price": "75",
        "unrealized_pnl": "6.10",
        "stop_loss": "145",
        "take_profit": "165",
    }
    context["instruments"][0]["futures"]["position"] = deepcopy(position)
    btc = deepcopy(context["instruments"][0])
    btc["instrument_id"] = "BTC"
    btc["spot"]["pair"] = "BTC/USDT"
    btc["futures"]["pair"] = "BTC/USDT:USDT"
    btc["futures"]["position"] = deepcopy(position)
    context["instruments"].append(btc)

    error = _assert_error(
        ContractErrorCode.DUPLICATE_VALUE,
        lambda: binder.bind_context(context, now_utc=NOW),
    )
    assert error.location.endswith("position.position_id")


def test_context_rejects_timestamp_news_and_arithmetic_inconsistency() -> None:
    binder, context = _binder_and_context()
    context["generated_at_utc"] = "2026-07-18T11:59:59Z"
    _assert_error(
        ContractErrorCode.INVALID_TIMESTAMP_ORDER,
        lambda: binder.bind_context(context, now_utc=NOW),
    )

    binder, context = _binder_and_context()
    context["news_items"][0]["published_at_utc"] = "2026-07-18T05:00:00Z"
    context["news_items"][0]["fetched_at_utc"] = "2026-07-18T05:01:00Z"
    _assert_error(
        ContractErrorCode.STALE_NEWS,
        lambda: binder.bind_context(context, now_utc=NOW),
    )

    binder, context = _binder_and_context()
    context["account"]["futures_margin_used"] = "501"
    _assert_error(
        ContractErrorCode.INVALID_ARITHMETIC,
        lambda: binder.bind_context(context, now_utc=NOW),
    )


def test_context_rejects_mismatched_pattern_semantic_without_free_text_fallback() -> None:
    binder, context = _binder_and_context()
    context["instruments"][0]["features"]["pattern_semantic"] = "bearish_reversal"

    error = _assert_error(
        ContractErrorCode.INVALID_REFERENCE,
        lambda: binder.bind_context(context, now_utc=NOW),
    )
    assert error.location.endswith("features.pattern_semantic")


def test_context_deduplicates_news_across_business_keys() -> None:
    binder, context = _binder_and_context()
    duplicate = deepcopy(context["news_items"][0])
    duplicate["news_id"] = "news-20260718T115501Z-abcdefabcdef"
    context["news_items"].append(duplicate)

    error = _assert_error(
        ContractErrorCode.DUPLICATE_VALUE,
        lambda: binder.bind_context(context, now_utc=NOW),
    )
    assert error.location == "context.news_items.canonical_url"


def test_model_decision_must_match_cycle_limits_and_local_context() -> None:
    binder, context_document = _binder_and_context()
    context = binder.bind_context(context_document, now_utc=NOW)
    decision = _yaml("model-decision.valid.yaml")
    decision["cycle_id"] = "cycle-20260718T120000Z-deadbeef"
    _assert_error(
        ContractErrorCode.CYCLE_MISMATCH,
        lambda: binder.bind_model_decision(context, decision),
    )

    decision = _yaml("model-decision.valid.yaml")
    actions = []
    for index in range(9):
        action = deepcopy(decision["actions"][0])
        action["action_id"] = f"act-20260718T120000Z-{index + 1:012x}"
        actions.append(action)
    decision["actions"] = actions
    _assert_error(
        ContractErrorCode.MAX_ACTIONS_EXCEEDED,
        lambda: binder.bind_model_decision(context, decision),
    )

    decision = _yaml("model-decision.valid.yaml")
    decision["actions"][0]["news_refs"] = ["news-20260718T115500Z-deadbeefdead"]
    _assert_error(
        ContractErrorCode.INVALID_REFERENCE,
        lambda: binder.bind_model_decision(context, decision),
    )


def test_model_action_requires_allowed_local_instrument_and_market() -> None:
    binder, context_document = _binder_and_context()
    context = binder.bind_context(context_document, now_utc=NOW)

    decision = _yaml("model-decision.valid.yaml")
    decision["actions"][0]["instrument_id"] = "XRP"
    _assert_error(
        ContractErrorCode.UNKNOWN_INSTRUMENT,
        lambda: binder.bind_model_decision(context, decision),
    )

    context_document["allowed_actions"].remove("OPEN")
    context = binder.bind_context(context_document, now_utc=NOW)
    decision = _yaml("model-decision.valid.yaml")
    _assert_error(
        ContractErrorCode.ACTION_NOT_ALLOWED,
        lambda: binder.bind_model_decision(context, decision),
    )

    binder, context_document = _binder_and_context()
    context_document["instruments"][0]["futures"] = None
    context = binder.bind_context(context_document, now_utc=NOW)
    decision = _yaml("model-decision.valid.yaml")
    _assert_error(
        ContractErrorCode.MARKET_UNAVAILABLE,
        lambda: binder.bind_model_decision(context, decision),
    )


def test_pending_action_and_cancel_target_references_are_local() -> None:
    binder, context_document = _binder_and_context()
    context_document["pending_approvals"] = [
        {
            "proposal_id": "proposal-20260718T120000Z-0123456789ab",
            "action_id": "act-20260718T120000Z-0123456789ab",
            "state": "PENDING_APPROVAL",
            "expires_at_utc": "2026-07-18T12:10:00Z",
        }
    ]
    context = binder.bind_context(context_document, now_utc=NOW)
    decision = _yaml("model-decision.valid.yaml")
    _assert_error(
        ContractErrorCode.DUPLICATE_VALUE,
        lambda: binder.bind_model_decision(context, decision),
    )

    binder, context_document = _binder_and_context()
    context = binder.bind_context(context_document, now_utc=NOW)
    decision = _yaml("model-decision.valid.yaml")
    action = decision["actions"][0]
    action.update(
        {
            "action": "CANCEL_ORDER",
            "order_preference": "none",
            "entry": None,
            "stop_loss": None,
            "take_profit": [],
            "target_reference_id": "missing-order",
        }
    )
    _assert_error(
        ContractErrorCode.INVALID_REFERENCE,
        lambda: binder.bind_model_decision(context, decision),
    )


def test_schema_errors_do_not_echo_untrusted_values() -> None:
    binder, context = _binder_and_context()
    context["unexpected"] = "must-not-appear-in-error"

    error = _assert_error(
        ContractErrorCode.SCHEMA_VALIDATION_FAILED,
        lambda: binder.bind_context(context, now_utc=NOW),
    )
    assert "must-not-appear-in-error" not in str(error)
