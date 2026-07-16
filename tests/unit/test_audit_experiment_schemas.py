from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest
import yaml

PROJECT_ROOT = Path(__file__).parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "data" / "schemas"


def load_validator(name: str) -> jsonschema.Draft202012Validator:
    schema = yaml.safe_load((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


@pytest.fixture(scope="module")
def audit_validator() -> jsonschema.Draft202012Validator:
    return load_validator("audit-event.schema.yaml")


@pytest.fixture(scope="module")
def experiment_validator() -> jsonschema.Draft202012Validator:
    return load_validator("experiment.schema.yaml")


def valid_replay_audit_event() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "018f1000-1234-7abc-8def-0123456789ab",
        "event_type": "reconciliation_result",
        "event_version": 1,
        "occurred_at_utc": "2026-07-16T01:00:00Z",
        "recorded_at_utc": "2026-07-16T01:00:01Z",
        "producer": {
            "component": "replay_runner",
            "instance_id": "replay-p0-07",
            "sequence": 1,
        },
        "execution_context": {
            "environment": "replay",
            "evidence_layer": "deterministic_replay",
            "credentials_profile": "fixture_only",
            "trade_write_permitted": False,
            "production_write_path_verified": False,
        },
        "provenance": {
            "project_commit": "1" * 40,
            "strategy_id": "donchian_trend",
            "strategy_version": "0.1.0",
            "strategy_config_sha256": "2" * 64,
            "runtime_lock_sha256": "3" * 64,
            "risk_snapshot_id": None,
            "experiment_id": "exp-20260716T010000Z-0123456789ab",
        },
        "runtime_links": [
            {
                "source": "freqtrade_runtime_db",
                "reference_type": "order_id",
                "reference_id": "fixture-order-1",
                "read_only": True,
            }
        ],
        "reason_codes": ["partial_fill_reconciled"],
        "payload_schema": "schemas/reconciliation-result/v1",
        "payload": {"filled": "0.001", "remaining": "0.002"},
        "payload_sha256": "4" * 64,
        "event_content_sha256": "5" * 64,
        "runtime_authority": False,
        "contains_secrets": False,
    }


def valid_preregistered_experiment() -> dict[str, object]:
    return {
        "schema_version": 1,
        "experiment_id": "exp-20260716T010000Z-0123456789ab",
        "created_at_utc": "2026-07-16T01:00:00Z",
        "status": "PRE_REGISTERED",
        "owner": "alphamind-research",
        "evidence_layer": "historical_backtest",
        "hypothesis": {
            "statement": "Donchian 趋势规则在冻结开发池中能够捕捉持续性价格突破。",
            "economic_rationale": "价格突破后的行为延续可能补偿低胜率、手续费和保守滑点成本。",
            "primary_metric": "expectancy_r",
            "pass_condition": "全部预注册门槛同时满足",
            "falsification_conditions": ["任一硬性风险或统计门槛失败"],
        },
        "strategy": {
            "strategy_id": "donchian_trend",
            "strategy_version": "0.1.0",
            "project_commit": "1" * 40,
            "strategy_config_sha256": "2" * 64,
            "parameters": {"entry_window": 20, "exit_window": 10},
        },
        "runtime": {
            "runtime_lock_sha256": "3" * 64,
            "python_version": "3.12.9",
            "freqtrade_version": "2026.6",
            "ccxt_version": "4.5.61",
            "random_seed": None,
        },
        "dataset": {
            "dataset_id": "bybit-spot-development-v1",
            "manifest_path": "data/manifests/regime-manifest.yaml",
            "manifest_sha256": "4" * 64,
            "development_start": "2022-01-01T00:00:00Z",
            "development_end_exclusive": "2025-07-01T00:00:00Z",
            "final_holdout_start": "2025-07-01T00:00:00Z",
            "final_holdout_end_exclusive": "2026-07-01T00:00:00Z",
            "holdout_state": "SEALED_UNREAD",
            "holdout_access_count": 0,
            "holdout_first_access_commit": None,
        },
        "validation": {
            "walk_forward_manifest": "data/manifests/regime-manifest.yaml",
            "folds": ["WF-01", "WF-02", "WF-03"],
            "metrics": ["expectancy_r", "maximum_drawdown"],
            "regime_reporting_required": True,
            "lookahead_analysis_required": True,
            "recursive_analysis_required": True,
        },
        "trial_budget": {
            "trial_index": 1,
            "maximum_trials": 1,
            "prior_result_used": False,
            "parameter_selection_allowed": False,
        },
        "cost_model": {
            "fee_rate": "0.001",
            "slippage_rate": "0.001",
            "gap_buffer_rate": "0.002",
            "all_costs_nonnegative": True,
        },
        "started_at_utc": None,
        "completed_at_utc": None,
        "result": None,
        "artifacts": [],
        "registration_sha256": "5" * 64,
    }


def test_replay_audit_event_is_non_authoritative_and_has_no_trade_permission(
    audit_validator: jsonschema.Draft202012Validator,
) -> None:
    audit_validator.validate(valid_replay_audit_event())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update({"runtime_authority": True}),
        lambda event: event.update({"contains_secrets": True}),
        lambda event: event["execution_context"].update({"trade_write_permitted": True}),
        lambda event: event["execution_context"].update({"production_write_path_verified": True}),
        lambda event: event.update({"unexpected": True}),
    ],
)
def test_unsafe_audit_event_is_rejected(
    audit_validator: jsonschema.Draft202012Validator,
    mutate: object,
) -> None:
    event = deepcopy(valid_replay_audit_event())
    assert callable(mutate)
    mutate(event)

    with pytest.raises(jsonschema.ValidationError):
        audit_validator.validate(event)


def test_preregistered_and_completed_experiments_are_valid(
    experiment_validator: jsonschema.Draft202012Validator,
) -> None:
    preregistered = valid_preregistered_experiment()
    experiment_validator.validate(preregistered)

    completed = deepcopy(preregistered)
    completed.update(
        {
            "status": "COMPLETED",
            "started_at_utc": "2026-07-16T01:01:00Z",
            "completed_at_utc": "2026-07-16T01:02:00Z",
            "result": {
                "outcome": "INCONCLUSIVE",
                "primary_metric_value": "0.10",
                "reason_codes": ["insufficient_independent_events"],
                "production_write_path_verified": False,
            },
            "artifacts": [
                {
                    "role": "report",
                    "path": "artifacts/exp-20260716/report.json",
                    "sha256": "6" * 64,
                }
            ],
        }
    )
    experiment_validator.validate(completed)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda experiment: experiment.update(
            {
                "result": {
                    "outcome": "PASS",
                    "primary_metric_value": "1.0",
                    "reason_codes": ["passed"],
                    "production_write_path_verified": False,
                }
            }
        ),
        lambda experiment: experiment["dataset"].update({"holdout_access_count": 1}),
        lambda experiment: experiment["cost_model"].update({"fee_rate": -0.001}),
        lambda experiment: experiment.update({"unexpected": True}),
    ],
)
def test_invalid_experiment_contract_is_rejected(
    experiment_validator: jsonschema.Draft202012Validator,
    mutate: object,
) -> None:
    experiment = deepcopy(valid_preregistered_experiment())
    assert callable(mutate)
    mutate(experiment)

    with pytest.raises(jsonschema.ValidationError):
        experiment_validator.validate(experiment)
