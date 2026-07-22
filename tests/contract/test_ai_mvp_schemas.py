from __future__ import annotations

import hashlib
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest
import yaml
from referencing import Registry, Resource

PROJECT_ROOT = Path(__file__).parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "data" / "schemas"
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "contracts"

AI_MVP_SCHEMAS = (
    "runtime-config.schema.yaml",
    "instrument-registry.schema.yaml",
    "news-item.schema.yaml",
    "decision-context.schema.yaml",
    "trade-action.schema.yaml",
    "approval-event.schema.yaml",
    "approval-record.schema.yaml",
    "model-decision.schema.yaml",
    "ai-profile.schema.yaml",
    "news-source-registry.schema.yaml",
    "market-capability-snapshot.schema.yaml",
    "telegram-notification.schema.yaml",
)


def load_yaml(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict[str, object]]:
    return {name: load_yaml(SCHEMA_ROOT / name) for name in AI_MVP_SCHEMAS}


@pytest.fixture(scope="module")
def registry(schemas: dict[str, dict[str, object]]) -> Registry:
    resources = []
    for schema in schemas.values():
        schema_id = schema["$id"]
        assert isinstance(schema_id, str)
        resources.append((schema_id, Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def validator(
    name: str,
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        schemas[name],
        registry=registry,
        format_checker=jsonschema.FormatChecker(),
    )


def test_all_ai_mvp_schemas_are_valid_json_schema(
    schemas: dict[str, dict[str, object]],
) -> None:
    for schema in schemas.values():
        jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(
    ("schema_name", "document_path"),
    [
        (
            "runtime-config.schema.yaml",
            PROJECT_ROOT / "configs" / "alphamind" / "runtime.example.yaml",
        ),
        (
            "instrument-registry.schema.yaml",
            PROJECT_ROOT / "configs" / "alphamind" / "instruments.example.yaml",
        ),
        ("news-item.schema.yaml", FIXTURE_ROOT / "news-item.valid.yaml"),
        ("decision-context.schema.yaml", FIXTURE_ROOT / "decision-context.valid.yaml"),
        ("trade-action.schema.yaml", FIXTURE_ROOT / "trade-action.valid.yaml"),
        ("approval-event.schema.yaml", FIXTURE_ROOT / "approval-event.valid.yaml"),
        ("approval-record.schema.yaml", FIXTURE_ROOT / "approval-record.valid.yaml"),
        ("model-decision.schema.yaml", FIXTURE_ROOT / "model-decision.valid.yaml"),
        (
            "ai-profile.schema.yaml",
            PROJECT_ROOT / "configs" / "alphamind" / "ai-profile.example.yaml",
        ),
        (
            "news-source-registry.schema.yaml",
            PROJECT_ROOT / "configs" / "alphamind" / "news-sources.example.yaml",
        ),
        (
            "market-capability-snapshot.schema.yaml",
            PROJECT_ROOT / "configs" / "alphamind" / "market-capabilities.snapshot.json",
        ),
        (
            "telegram-notification.schema.yaml",
            FIXTURE_ROOT / "telegram-notification.valid.yaml",
        ),
    ],
)
def test_valid_examples_satisfy_contracts(
    schema_name: str,
    document_path: Path,
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    validator(schema_name, schemas, registry).validate(load_yaml(document_path))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: config["execution"].update({"approval_required": False}),
        lambda config: config["execution"]["futures"].update({"global_max_leverage": 3}),
        lambda config: config["approval"].update({"bot_token": "must-not-be-stored"}),
        lambda config: config["scheduler"].update({"decision_cycle_minutes": 1}),
        lambda config: config["decision"].update(
            {"ai_profile_path": "configs/alphamind/unreviewed-provider.yaml"}
        ),
    ],
)
def test_runtime_config_rejects_unsafe_or_ambiguous_values(
    mutate: object,
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    config = load_yaml(PROJECT_ROOT / "configs" / "alphamind" / "runtime.example.yaml")
    assert callable(mutate)
    mutate(config)

    with pytest.raises(jsonschema.ValidationError):
        validator("runtime-config.schema.yaml", schemas, registry).validate(config)


def test_instrument_registry_is_extensible_without_schema_edits(
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    instrument_registry = load_yaml(
        PROJECT_ROOT / "configs" / "alphamind" / "instruments.example.yaml"
    )
    instruments = instrument_registry["instruments"]
    assert isinstance(instruments, list)
    instruments.append(
        {
            "id": "XRP",
            "enabled": True,
            "spot": {"enabled": True, "pair": "XRP/USDT"},
            "futures": {
                "enabled": False,
                "pair": None,
                "allow_long": False,
                "allow_short": False,
                "max_leverage": None,
            },
        }
    )

    validator("instrument-registry.schema.yaml", schemas, registry).validate(instrument_registry)
    rules = schemas["instrument-registry.schema.yaml"]["x-business-rules"]
    assert isinstance(rules, list)
    assert any("unique" in rule for rule in rules)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda registry: registry["instruments"][0]["futures"].update({"max_leverage": 2}),
        lambda registry: registry["instruments"][0]["spot"].update({"enabled": False}),
        lambda registry: registry["instruments"][0].update({"symbol_is_hardcoded": True}),
    ],
)
def test_instrument_registry_rejects_invalid_market_contracts(
    mutate: object,
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    instrument_registry = load_yaml(
        PROJECT_ROOT / "configs" / "alphamind" / "instruments.example.yaml"
    )
    assert callable(mutate)
    mutate(instrument_registry)

    with pytest.raises(jsonschema.ValidationError):
        validator("instrument-registry.schema.yaml", schemas, registry).validate(
            instrument_registry
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda action: action.update({"market": "spot", "requested_leverage": "2"}),
        lambda action: action.update({"side": "short", "market": "spot"}),
        lambda action: action.update({"stop_loss": None}),
        lambda action: action.update({"news_refs": []}),
        lambda action: action.update({"unexpected_execution_quantity": "1"}),
    ],
)
def test_trade_action_rejects_model_output_that_cannot_enter_approval(
    mutate: object,
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    action = load_yaml(FIXTURE_ROOT / "trade-action.valid.yaml")
    assert callable(mutate)
    mutate(action)

    with pytest.raises(jsonschema.ValidationError):
        validator("trade-action.schema.yaml", schemas, registry).validate(action)


def test_hold_action_cannot_smuggle_execution_fields(
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    action = load_yaml(FIXTURE_ROOT / "trade-action.valid.yaml")
    action.update(
        {
            "action": "HOLD",
            "order_preference": "none",
            "entry": {"min": "155", "max": "156"},
            "stop_loss": None,
            "take_profit": [],
            "reason_codes": ["NO_EDGE"],
            "news_refs": [],
        }
    )

    with pytest.raises(jsonschema.ValidationError):
        validator("trade-action.schema.yaml", schemas, registry).validate(action)


def test_decision_context_applies_nested_news_contract(
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    context = load_yaml(FIXTURE_ROOT / "decision-context.valid.yaml")
    invalid_context = deepcopy(context)
    invalid_context["news_items"][0]["untrusted_external_content"] = False

    with pytest.raises(jsonschema.ValidationError):
        validator("decision-context.schema.yaml", schemas, registry).validate(invalid_context)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rsi", "100.00000001"),
        ("adx", "-1"),
        ("ema_alignment", "sideways"),
        ("candlestick_pattern", "three_white_soldiers"),
        ("pattern_semantic", "buy immediately"),
    ],
)
def test_decision_context_v2_rejects_unbounded_or_free_form_feature_semantics(
    field: str,
    value: str,
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    context = load_yaml(FIXTURE_ROOT / "decision-context.valid.yaml")
    context["instruments"][0]["features"][field] = value

    with pytest.raises(jsonschema.ValidationError):
        validator("decision-context.schema.yaml", schemas, registry).validate(context)


def test_decision_context_v2_accepts_explicitly_unavailable_expanded_features(
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    context = load_yaml(FIXTURE_ROOT / "decision-context.valid.yaml")
    features = context["instruments"][0]["features"]
    for field in ("rsi", "adx", "ema_alignment", "candlestick_pattern", "pattern_semantic"):
        features[field] = None

    validator("decision-context.schema.yaml", schemas, registry).validate(context)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update({"to_state": "EXECUTED"}),
        lambda event: event.update({"nonce": "raw-callback-nonce"}),
        lambda event: event["actor"].update({"actor_type": "system"}),
        lambda event: event.update({"nonce_sha256": None}),
    ],
)
def test_approval_event_rejects_illegal_or_unauthorized_transition(
    mutate: object,
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    event = load_yaml(FIXTURE_ROOT / "approval-event.valid.yaml")
    assert callable(mutate)
    mutate(event)

    with pytest.raises(jsonschema.ValidationError):
        validator("approval-event.schema.yaml", schemas, registry).validate(event)


def test_approval_record_reuses_action_and_event_contracts(
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    record = load_yaml(FIXTURE_ROOT / "approval-record.valid.yaml")
    invalid_record = deepcopy(record)
    invalid_record["action"]["market"] = "spot"
    invalid_record["action"]["side"] = "short"

    with pytest.raises(jsonschema.ValidationError):
        validator("approval-record.schema.yaml", schemas, registry).validate(invalid_record)


def test_ai_profile_pins_the_versioned_prompt_content(
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    profile = load_yaml(PROJECT_ROOT / "configs" / "alphamind" / "ai-profile.example.yaml")
    validator("ai-profile.schema.yaml", schemas, registry).validate(profile)
    prompt = PROJECT_ROOT / "prompts" / "ai" / "trade-decision-v2.md"
    actual_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()

    assert profile["prompt"]["sha256"] == actual_sha256

    deepseek = load_yaml(PROJECT_ROOT / "configs" / "alphamind" / "ai-profile.deepseek-test.yaml")
    validator("ai-profile.schema.yaml", schemas, registry).validate(deepseek)
    assert deepseek["prompt"]["sha256"] == actual_sha256


def test_ai_cost_caps_cover_one_full_attempt_and_the_half_hour_schedule() -> None:
    runtime = load_yaml(PROJECT_ROOT / "configs" / "alphamind" / "runtime.example.yaml")
    for filename in ("ai-profile.example.yaml", "ai-profile.deepseek-test.yaml"):
        profile = load_yaml(PROJECT_ROOT / "configs" / "alphamind" / filename)
        request = profile["request"]
        cost = profile["cost"]
        assert isinstance(request, dict)
        assert isinstance(cost, dict)

        one_million = Decimal("1000000")
        maximum_attempt_cost = (
            Decimal(request["max_input_tokens"])
            * Decimal(cost["input_per_million_tokens"])
            / one_million
            + Decimal(request["max_output_tokens"])
            * Decimal(cost["output_per_million_tokens"])
            / one_million
        )
        per_cycle_cap = Decimal(cost["maximum_cost_per_cycle"])
        cycles_per_day = Decimal(1440 // runtime["scheduler"]["decision_cycle_minutes"])

        assert maximum_attempt_cost <= per_cycle_cap
        assert per_cycle_cap * cycles_per_day <= Decimal(cost["maximum_cost_per_utc_day"])


def test_trade_decision_prompt_keeps_model_read_only_and_news_untrusted() -> None:
    prompt = (PROJECT_ROOT / "prompts" / "ai" / "trade-decision-v2.md").read_text(encoding="utf-8")
    normalized = prompt.lower()

    assert "no authority to place orders" in normalized
    assert "untrusted quoted data" in normalized
    assert "telegram approval" in normalized
    assert "prefer `hold`" in normalized
    assert "martingale" in normalized
    assert "do not emit executable quantity" in normalized
    assert "rsi momentum" in normalized
    assert "adx trend strength" in normalized
    assert "ema alignment" in normalized
    assert "indicators conflict" in normalized
    assert "hidden chain-of-thought" in normalized
    assert "[confidence: high]" in normalized


@pytest.mark.parametrize(
    "mutate",
    [
        lambda profile: profile["model"].update({"id": "chat-latest"}),
        lambda profile: profile["request"].update({"tools_enabled": True}),
        lambda profile: profile["request"].update({"store_response": True}),
        lambda profile: profile["retry"].update({"maximum_attempts": 3}),
        lambda profile: profile["cost"].update({"input_per_million_tokens": 2.5}),
        lambda profile: profile["failure_policy"].update({"provider_error": "CONTINUE"}),
        lambda profile: profile["provider"].update({"api_key": "must-not-be-stored"}),
    ],
)
def test_ai_profile_rejects_mutable_or_privileged_model_configuration(
    mutate: object,
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    profile = load_yaml(PROJECT_ROOT / "configs" / "alphamind" / "ai-profile.example.yaml")
    assert callable(mutate)
    mutate(profile)

    with pytest.raises(jsonschema.ValidationError):
        validator("ai-profile.schema.yaml", schemas, registry).validate(profile)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda source_registry: source_registry.update({"sources": source_registry["sources"][:1]}),
        lambda source_registry: source_registry["sources"][0].update(
            {"untrusted_external_content": False}
        ),
        lambda source_registry: source_registry["sources"][0].update(
            {"endpoint": "https://example.invalid/announcements"}
        ),
        lambda source_registry: source_registry["sources"][1].update(
            {"request_params": {"limit": 10}}
        ),
        lambda source_registry: source_registry["sources"][2].update({"fetch_full_article": True}),
    ],
)
def test_news_source_registry_rejects_unsafe_or_unbounded_sources(
    mutate: object,
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    source_registry = load_yaml(
        PROJECT_ROOT / "configs" / "alphamind" / "news-sources.example.yaml"
    )
    assert callable(mutate)
    mutate(source_registry)

    with pytest.raises(jsonschema.ValidationError):
        validator("news-source-registry.schema.yaml", schemas, registry).validate(source_registry)


def test_model_decision_is_provider_strict_but_still_requires_runtime_validation(
    schemas: dict[str, dict[str, object]],
    registry: Registry,
) -> None:
    decision = load_yaml(FIXTURE_ROOT / "model-decision.valid.yaml")
    validator("model-decision.schema.yaml", schemas, registry).validate(decision)

    invalid_decision = deepcopy(decision)
    invalid_decision["actions"][0]["execution_quantity"] = "1"
    with pytest.raises(jsonschema.ValidationError):
        validator("model-decision.schema.yaml", schemas, registry).validate(invalid_decision)

    business_invalid = deepcopy(decision)
    business_invalid["actions"][0]["market"] = "spot"
    business_invalid["actions"][0]["side"] = "short"
    validator("model-decision.schema.yaml", schemas, registry).validate(business_invalid)
    with pytest.raises(jsonschema.ValidationError):
        validator("trade-action.schema.yaml", schemas, registry).validate(
            business_invalid["actions"][0]
        )
