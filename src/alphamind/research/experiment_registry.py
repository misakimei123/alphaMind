"""P1-06 实验预注册、artifact 追溯和策略选择门禁。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

REGISTRY_PATH = Path("research/experiments/trial-registry.json")
_RESULT_FIELDS = {
    "status",
    "started_at_utc",
    "completed_at_utc",
    "result",
    "artifacts",
    "review_result",
    "registration_sha256",
}
_FINAL_STATUSES = {"COMPLETED", "REJECTED", "INVALIDATED"}


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{name} must be an object with string keys")
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _canonical_value(value: object, path: str = "$") -> None:
    """限制到当前 JCS 合同需要的 JSON 子集，主动拒绝浮点和非 JSON 类型。"""

    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise ValueError(f"{path} integer exceeds the JCS interoperable range")
        return
    if isinstance(value, float):
        raise TypeError(f"{path} must encode decimals as strings, not float")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _canonical_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string key")
            if not key.isascii():
                raise ValueError(
                    f"{path} contains a non-ASCII key outside the restricted JCS profile"
                )
            _canonical_value(item, f"{path}.{key}")
        return
    raise TypeError(f"{path} contains unsupported type {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """返回稳定 UTF-8 JSON；研究数值必须先转为精确十进制字符串。"""

    _canonical_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def registration_sha256(experiment: dict[str, object]) -> str:
    """只 hash 预注册不可变字段，生命周期结果与评审只能追加。"""

    frozen = {key: value for key, value in experiment.items() if key not in _RESULT_FIELDS}
    return canonical_json_sha256(frozen)


def manifest_content_sha256(manifest: dict[str, object]) -> str:
    content = {key: value for key, value in manifest.items() if key != "manifest_content_sha256"}
    return canonical_json_sha256(content)


def _repository_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes repository: {relative}")
    return candidate


def _verify_file(root: Path, relative: str, expected_sha256: str) -> None:
    path = _repository_path(root, relative)
    if not path.is_file():
        raise FileNotFoundError(relative)
    actual = file_sha256(path)
    if actual != expected_sha256:
        raise ValueError(
            f"sha256 mismatch for {relative}: expected {expected_sha256}, got {actual}"
        )


def _parse_utc(value: object, name: str) -> datetime:
    text = _string(value, name)
    if not text.endswith("Z"):
        raise ValueError(f"{name} must be UTC and end with Z")
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _input_files(experiment: dict[str, object]) -> list[tuple[str, str, str]]:
    hypothesis = _mapping(experiment.get("hypothesis"), "hypothesis")
    strategy = _mapping(experiment.get("strategy"), "strategy")
    runtime = _mapping(experiment.get("runtime"), "runtime")
    dataset = _mapping(experiment.get("dataset"), "dataset")
    cost_model = _mapping(experiment.get("cost_model"), "cost_model")
    return [
        (
            "hypothesis",
            _string(hypothesis.get("hypothesis_path"), "hypothesis.hypothesis_path"),
            _string(hypothesis.get("hypothesis_sha256"), "hypothesis.hypothesis_sha256"),
        ),
        (
            "strategy_card",
            _string(strategy.get("strategy_card_path"), "strategy.strategy_card_path"),
            _string(strategy.get("strategy_card_sha256"), "strategy.strategy_card_sha256"),
        ),
        (
            "strategy_config",
            _string(strategy.get("strategy_config_path"), "strategy.strategy_config_path"),
            _string(strategy.get("strategy_config_sha256"), "strategy.strategy_config_sha256"),
        ),
        (
            "runtime_lock",
            _string(runtime.get("runtime_lock_path"), "runtime.runtime_lock_path"),
            _string(runtime.get("runtime_lock_sha256"), "runtime.runtime_lock_sha256"),
        ),
        (
            "dataset_manifest",
            _string(dataset.get("manifest_path"), "dataset.manifest_path"),
            _string(dataset.get("manifest_sha256"), "dataset.manifest_sha256"),
        ),
        (
            "cost_model",
            _string(cost_model.get("config_path"), "cost_model.config_path"),
            _string(cost_model.get("config_sha256"), "cost_model.config_sha256"),
        ),
    ]


def validate_registration(root: Path, experiment: dict[str, object]) -> None:
    """验证 schema 之外必须由执行代码保证的跨字段和文件 hash 约束。"""

    if experiment.get("status") != "PRE_REGISTERED":
        raise ValueError("new experiment must be PRE_REGISTERED")
    if experiment.get("review_result") != "PENDING":
        raise ValueError("new experiment review_result must be PENDING")
    if any(
        experiment.get(name) is not None
        for name in ("started_at_utc", "completed_at_utc", "result")
    ):
        raise ValueError("pre-registered experiment must not contain lifecycle results")
    if experiment.get("artifacts") != []:
        raise ValueError("pre-registered experiment artifacts must be empty")

    trial = _mapping(experiment.get("trial_budget"), "trial_budget")
    trial_index = trial.get("trial_index")
    maximum_trials = trial.get("maximum_trials")
    if not isinstance(trial_index, int) or not isinstance(maximum_trials, int):
        raise TypeError("trial indices must be integers")
    if not 1 <= trial_index <= maximum_trials:
        raise ValueError("trial_index must be in [1, maximum_trials]")

    expected_registration_hash = _string(
        experiment.get("registration_sha256"), "registration_sha256"
    )
    actual_registration_hash = registration_sha256(experiment)
    if expected_registration_hash != actual_registration_hash:
        raise ValueError("registration_sha256 does not match immutable pre-registration fields")

    for _, relative, expected_hash in _input_files(experiment):
        _verify_file(root, relative, expected_hash)

    validation = _mapping(experiment.get("validation"), "validation")
    slices = _mapping(validation.get("slices"), "validation.slices")
    for category in ("train", "validation", "holdout", "stress"):
        for index, raw_slice in enumerate(_list(slices.get(category), f"slices.{category}")):
            item = _mapping(raw_slice, f"slices.{category}[{index}]")
            start = _parse_utc(item.get("start"), f"slices.{category}[{index}].start")
            end = _parse_utc(item.get("end_exclusive"), f"slices.{category}[{index}].end_exclusive")
            if end <= start:
                raise ValueError(f"slices.{category}[{index}] has an empty or reversed range")


def _read_json(path: Path) -> dict[str, object]:
    return _mapping(json.loads(path.read_text(encoding="utf-8")), str(path))


def _write_json_exclusive(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def _replace_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
    temporary.replace(path)


def _relative_experiment_path(experiment_id: str, filename: str) -> str:
    return f"research/experiments/{experiment_id}/{filename}"


def _slice_lines(experiment: dict[str, object], category: str) -> list[str]:
    validation = _mapping(experiment.get("validation"), "validation")
    slices = _mapping(validation.get("slices"), "validation.slices")
    values = _list(slices.get(category), f"slices.{category}")
    if not values:
        return ["- None (not available or not authorized)"]
    lines: list[str] = []
    for raw_slice in values:
        item = _mapping(raw_slice, f"slices.{category}")
        lines.append(
            "- "
            f"{_string(item.get('slice_id'), 'slice_id')}: "
            f"[{_string(item.get('start'), 'start')}, "
            f"{_string(item.get('end_exclusive'), 'end_exclusive')})"
        )
    return lines


def render_experiment_report(experiment: dict[str, object]) -> str:
    """生成固定章节报告，始终显式分开 train/validation/holdout/stress。"""

    strategy = _mapping(experiment.get("strategy"), "strategy")
    runtime = _mapping(experiment.get("runtime"), "runtime")
    dataset = _mapping(experiment.get("dataset"), "dataset")
    cost_model = _mapping(experiment.get("cost_model"), "cost_model")
    config_hash = _string(strategy.get("strategy_config_sha256"), "strategy.strategy_config_sha256")
    environment_hash = _string(runtime.get("runtime_lock_sha256"), "runtime.runtime_lock_sha256")
    result = experiment.get("result")
    if result is None:
        result_lines = ["- Outcome: Not run", "- Primary metric: Not available"]
    else:
        result_map = _mapping(result, "result")
        result_lines = [
            f"- Outcome: {_string(result_map.get('outcome'), 'result.outcome')}",
            f"- Primary metric: {result_map.get('primary_metric_value')}",
        ]

    sections = [
        f"# Experiment {_string(experiment.get('experiment_id'), 'experiment_id')}",
        "",
        "## Status",
        "",
        f"- Lifecycle: {_string(experiment.get('status'), 'status')}",
        f"- Review: {_string(experiment.get('review_result'), 'review_result')}",
        "",
        "## Provenance",
        "",
        f"- Commit: {_string(strategy.get('project_commit'), 'strategy.project_commit')}",
        f"- Config SHA-256: {config_hash}",
        f"- Data SHA-256: {_string(dataset.get('manifest_sha256'), 'dataset.manifest_sha256')}",
        f"- Environment SHA-256: {environment_hash}",
        f"- Feature version: {_string(dataset.get('feature_version'), 'dataset.feature_version')}",
        f"- Random seed: {runtime.get('random_seed')}",
        f"- Cost model version: {_string(cost_model.get('version'), 'cost_model.version')}",
    ]
    for title, category in (
        ("Train", "train"),
        ("Validation", "validation"),
        ("Holdout", "holdout"),
        ("Stress", "stress"),
    ):
        sections.extend(["", f"## {title} slices", "", *_slice_lines(experiment, category)])
    sections.extend(["", "## Result", "", *result_lines, ""])
    return "\n".join(sections)


def _manifest_file(
    direction: str,
    role: str,
    path: str,
    sha256: str,
    *,
    slice_name: str = "none",
    record_count: int | None = None,
) -> dict[str, object]:
    return {
        "direction": direction,
        "path": path,
        "record_count": record_count,
        "required": True,
        "role": role,
        "sha256": sha256,
        "slice": slice_name,
    }


def _build_manifest(
    root: Path,
    experiment: dict[str, object],
    generated_at_utc: str,
    output_paths: list[tuple[str, str, int | None]],
) -> dict[str, object]:
    files = [
        _manifest_file("INPUT", role, path, sha256, slice_name="all")
        for role, path, sha256 in _input_files(experiment)
    ]
    files.extend(
        _manifest_file(
            "OUTPUT",
            role,
            path,
            file_sha256(_repository_path(root, path)),
            record_count=record_count,
        )
        for role, path, record_count in output_paths
    )
    manifest: dict[str, object] = {
        "experiment_id": experiment["experiment_id"],
        "files": files,
        "generated_at_utc": generated_at_utc,
        "manifest_content_sha256": "",
        "registration_sha256": experiment["registration_sha256"],
        "schema_version": 1,
        "status": experiment["status"],
    }
    manifest["manifest_content_sha256"] = manifest_content_sha256(manifest)
    return manifest


def _load_registry(root: Path) -> tuple[Path, dict[str, object], list[object]]:
    path = root / REGISTRY_PATH
    registry = _read_json(path)
    return path, registry, _list(registry.get("entries"), "registry.entries")


def register_experiment(root: Path, experiment: dict[str, object]) -> dict[str, object]:
    """登记实验并生成不可变 registration、固定报告和 artifact manifest。"""

    validate_registration(root, experiment)
    registry_path, registry, entries = _load_registry(root)
    strategy = _mapping(experiment.get("strategy"), "strategy")
    trial = _mapping(experiment.get("trial_budget"), "trial_budget")
    experiment_id = _string(experiment.get("experiment_id"), "experiment_id")
    trial_index = trial.get("trial_index")

    if strategy.get("strategy_id") != registry.get("strategy_id"):
        raise ValueError("experiment strategy_id does not match registry")
    if strategy.get("strategy_version") != registry.get("strategy_version"):
        raise ValueError("experiment strategy_version does not match registry")
    if strategy.get("strategy_card_sha256") != registry.get("strategy_card_sha256"):
        raise ValueError("experiment Strategy Card hash does not match registry")
    if trial.get("maximum_trials") != registry.get("maximum_trials"):
        raise ValueError("experiment maximum_trials does not match registry")
    for raw_entry in entries:
        existing_entry = _mapping(raw_entry, "registry entry")
        if existing_entry.get("experiment_id") == experiment_id:
            raise ValueError(f"experiment already registered: {experiment_id}")
        if existing_entry.get("trial_index") == trial_index:
            raise ValueError(f"trial_index already registered: {trial_index}")

    registration_path = _relative_experiment_path(experiment_id, "registration.json")
    report_path = _relative_experiment_path(experiment_id, "report-pre-registered.md")
    manifest_path = _relative_experiment_path(
        experiment_id, "artifact-manifest-pre-registered.json"
    )
    _write_json_exclusive(_repository_path(root, registration_path), experiment)
    report_file = _repository_path(root, report_path)
    report_file.write_text(render_experiment_report(experiment), encoding="utf-8", newline="\n")
    manifest = _build_manifest(
        root,
        experiment,
        _string(experiment.get("created_at_utc"), "created_at_utc"),
        [("registration", registration_path, None), ("report", report_path, None)],
    )
    _write_json_exclusive(_repository_path(root, manifest_path), manifest)

    entry: dict[str, object] = {
        "artifact_manifest_path": manifest_path,
        "artifact_manifest_sha256": file_sha256(_repository_path(root, manifest_path)),
        "experiment_id": experiment_id,
        "outcome": None,
        "registration_path": registration_path,
        "registration_sha256": experiment["registration_sha256"],
        "report_path": report_path,
        "report_sha256": file_sha256(report_file),
        "review_result": "PENDING",
        "review_path": None,
        "review_sha256": None,
        "status": "PRE_REGISTERED",
        "trial_index": trial_index,
    }
    entries.append(entry)
    _replace_json(registry_path, registry)
    return entry


def _registry_entry(entries: list[object], experiment_id: str) -> dict[str, object]:
    for raw_entry in entries:
        entry = _mapping(raw_entry, "registry entry")
        if entry.get("experiment_id") == experiment_id:
            return entry
    raise KeyError(f"experiment is not registered: {experiment_id}")


def locate_experiment(root: Path, experiment_id: str) -> dict[str, object]:
    """按 ID 定位并验证当前 registry、registration、报告和 manifest 全部 hash。"""

    _, _, entries = _load_registry(root)
    entry = _registry_entry(entries, experiment_id)
    registration_path = _string(entry.get("registration_path"), "registration_path")
    registration = _read_json(_repository_path(root, registration_path))
    expected_registration_hash = _string(entry.get("registration_sha256"), "registration_sha256")
    if registration_sha256(registration) != expected_registration_hash:
        raise ValueError("registered immutable fields no longer match registration hash")

    report_path = _string(entry.get("report_path"), "report_path")
    report_sha = _string(entry.get("report_sha256"), "report_sha256")
    _verify_file(root, report_path, report_sha)
    manifest_path = _string(entry.get("artifact_manifest_path"), "artifact_manifest_path")
    manifest_sha = _string(entry.get("artifact_manifest_sha256"), "artifact_manifest_sha256")
    _verify_file(root, manifest_path, manifest_sha)
    manifest = _read_json(_repository_path(root, manifest_path))
    if manifest_content_sha256(manifest) != manifest.get("manifest_content_sha256"):
        raise ValueError("artifact manifest content hash mismatch")
    for raw_file in _list(manifest.get("files"), "manifest.files"):
        item = _mapping(raw_file, "manifest file")
        _verify_file(
            root,
            _string(item.get("path"), "manifest file path"),
            _string(item.get("sha256"), "manifest file sha256"),
        )
    return copy.deepcopy(entry)


def finalize_experiment(
    root: Path,
    experiment_id: str,
    *,
    status: str,
    started_at_utc: str,
    completed_at_utc: str,
    result: dict[str, object],
    trades: list[object],
    metrics: dict[str, object],
) -> dict[str, object]:
    """追加最终结果；FAIL/REJECTED/INVALIDATED 与成功结果走同一保留路径。"""

    if status not in _FINAL_STATUSES:
        raise ValueError(f"final status must be one of {sorted(_FINAL_STATUSES)}")
    if _parse_utc(completed_at_utc, "completed_at_utc") < _parse_utc(
        started_at_utc, "started_at_utc"
    ):
        raise ValueError("completed_at_utc must not precede started_at_utc")
    registry_path, registry, entries = _load_registry(root)
    entry = _registry_entry(entries, experiment_id)
    if entry.get("status") != "PRE_REGISTERED":
        raise ValueError("only a PRE_REGISTERED experiment can be finalized")
    registration = _read_json(
        _repository_path(root, _string(entry.get("registration_path"), "registration_path"))
    )

    final = copy.deepcopy(registration)
    final.update(
        {
            "artifacts": [],
            "completed_at_utc": completed_at_utc,
            "result": result,
            "review_result": "PENDING",
            "started_at_utc": started_at_utc,
            "status": status,
        }
    )
    outcome = _string(result.get("outcome"), "result.outcome")
    if status == "INVALIDATED" and outcome != "INVALIDATED":
        raise ValueError("INVALIDATED lifecycle requires INVALIDATED outcome")

    trades_path = _relative_experiment_path(experiment_id, "trades.json")
    metrics_path = _relative_experiment_path(experiment_id, "metrics.json")
    report_path = _relative_experiment_path(experiment_id, "report-final.md")
    completion_path = _relative_experiment_path(experiment_id, "completion.json")
    manifest_path = _relative_experiment_path(experiment_id, "artifact-manifest-final.json")
    _write_json_exclusive(_repository_path(root, trades_path), trades)
    _write_json_exclusive(_repository_path(root, metrics_path), metrics)
    report_file = _repository_path(root, report_path)
    report_file.write_text(render_experiment_report(final), encoding="utf-8", newline="\n")
    final["artifacts"] = [
        {"path": trades_path, "role": "trades", "sha256": file_sha256(root / trades_path)},
        {"path": metrics_path, "role": "metrics", "sha256": file_sha256(root / metrics_path)},
        {"path": report_path, "role": "report", "sha256": file_sha256(report_file)},
    ]
    if registration_sha256(final) != final.get("registration_sha256"):
        raise ValueError("final result attempted to mutate immutable registration fields")
    _write_json_exclusive(_repository_path(root, completion_path), final)
    manifest = _build_manifest(
        root,
        final,
        completed_at_utc,
        [
            ("registration", _string(entry.get("registration_path"), "registration_path"), None),
            ("completion", completion_path, None),
            ("trades", trades_path, len(trades)),
            ("metrics", metrics_path, len(metrics)),
            ("report", report_path, None),
        ],
    )
    _write_json_exclusive(_repository_path(root, manifest_path), manifest)

    entry.update(
        {
            "artifact_manifest_path": manifest_path,
            "artifact_manifest_sha256": file_sha256(root / manifest_path),
            "outcome": outcome,
            "report_path": report_path,
            "report_sha256": file_sha256(report_file),
            "status": status,
        }
    )
    _replace_json(registry_path, registry)
    return copy.deepcopy(entry)


def record_review(
    root: Path,
    experiment_id: str,
    *,
    review_result: str,
    reviewed_at_utc: str,
    reviewer: str,
    reason_codes: list[str],
) -> dict[str, object]:
    """追加独立评审；仅完成且 PASS 的实验可以被批准进入策略选择。"""

    if review_result not in {"APPROVED", "REJECTED"}:
        raise ValueError("review_result must be APPROVED or REJECTED")
    _parse_utc(reviewed_at_utc, "reviewed_at_utc")
    registry_path, registry, entries = _load_registry(root)
    entry = _registry_entry(entries, experiment_id)
    if entry.get("status") not in _FINAL_STATUSES:
        raise ValueError("only a finalized experiment can be reviewed")
    if review_result == "APPROVED" and not (
        entry.get("status") == "COMPLETED" and entry.get("outcome") == "PASS"
    ):
        raise ValueError("only COMPLETED/PASS experiments can be approved")
    if entry.get("review_result") != "PENDING":
        raise ValueError("review is append-only and has already been recorded")
    if not reviewer or not reason_codes:
        raise ValueError("reviewer and at least one reason code are required")

    review_path = _relative_experiment_path(experiment_id, "review.json")
    review = {
        "experiment_id": experiment_id,
        "reason_codes": reason_codes,
        "review_result": review_result,
        "reviewed_at_utc": reviewed_at_utc,
        "reviewer": reviewer,
    }
    _write_json_exclusive(_repository_path(root, review_path), review)

    completion_path = _relative_experiment_path(experiment_id, "completion.json")
    trades_path = _relative_experiment_path(experiment_id, "trades.json")
    metrics_path = _relative_experiment_path(experiment_id, "metrics.json")
    reviewed_report_path = _relative_experiment_path(experiment_id, "report-reviewed.md")
    reviewed_manifest_path = _relative_experiment_path(
        experiment_id, "artifact-manifest-reviewed.json"
    )
    completion = _read_json(_repository_path(root, completion_path))
    completion["review_result"] = review_result
    reviewed_report_file = _repository_path(root, reviewed_report_path)
    reviewed_report_file.write_text(
        render_experiment_report(completion), encoding="utf-8", newline="\n"
    )
    trades = _list(
        json.loads(_repository_path(root, trades_path).read_text(encoding="utf-8")), "trades"
    )
    metrics = _mapping(
        json.loads(_repository_path(root, metrics_path).read_text(encoding="utf-8")), "metrics"
    )
    reviewed_manifest = _build_manifest(
        root,
        completion,
        reviewed_at_utc,
        [
            ("registration", _string(entry.get("registration_path"), "registration_path"), None),
            ("completion", completion_path, None),
            ("trades", trades_path, len(trades)),
            ("metrics", metrics_path, len(metrics)),
            ("report", reviewed_report_path, None),
            ("review", review_path, None),
        ],
    )
    _write_json_exclusive(_repository_path(root, reviewed_manifest_path), reviewed_manifest)
    entry.update(
        {
            "artifact_manifest_path": reviewed_manifest_path,
            "artifact_manifest_sha256": file_sha256(root / reviewed_manifest_path),
            "report_path": reviewed_report_path,
            "report_sha256": file_sha256(reviewed_report_file),
            "review_path": review_path,
            "review_result": review_result,
            "review_sha256": file_sha256(root / review_path),
        }
    )
    _replace_json(registry_path, registry)
    return copy.deepcopy(entry)


def assert_selectable_experiment(root: Path, experiment_id: str) -> None:
    """策略选择只能消费已登记、hash 完整、完成、通过且已批准的实验。"""

    entry = locate_experiment(root, experiment_id)
    if not (
        entry.get("status") == "COMPLETED"
        and entry.get("outcome") == "PASS"
        and entry.get("review_result") == "APPROVED"
    ):
        raise ValueError("experiment is not eligible for strategy selection")


def compare_reproduction(
    expected_trades: list[object],
    actual_trades: list[object],
    expected_metrics: dict[str, object],
    actual_metrics: dict[str, object],
    *,
    metric_tolerance: Decimal,
) -> list[str]:
    """交易列表要求逐字段一致，指标使用显式绝对误差；返回全部差异。"""

    if metric_tolerance < 0 or not metric_tolerance.is_finite():
        raise ValueError("metric_tolerance must be finite and nonnegative")
    differences: list[str] = []
    if canonical_json_bytes(expected_trades) != canonical_json_bytes(actual_trades):
        differences.append("trade_list_mismatch")
    if expected_metrics.keys() != actual_metrics.keys():
        differences.append("metric_keys_mismatch")
        return differences
    for name in sorted(expected_metrics):
        expected = expected_metrics[name]
        actual = actual_metrics[name]
        if expected is None or actual is None:
            if expected is not actual:
                differences.append(f"metric_mismatch:{name}")
            continue
        try:
            expected_decimal = Decimal(_string(expected, f"expected_metrics.{name}"))
            actual_decimal = Decimal(_string(actual, f"actual_metrics.{name}"))
        except InvalidOperation as error:
            raise ValueError(f"metric {name} is not a decimal string") from error
        if not expected_decimal.is_finite() or not actual_decimal.is_finite():
            raise ValueError(f"metric {name} must be finite")
        if abs(expected_decimal - actual_decimal) > metric_tolerance:
            differences.append(f"metric_mismatch:{name}")
    return differences
