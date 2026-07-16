import json
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest
import yaml

from alphamind.research.experiment_registry import (
    assert_selectable_experiment,
    canonical_json_bytes,
    compare_reproduction,
    file_sha256,
    finalize_experiment,
    locate_experiment,
    record_review,
    register_experiment,
    registration_sha256,
)

PROJECT_ROOT = Path(__file__).parents[2]


def _write(root: Path, relative: str, content: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return file_sha256(path)


def _prepare_repository(root: Path, *, maximum_trials: int = 3) -> dict[str, str]:
    hashes = {
        "hypothesis": _write(root, "research/hypotheses/hypothesis.yaml", "hypothesis\n"),
        "strategy_card": _write(root, "research/strategy_cards/strategy.yaml", "strategy-card\n"),
        "strategy_config": _write(root, "configs/research/strategy.toml", "entry = 20\n"),
        "runtime_lock": _write(root, "configs/common/runtime.toml", "python = '3.12.9'\n"),
        "dataset_manifest": _write(root, "data/manifests/dataset.yaml", "dataset: fixture\n"),
        "cost_model": _write(root, "configs/research/cost.toml", "fee = '0.001'\n"),
    }
    registry = {
        "entries": [],
        "failed_trials_must_be_retained": True,
        "maximum_trials": maximum_trials,
        "schema_version": 1,
        "strategy_card_path": "research/strategy_cards/strategy.yaml",
        "strategy_card_sha256": hashes["strategy_card"],
        "strategy_id": "donchian_trend",
        "strategy_version": "0.1.0",
    }
    _write(
        root,
        "research/experiments/trial-registry.json",
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return hashes


def _experiment(
    hashes: dict[str, str],
    *,
    trial_index: int = 1,
    suffix: str = "0123456789ab",
    maximum_trials: int = 3,
) -> dict[str, object]:
    experiment: dict[str, object] = {
        "artifacts": [],
        "completed_at_utc": None,
        "cost_model": {
            "all_costs_nonnegative": True,
            "config_path": "configs/research/cost.toml",
            "config_sha256": hashes["cost_model"],
            "fee_rate": "0.001",
            "gap_buffer_rate": "0.002",
            "slippage_rate": "0.001",
            "version": "research-cost-v1",
        },
        "created_at_utc": "2026-07-16T01:00:00Z",
        "dataset": {
            "dataset_id": "fixture-development-v1",
            "development_end_exclusive": "2026-07-01T00:00:00Z",
            "development_start": "2022-01-01T00:00:00Z",
            "feature_version": "ohlcv-v1",
            "final_holdout_end_exclusive": "2026-07-01T00:00:00Z",
            "final_holdout_start": "2025-07-01T00:00:00Z",
            "holdout_access_count": 1,
            "holdout_first_access_commit": "9" * 40,
            "holdout_state": "DEGRADED_TO_DEVELOPMENT",
            "manifest_path": "data/manifests/dataset.yaml",
            "manifest_sha256": hashes["dataset_manifest"],
        },
        "evidence_layer": "historical_backtest",
        "experiment_id": f"exp-20260716T010000Z-{suffix}",
        "hypothesis": {
            "economic_rationale": "价格突破后的有限持续性可能补偿假突破和全部预注册交易成本。",
            "falsification_conditions": ["任一预注册证伪条件成立即视为失败"],
            "hypothesis_id": "hyp-donchian-trend-v1",
            "hypothesis_path": "research/hypotheses/hypothesis.yaml",
            "hypothesis_sha256": hashes["hypothesis"],
            "pass_condition": "全部预注册门槛同时满足",
            "primary_metric": "expectancy_r",
            "statement": "Donchian 趋势规则在冻结开发池中能够捕捉持续性价格突破。",
        },
        "owner": "alphamind-research",
        "registration_sha256": "",
        "result": None,
        "review_result": "PENDING",
        "runtime": {
            "ccxt_version": "4.5.61",
            "freqtrade_version": "2026.6",
            "python_version": "3.12.9",
            "random_seed": 20260716,
            "runtime_lock_path": "configs/common/runtime.toml",
            "runtime_lock_sha256": hashes["runtime_lock"],
        },
        "schema_version": 1,
        "started_at_utc": None,
        "status": "PRE_REGISTERED",
        "strategy": {
            "parameters": {"entry_window": 20, "exit_window": 10},
            "project_commit": "1" * 40,
            "strategy_card_path": "research/strategy_cards/strategy.yaml",
            "strategy_card_sha256": hashes["strategy_card"],
            "strategy_config_path": "configs/research/strategy.toml",
            "strategy_config_sha256": hashes["strategy_config"],
            "strategy_id": "donchian_trend",
            "strategy_version": "0.1.0",
        },
        "trial_budget": {
            "maximum_trials": maximum_trials,
            "parameter_selection_allowed": True,
            "prior_result_used": False,
            "trial_index": trial_index,
        },
        "validation": {
            "folds": ["WF-01"],
            "lookahead_analysis_required": True,
            "metrics": ["expectancy_r", "maximum_drawdown"],
            "recursive_analysis_required": True,
            "regime_reporting_required": True,
            "slices": {
                "holdout": [],
                "stress": [
                    {
                        "end_exclusive": "2022-07-01T00:00:00Z",
                        "slice_id": "rapid-crash",
                        "start": "2022-05-01T00:00:00Z",
                    }
                ],
                "train": [
                    {
                        "end_exclusive": "2023-07-01T00:00:00Z",
                        "slice_id": "WF-01-train",
                        "start": "2022-01-01T00:00:00Z",
                    }
                ],
                "validation": [
                    {
                        "end_exclusive": "2024-01-01T00:00:00Z",
                        "slice_id": "WF-01-validation",
                        "start": "2023-07-01T00:00:00Z",
                    }
                ],
            },
            "walk_forward_manifest": "data/manifests/regime-manifest.yaml",
        },
    }
    experiment["registration_sha256"] = registration_sha256(experiment)
    return experiment


def _validator(name: str) -> jsonschema.Draft202012Validator:
    schema = yaml.safe_load((PROJECT_ROOT / "data/schemas" / name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def test_registered_experiment_is_locatable_and_report_separates_slices(tmp_path: Path) -> None:
    hashes = _prepare_repository(tmp_path)
    experiment = _experiment(hashes)

    entry = register_experiment(tmp_path, experiment)
    located = locate_experiment(tmp_path, str(experiment["experiment_id"]))
    report = (tmp_path / str(entry["report_path"])).read_text(encoding="utf-8")
    manifest = json.loads(
        (tmp_path / str(entry["artifact_manifest_path"])).read_text(encoding="utf-8")
    )

    assert located == entry
    assert all(
        heading in report
        for heading in (
            "## Train slices",
            "## Validation slices",
            "## Holdout slices",
            "## Stress slices",
        )
    )
    _validator("experiment.schema.yaml").validate(experiment)
    _validator("artifact-manifest.schema.yaml").validate(manifest)
    with pytest.raises(ValueError, match="already registered"):
        register_experiment(tmp_path, experiment)
    with pytest.raises(KeyError, match="not registered"):
        assert_selectable_experiment(tmp_path, "exp-20260716T010000Z-ffffffffffff")


def test_restricted_jcs_profile_rejects_float_and_non_ascii_keys() -> None:
    with pytest.raises(TypeError, match="decimals as strings"):
        canonical_json_bytes({"metric": 0.1})
    with pytest.raises(ValueError, match="non-ASCII key"):
        canonical_json_bytes({"指标": "0.1"})


def test_failed_trial_is_retained_and_cannot_be_approved(tmp_path: Path) -> None:
    hashes = _prepare_repository(tmp_path)
    experiment = _experiment(hashes)
    experiment_id = str(experiment["experiment_id"])
    register_experiment(tmp_path, experiment)

    entry = finalize_experiment(
        tmp_path,
        experiment_id,
        status="REJECTED",
        started_at_utc="2026-07-16T01:01:00Z",
        completed_at_utc="2026-07-16T01:02:00Z",
        result={
            "outcome": "FAIL",
            "primary_metric_value": "-0.10",
            "production_write_path_verified": False,
            "reason_codes": ["non_positive_expectancy"],
        },
        trades=[],
        metrics={"expectancy_r": "-0.10"},
    )

    assert entry["status"] == "REJECTED"
    assert locate_experiment(tmp_path, experiment_id)["outcome"] == "FAIL"
    with pytest.raises(ValueError, match="only COMPLETED/PASS"):
        record_review(
            tmp_path,
            experiment_id,
            review_result="APPROVED",
            reviewed_at_utc="2026-07-16T01:03:00Z",
            reviewer="fixture-reviewer",
            reason_codes=["must_retain_failure"],
        )
    registry = json.loads(
        (tmp_path / "research/experiments/trial-registry.json").read_text(encoding="utf-8")
    )
    assert [item["experiment_id"] for item in registry["entries"]] == [experiment_id]


def test_passed_approved_trial_is_selectable_and_reproduction_is_bounded(tmp_path: Path) -> None:
    hashes = _prepare_repository(tmp_path)
    experiment = _experiment(hashes)
    experiment_id = str(experiment["experiment_id"])
    trades = [{"entry": "100", "exit": "110", "pair": "BTC/USDT"}]
    metrics = {"expectancy_r": "0.100000", "maximum_drawdown": "0.050000"}
    register_experiment(tmp_path, experiment)
    finalize_experiment(
        tmp_path,
        experiment_id,
        status="COMPLETED",
        started_at_utc="2026-07-16T01:01:00Z",
        completed_at_utc="2026-07-16T01:02:00Z",
        result={
            "outcome": "PASS",
            "primary_metric_value": "0.100000",
            "production_write_path_verified": False,
            "reason_codes": ["all_preregistered_thresholds_passed"],
        },
        trades=trades,
        metrics=metrics,
    )
    with pytest.raises(ValueError, match="not eligible"):
        assert_selectable_experiment(tmp_path, experiment_id)

    record_review(
        tmp_path,
        experiment_id,
        review_result="APPROVED",
        reviewed_at_utc="2026-07-16T01:03:00Z",
        reviewer="fixture-reviewer",
        reason_codes=["independent_reproduction_passed"],
    )
    assert_selectable_experiment(tmp_path, experiment_id)
    completion = json.loads(
        (tmp_path / f"research/experiments/{experiment_id}/completion.json").read_text(
            encoding="utf-8"
        )
    )
    reviewed_entry = locate_experiment(tmp_path, experiment_id)
    reviewed_manifest = json.loads(
        (tmp_path / str(reviewed_entry["artifact_manifest_path"])).read_text(encoding="utf-8")
    )
    registry = json.loads(
        (tmp_path / "research/experiments/trial-registry.json").read_text(encoding="utf-8")
    )
    _validator("experiment.schema.yaml").validate(completion)
    _validator("artifact-manifest.schema.yaml").validate(reviewed_manifest)
    _validator("trial-registry.schema.yaml").validate(registry)
    assert (
        compare_reproduction(
            trades,
            list(reversed(trades)),
            metrics,
            {"expectancy_r": "0.100001", "maximum_drawdown": "0.050000"},
            metric_tolerance=Decimal("0.000001"),
        )
        == []
    )
    assert compare_reproduction(
        trades,
        [{"entry": "101", "exit": "110", "pair": "BTC/USDT"}],
        metrics,
        {"expectancy_r": "0.100002", "maximum_drawdown": "0.050000"},
        metric_tolerance=Decimal("0.000001"),
    ) == ["trade_list_mismatch", "metric_mismatch:expectancy_r"]
