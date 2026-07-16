import json
from pathlib import Path

import jsonschema
import yaml

from scripts.build_walk_forward_report import _folds, _load_config, _registration, _trials

PROJECT_ROOT = Path(__file__).parents[2]


def test_walk_forward_matrix_is_preregistered_one_parameter_at_a_time() -> None:
    config = _load_config(PROJECT_ROOT)
    trials = _trials(config)
    baseline = trials[0]

    assert len(trials) == 13
    assert [trial.trial_index for trial in trials] == list(range(1, 14))
    assert baseline.trial_id == "baseline"
    for trial in trials[1:]:
        differences = sum(
            (
                trial.entry_window != baseline.entry_window,
                trial.exit_window != baseline.exit_window,
                trial.atr_period != baseline.atr_period,
                trial.stop_multiple != baseline.stop_multiple,
            )
        )
        assert differences == 1


def test_registration_covers_three_expanding_folds_without_holdout() -> None:
    config = _load_config(PROJECT_ROOT)
    folds = _folds(PROJECT_ROOT, config)
    experiment = _registration(PROJECT_ROOT, config, folds, _trials(config)[0])
    schema = yaml.safe_load(
        (PROJECT_ROOT / "data/schemas/experiment.schema.yaml").read_text(encoding="utf-8")
    )

    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        experiment
    )
    assert [fold.fold_id for fold in folds] == ["WF-01", "WF-02", "WF-03"]
    assert all(fold.train_start == folds[0].train_start for fold in folds)
    assert experiment["validation"]["slices"]["holdout"] == []
    assert experiment["dataset"]["holdout_state"] == "DEGRADED_TO_DEVELOPMENT"
    assert folds[-1].validation_end_exclusive.isoformat() == "2025-07-01T00:00:00+00:00"


def test_committed_trial_artifacts_remain_pending_and_schema_valid() -> None:
    experiment_schema = yaml.safe_load(
        (PROJECT_ROOT / "data/schemas/experiment.schema.yaml").read_text(encoding="utf-8")
    )
    manifest_schema = yaml.safe_load(
        (PROJECT_ROOT / "data/schemas/artifact-manifest.schema.yaml").read_text(encoding="utf-8")
    )
    registry_schema = yaml.safe_load(
        (PROJECT_ROOT / "data/schemas/trial-registry.schema.yaml").read_text(encoding="utf-8")
    )
    registry = json.loads(
        (PROJECT_ROOT / "research/experiments/trial-registry.json").read_text(encoding="utf-8")
    )
    summary = json.loads(
        (PROJECT_ROOT / "research/reports/walk-forward/p2-05-v1/summary.json").read_text(
            encoding="utf-8"
        )
    )

    jsonschema.Draft202012Validator(registry_schema).validate(registry)
    assert len(registry["entries"]) == 13
    assert summary["selection"]["parameter_selection"] is None
    assert summary["selection"]["parameter_selection_blocker"] == (
        "independent_review_and_p2_06_pending"
    )
    for entry in registry["entries"]:
        assert entry["status"] == "COMPLETED"
        assert entry["outcome"] == "PASS"
        assert entry["review_result"] == "PENDING"
        completion = json.loads(
            (PROJECT_ROOT / entry["registration_path"])
            .with_name("completion.json")
            .read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (PROJECT_ROOT / entry["artifact_manifest_path"]).read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(
            experiment_schema, format_checker=jsonschema.FormatChecker()
        ).validate(completion)
        jsonschema.Draft202012Validator(
            manifest_schema, format_checker=jsonschema.FormatChecker()
        ).validate(manifest)
