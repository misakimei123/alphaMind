"""构建并复核 P2-06 自动反作弊证据。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import importlib.util
import json
import math
import shlex
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from alphamind.research.anti_cheat import (  # noqa: E402
    DatasetAccessGuard,
    scan_strategy_source,
    validate_registry_selection,
    validate_signal_execution_separation,
    validate_timestamp_boundary,
)

OHLCV_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a table/object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} must be a non-empty string")
    return value


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{label} must be a non-empty string list")
    return list(value)


def _int_list(value: object, label: str) -> list[int]:
    if not isinstance(value, list) or not value or not all(type(item) is int for item in value):
        raise TypeError(f"{label} must be a non-empty integer list")
    return list(value)


def repo_path(project_root: Path, relative: str) -> Path:
    candidate = (project_root / relative).resolve()
    try:
        candidate.relative_to(project_root.resolve())
    except ValueError as error:
        raise ValueError(f"path escapes project root: {relative}") from error
    return candidate


def load_config(path: Path) -> dict[str, Any]:
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1 or config.get("model_version") != "p2-06-v1":
        raise RuntimeError("unsupported anti-cheat config version")
    if config.get("freqtrade_version") != "2026.6" or config.get("ccxt_version") != "4.5.61":
        raise RuntimeError("anti-cheat runtime lock drifted")
    if config.get("timeframe") != "4h" or config.get("timerange") != "20220101-20250701":
        raise RuntimeError("anti-cheat selection window drifted")
    official = _mapping(config.get("official_analysis"), "official_analysis")
    startups = _int_list(official.get("startup_candles"), "startup_candles")
    if startups != sorted(set(startups)) or 120 in startups:
        raise RuntimeError(
            "startup candles must be unique, sorted, and independent of baseline 120"
        )
    if official.get("allow_limit_orders") is not False:
        raise RuntimeError("lookahead analysis must retain the official market-order override")
    alternate = _mapping(config.get("alternate_exchange"), "alternate_exchange")
    if alternate.get("performance_selection_allowed") is not False:
        raise RuntimeError("cross-exchange evidence cannot create parameter selection rights")
    return config


def run_command(command: Sequence[str], cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    output = (
        f"$ {shlex.join(command)}\n"
        f"[stdout]\n{completed.stdout}"
        f"[stderr]\n{completed.stderr}"
        f"[exit_code]\n{completed.returncode}\n"
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {shlex.join(command)}\n{output}")
    return {"command": list(command), "exit_code": completed.returncode, "output": output}


def _read_frame(path: Path) -> Any:
    pandas = importlib.import_module("pandas")
    frame = pandas.read_feather(path)
    if "timestamp" in frame.columns:
        frame = frame.rename(columns={"timestamp": "date"})
    missing = sorted(set(OHLCV_COLUMNS) - set(frame.columns))
    if missing:
        raise RuntimeError(f"OHLCV partition {path} is missing columns {missing}")
    return frame.loc[:, OHLCV_COLUMNS].reset_index(drop=True)


def development_frame(frame: Any) -> Any:
    """在任何指标或 signal 计算前执行冻结的 development 左闭右开边界。"""

    pandas = importlib.import_module("pandas")
    dates = pandas.to_datetime(frame["date"], utc=True)
    start = datetime(2022, 1, 1, tzinfo=UTC)
    end_exclusive = datetime(2025, 7, 1, tzinfo=UTC)
    selected = frame.loc[(dates >= start) & (dates < end_exclusive)].reset_index(drop=True)
    if selected.empty:
        raise RuntimeError("development selection produced an empty frame")
    return selected


def stage_freqtrade_data(clean_root: Path, destination: Path, pairs: Sequence[str]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for pair in pairs:
        filename = f"{pair.replace('/', '_')}-4h.feather"
        frame = development_frame(_read_frame(clean_root / filename))
        frame.to_feather(destination / filename)


def _partition_record(path: Path, pair: str) -> dict[str, Any]:
    pandas = importlib.import_module("pandas")
    frame = _read_frame(path)
    dates = pandas.to_datetime(frame["date"], utc=True)
    if dates.empty or not dates.is_monotonic_increasing or dates.duplicated().any():
        raise RuntimeError(f"alternate exchange partition has invalid timestamps: {path}")
    end_exclusive = datetime(2025, 7, 1, tzinfo=UTC)
    validate_timestamp_boundary([value.to_pydatetime() for value in dates], end_exclusive)
    return {
        "pair": pair,
        "timeframe": "4h",
        "relative_path": path.name,
        "byte_size": path.stat().st_size,
        "file_sha256": file_sha256(path),
        "candle_count": len(frame),
        "first_candle_utc": dates.iloc[0].isoformat().replace("+00:00", "Z"),
        "last_candle_utc": dates.iloc[-1].isoformat().replace("+00:00", "Z"),
    }


def trim_alternate_development_window(path: Path) -> None:
    """Freqtrade timerange 下载可能包含结束 candle；发布前强制执行右开区间。"""

    pandas = importlib.import_module("pandas")
    frame = pandas.read_feather(path)
    if "date" not in frame.columns:
        raise RuntimeError(f"alternate partition is missing date: {path}")
    dates = pandas.to_datetime(frame["date"], utc=True)
    start = datetime(2022, 1, 1, tzinfo=UTC)
    end_exclusive = datetime(2025, 7, 1, tzinfo=UTC)
    retained = frame.loc[(dates >= start) & (dates < end_exclusive)].reset_index(drop=True)
    if retained.empty:
        raise RuntimeError(f"alternate partition became empty after boundary trim: {path}")
    retained.to_feather(path)


def verify_alternate_manifest(project_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    source_root = repo_path(project_root, _string(manifest.get("source_root"), "source_root"))
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or len(partitions) != 2:
        raise RuntimeError("alternate manifest must contain exactly two partitions")
    verified = []
    for partition in partitions:
        item = _mapping(partition, "partition")
        path = source_root / _string(item.get("relative_path"), "partition.relative_path")
        actual = _partition_record(path, _string(item.get("pair"), "partition.pair"))
        for key in ("byte_size", "file_sha256", "candle_count"):
            if actual[key] != item.get(key):
                raise RuntimeError(f"alternate partition {path.name} mismatched {key}")
        verified.append(actual)
    return {"source_root": source_root, "partitions": verified}


def build_alternate_snapshot(
    project_root: Path, config: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    alternate = _mapping(config.get("alternate_exchange"), "alternate_exchange")
    manifest_path = repo_path(
        project_root, _string(alternate.get("manifest_path"), "manifest_path")
    )
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        verified = verify_alternate_manifest(project_root, _mapping(manifest, "alternate manifest"))
        evidence = {
            "command": manifest.get("download_command"),
            "exit_code": 0,
            "output": (
                "Existing immutable alternate snapshot verified; download was not repeated.\n"
            ),
        }
        return _mapping(manifest, "alternate manifest"), {**verified, "evidence": evidence}

    source_parent = repo_path(
        project_root, _string(alternate.get("source_parent"), "source_parent")
    )
    source_parent.mkdir(parents=True, exist_ok=True)
    staging = source_parent / ".staging-p2-06-v1"
    # 网络或宿主终端中断后保留已下载分区；Freqtrade --prepend 会按时间范围安全续传并去重。
    staging.mkdir(exist_ok=True)
    pairs = _string_list(alternate.get("pairs"), "alternate pairs")
    command = [
        "freqtrade",
        "download-data",
        "--exchange",
        _string(alternate.get("exchange"), "alternate exchange"),
        "--trading-mode",
        "spot",
        "--pairs",
        *pairs,
        "--timeframes",
        "4h",
        "--timerange",
        _string(alternate.get("timerange"), "alternate timerange"),
        "--data-format-ohlcv",
        "feather",
        "--datadir",
        str(staging),
        "--prepend",
        "--no-color",
    ]
    evidence = run_command(command, project_root)
    for pair in pairs:
        trim_alternate_development_window(staging / f"{pair.replace('/', '_')}-4h.feather")
    partitions = [
        _partition_record(staging / f"{pair.replace('/', '_')}-4h.feather", pair) for pair in pairs
    ]
    lines = [f"{item['relative_path']}:{item['file_sha256']}\n" for item in partitions]
    snapshot_hash = hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()
    snapshot_id = f"okx-spot-cross-check-{snapshot_hash[:12]}"
    final_root = source_parent / snapshot_id
    if final_root.exists():
        raise FileExistsError(f"alternate snapshot already exists: {final_root}")
    staging.rename(final_root)
    created_manifest: dict[str, Any] = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at_utc": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "exchange": "okx",
        "market_type": "spot",
        "role": "p2_06_cross_exchange_signal_robustness_only",
        "performance_selection_allowed": False,
        "timerange": alternate["timerange"],
        "source_root": final_root.relative_to(project_root).as_posix(),
        "snapshot_sha256": snapshot_hash,
        "download_command": command,
        "partitions": partitions,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("xb") as destination:
        destination.write(json_bytes(created_manifest))
    verified = verify_alternate_manifest(project_root, created_manifest)
    return created_manifest, {**verified, "evidence": evidence}


def load_strategy_type(path: Path, class_name: str) -> Any:
    module_name = f"alphamind_p2_06_{class_name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load strategy from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


def verify_analysis_subclass(analysis_type: Any, production_type: Any) -> dict[str, Any]:
    direct_bases = analysis_type.__bases__
    production_source = production_type.populate_indicators.__code__.co_filename
    base_source = (
        direct_bases[0].populate_indicators.__code__.co_filename if len(direct_bases) == 1 else None
    )
    if (
        len(direct_bases) != 1
        or direct_bases[0].__name__ != production_type.__name__
        or base_source is None
        or Path(base_source).resolve() != Path(production_source).resolve()
    ):
        raise RuntimeError("analysis strategy must directly subclass the production strategy")
    forbidden_overrides = {
        "populate_indicators",
        "populate_entry_trend",
        "populate_exit_trend",
        "custom_stoploss",
        "custom_stake_amount",
    }
    actual_overrides = forbidden_overrides.intersection(analysis_type.__dict__)
    if actual_overrides:
        raise RuntimeError(f"analysis strategy overrides signal/risk methods: {actual_overrides}")
    production = production_type({})
    analysis = analysis_type({})
    if production.confirm_trade_entry(
        "BTC/USDT", "market", 1.0, 1.0, "GTC", datetime.now(UTC), "entry_breakout", "long"
    ):
        raise RuntimeError("production strategy unexpectedly enables execution")
    if not analysis.confirm_trade_entry(
        "BTC/USDT", "market", 1.0, 1.0, "GTC", datetime.now(UTC), "entry_breakout", "long"
    ):
        raise RuntimeError("analysis strategy cannot form trades for official analysis")
    return {
        "direct_subclass": True,
        "signal_method_override_count": 0,
        "production_execution_enabled": False,
        "analysis_execution_enabled": True,
    }


def _run_adapter(strategy: Any, frame: Any, pair: str) -> Any:
    metadata = {"pair": pair}
    analyzed = strategy.populate_indicators(frame.copy(), metadata)
    analyzed = strategy.populate_entry_trend(analyzed, metadata)
    return strategy.populate_exit_trend(analyzed, metadata)


def _equal_value(left: Any, right: Any) -> bool:
    try:
        if bool(importlib.import_module("pandas").isna(left)) and bool(
            importlib.import_module("pandas").isna(right)
        ):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(left, float) or isinstance(right, float):
        try:
            return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-12)
        except (TypeError, ValueError):
            return False
    return bool(left == right)


def prefix_invariance_scan(
    strategy_type: Any,
    frame: Any,
    pair: str,
    columns: Sequence[str],
    regular_checkpoint_count: int,
) -> dict[str, Any]:
    """逐列比较全 dataframe 与同一时点 prefix 的结果，动态探测未来依赖。"""

    if regular_checkpoint_count < 8:
        raise ValueError("regular checkpoint count is too small")
    strategy = strategy_type({})
    full = _run_adapter(strategy, frame, pair)
    missing = sorted(set(columns) - set(full.columns))
    if missing:
        raise RuntimeError(f"strategy output is missing anti-cheat columns: {missing}")
    startup = int(strategy.startup_candle_count)
    step = max((len(frame) - startup) // regular_checkpoint_count, 1)
    checkpoints = set(range(startup, len(frame), step))
    checkpoints.add(len(frame) - 1)
    for signal_column in ("enter_long", "exit_long"):
        checkpoints.update(int(index) for index in full.index[full[signal_column].eq(1)])
    checkpoints = {index for index in checkpoints if startup <= index < len(frame)}
    mismatch_examples: list[dict[str, Any]] = []
    counts = {column: 0 for column in columns}
    for index in sorted(checkpoints):
        prefix = _run_adapter(strategy, frame.iloc[: index + 1].copy(), pair)
        for column in columns:
            counts[column] += 1
            left = full.at[index, column]
            right = prefix.iloc[-1][column]
            if not _equal_value(left, right):
                mismatch_examples.append(
                    {"index": index, "column": column, "full": str(left), "prefix": str(right)}
                )
    if mismatch_examples:
        raise RuntimeError(f"prefix invariance failed: {mismatch_examples[:5]}")
    return {
        "pair": pair,
        "candle_count": len(frame),
        "checkpoint_count": len(checkpoints),
        "column_check_counts": counts,
        "mismatch_count": 0,
        "entry_signal_count": int(full["enter_long"].eq(1).sum()),
        "exit_signal_count": int(full["exit_long"].eq(1).sum()),
    }


def parse_lookahead_csv(path: Path, minimum_signals: int) -> dict[str, Any]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
    if len(rows) != 1:
        raise RuntimeError(f"expected one lookahead result row, got {len(rows)}")
    row = rows[0]
    normalized = {key.strip(): (value or "").strip() for key, value in row.items()}
    has_bias = normalized.get("has_bias", "").lower()
    if has_bias not in {"false", "no"}:
        raise RuntimeError(f"Freqtrade lookahead analysis reported bias: {normalized}")
    total_signals = int(normalized.get("total_signals", "0"))
    if total_signals < minimum_signals:
        raise RuntimeError(
            f"lookahead analysis checked only {total_signals} signals, minimum is {minimum_signals}"
        )
    biased_entry_signals = int(normalized.get("biased_entry_signals", "0"))
    biased_exit_signals = int(normalized.get("biased_exit_signals", "0"))
    biased_indicators = normalized.get("biased_indicators", "")
    if biased_entry_signals != 0 or biased_exit_signals != 0 or biased_indicators:
        raise RuntimeError(f"lookahead summary contradicts bias detail columns: {normalized}")
    return {
        "has_bias": False,
        "total_signals": total_signals,
        "biased_entry_signals": biased_entry_signals,
        "biased_exit_signals": biased_exit_signals,
        "biased_indicators": biased_indicators,
        "raw_row": normalized,
    }


def parse_recursive_output(output: str) -> dict[str, Any]:
    no_variance = "No variance on indicator(s) found due to recursive formula." in output
    no_lookahead = "No lookahead bias on indicators found." in output
    if not no_variance or not no_lookahead:
        raise RuntimeError("recursive analysis did not prove zero variance and zero indicator bias")
    return {
        "maximum_variance_percent": "0.0",
        "indicator_lookahead_bias": False,
        "zero_variance": True,
    }


def official_analysis(
    project_root: Path,
    config: Mapping[str, Any],
    staged_data: Path,
    temporary_root: Path,
) -> dict[str, Any]:
    inputs = _mapping(config.get("inputs"), "inputs")
    official = _mapping(config.get("official_analysis"), "official_analysis")
    pairs = _string_list(config.get("pairs"), "pairs")
    common = [
        "--config",
        str(repo_path(project_root, _string(inputs.get("freqtrade_config"), "freqtrade_config"))),
        "--userdir",
        str(project_root / "user_data"),
        "--strategy",
        _string(config.get("analysis_strategy"), "analysis_strategy"),
        "--strategy-path",
        str(project_root / "research/strategies"),
        "--datadir",
        str(staged_data),
        "--timerange",
        _string(config.get("timerange"), "timerange"),
        "--pairs",
        *pairs,
        "--no-color",
    ]
    version = run_command(["freqtrade", "--version"], project_root)
    csv_path = temporary_root / "lookahead.csv"
    lookahead_command = [
        "freqtrade",
        "lookahead-analysis",
        *common,
        "--minimum-trade-amount",
        str(official["minimum_trade_amount"]),
        "--targeted-trade-amount",
        str(official["targeted_trade_amount"]),
        "--lookahead-analysis-exportfilename",
        str(csv_path),
        "--export",
        "none",
        "--backtest-directory",
        str(temporary_root / "backtests"),
    ]
    lookahead = run_command(lookahead_command, project_root)
    lookahead_result = parse_lookahead_csv(csv_path, int(official["minimum_trade_amount"]))
    recursive_command = [
        "freqtrade",
        "recursive-analysis",
        *common[: common.index("--pairs")],
        "--pairs",
        _string(official.get("recursive_pair"), "recursive_pair"),
        "--startup-candle",
        *[str(value) for value in _int_list(official["startup_candles"], "startup_candles")],
        "--no-color",
    ]
    recursive = run_command(recursive_command, project_root)
    recursive_result = parse_recursive_output(recursive["output"])
    return {
        "version": version,
        "lookahead": {**lookahead, "result": lookahead_result, "csv": csv_path.read_text()},
        "recursive": {**recursive, "result": recursive_result},
    }


def static_and_registry_checks(project_root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    inputs = _mapping(config.get("inputs"), "inputs")
    production_path = repo_path(
        project_root, _string(inputs.get("production_strategy"), "production_strategy")
    )
    analysis_path = repo_path(
        project_root, _string(inputs.get("analysis_strategy"), "analysis_strategy")
    )
    findings = [
        {
            "path": path.relative_to(project_root).as_posix(),
            "findings": scan_strategy_source(path.read_text()),
        }
        for path in (production_path, analysis_path)
    ]
    serialized_findings = [
        {
            "path": item["path"],
            "findings": [finding.__dict__ for finding in item["findings"]],
        }
        for item in findings
    ]
    if any(item["findings"] for item in serialized_findings):
        raise RuntimeError(f"strategy static anti-cheat scan failed: {serialized_findings}")
    production_type = load_strategy_type(
        production_path, _string(config.get("strategy"), "strategy")
    )
    analysis_type = load_strategy_type(
        analysis_path, _string(config.get("analysis_strategy"), "analysis_strategy")
    )
    subclass = verify_analysis_subclass(analysis_type, production_type)
    registry_path = repo_path(project_root, _string(inputs.get("trial_registry"), "trial_registry"))
    summary_path = repo_path(
        project_root, _string(inputs.get("walk_forward_summary"), "walk_forward_summary")
    )
    registry = _mapping(json.loads(registry_path.read_text()), "trial registry")
    summary = _mapping(json.loads(summary_path.read_text()), "walk-forward summary")
    registry_result = validate_registry_selection(registry, summary)
    trades: list[dict[str, Any]] = []
    trials = summary.get("trials")
    assert isinstance(trials, list)
    for trial in trials:
        trial_object = _mapping(trial, "summary trial")
        experiment_id = _string(trial_object.get("experiment_id"), "experiment_id")
        trial_trades = json.loads(
            (project_root / "research/experiments" / experiment_id / "trades.json").read_text()
        )
        if not isinstance(trial_trades, list):
            raise TypeError("trades artifact must be an array")
        trades.extend(_mapping(item, "trade") for item in trial_trades)
    timing = validate_signal_execution_separation(trades, timedelta(hours=4))
    return {
        "static_source_scan": serialized_findings,
        "analysis_subclass": subclass,
        "registry": registry_result,
        "signal_execution_separation": timing,
        "production_type": production_type,
    }


def dataset_scans(
    project_root: Path,
    config: Mapping[str, Any],
    alternate_manifest: Mapping[str, Any],
    strategy_type: Any,
) -> dict[str, Any]:
    inputs = _mapping(config.get("inputs"), "inputs")
    prefix = _mapping(config.get("prefix_invariance"), "prefix_invariance")
    alternate = _mapping(config.get("alternate_exchange"), "alternate_exchange")
    clean_root = repo_path(project_root, _string(inputs.get("clean_root"), "clean_root"))
    alternate_root = repo_path(
        project_root, _string(alternate_manifest.get("source_root"), "alternate source_root")
    )
    guard = DatasetAccessGuard(project_root, [clean_root, alternate_root])
    columns = _string_list(prefix.get("columns"), "prefix columns")
    checkpoint_count = int(prefix["regular_checkpoint_count"])
    pairs = _string_list(config.get("pairs"), "pairs")
    result: dict[str, Any] = {"bybit": [], "okx": [], "common_candles": {}}
    end_exclusive = datetime.fromisoformat(
        _string(config.get("selection_end_exclusive"), "selection_end_exclusive").replace(
            "Z", "+00:00"
        )
    )
    pandas = importlib.import_module("pandas")
    for pair in pairs:
        filename = f"{pair.replace('/', '_')}-4h.feather"
        bybit_path = guard.approve(clean_root / filename)
        okx_path = guard.approve(alternate_root / filename)
        bybit_frame = development_frame(_read_frame(bybit_path))
        okx_frame = development_frame(_read_frame(okx_path))
        for frame in (bybit_frame, okx_frame):
            dates = pandas.to_datetime(frame["date"], utc=True)
            validate_timestamp_boundary([value.to_pydatetime() for value in dates], end_exclusive)
        result["bybit"].append(
            prefix_invariance_scan(strategy_type, bybit_frame, pair, columns, checkpoint_count)
        )
        result["okx"].append(
            prefix_invariance_scan(strategy_type, okx_frame, pair, columns, checkpoint_count)
        )
        common = len(
            set(pandas.to_datetime(bybit_frame["date"], utc=True)).intersection(
                set(pandas.to_datetime(okx_frame["date"], utc=True))
            )
        )
        if common < int(alternate["minimum_common_candles_per_pair"]):
            raise RuntimeError(f"cross-exchange overlap for {pair} is only {common} candles")
        result["common_candles"][pair] = common
    result["accessed_roots"] = [
        clean_root.relative_to(project_root).as_posix(),
        alternate_root.relative_to(project_root).as_posix(),
    ]
    result["final_holdout_access_count"] = 0
    return result


def report_markdown(report: Mapping[str, Any]) -> str:
    official = _mapping(report.get("official_analysis"), "official_analysis")
    dataset = _mapping(report.get("dataset_scans"), "dataset_scans")
    timing = _mapping(report.get("signal_execution_separation"), "signal timing")
    return "\n".join(
        [
            "# P2-06 自动反作弊报告",
            "",
            "- Assessment: **PASS**",
            f"- Freqtrade lookahead bias: `{official['lookahead']['has_bias']}`",
            f"- Lookahead checked signals: `{official['lookahead']['total_signals']}`",
            f"- Recursive maximum variance: `{official['recursive']['maximum_variance_percent']}%`",
            "- Prefix-invariance mismatches: `0` across Bybit and OKX",
            f"- Signal/next-candle trades checked: `{timing['checked_trade_count']}`",
            f"- Common cross-exchange candles: `{dataset['common_candles']}`",
            "- Final Holdout access count: `0`",
            "- Parameter selection: still blocked by independent review; "
            "this report does not select parameters",
            "",
            "官方命令的完整 command、stdout、stderr、exit code 和 lookahead CSV 保存在 `raw/`。",
            "OKX 只用于同标的数据路径/未来依赖复测，不以收益或信号数量筛选参数。",
            "",
        ]
    )


def write_report(
    project_root: Path,
    config_path: Path,
    config: Mapping[str, Any],
    alternate_manifest: Mapping[str, Any],
    alternate_evidence: Mapping[str, Any],
    official: Mapping[str, Any],
    checks: Mapping[str, Any],
    dataset: Mapping[str, Any],
) -> Path:
    output = _mapping(config.get("output"), "output")
    final_root = repo_path(project_root, _string(output.get("report_root"), "report_root"))
    staging = final_root.parent / ".staging-p2-06-v1"
    if final_root.exists() or staging.exists():
        raise FileExistsError("P2-06 report is append-only and already exists or has staging")
    raw = staging / "raw"
    raw.mkdir(parents=True)
    raw_files = {
        "freqtrade-version.txt": official["version"]["output"],
        "lookahead-analysis.txt": official["lookahead"]["output"],
        "lookahead-analysis.csv": official["lookahead"]["csv"],
        "recursive-analysis.txt": official["recursive"]["output"],
        "alternate-download.txt": alternate_evidence["output"],
    }
    for name, content in raw_files.items():
        (raw / name).write_text(str(content), encoding="utf-8", newline="\n")
    report: dict[str, Any] = {
        "schema_version": 1,
        "model_version": "p2-06-v1",
        "assessment": "PASS",
        "official_analysis": {
            "lookahead": official["lookahead"]["result"],
            "recursive": official["recursive"]["result"],
        },
        "static_source_scan": checks["static_source_scan"],
        "analysis_subclass": checks["analysis_subclass"],
        "registry": checks["registry"],
        "signal_execution_separation": checks["signal_execution_separation"],
        "dataset_scans": dataset,
        "alternate_snapshot": {
            "snapshot_id": alternate_manifest["snapshot_id"],
            "manifest_path": _mapping(config["alternate_exchange"], "alternate_exchange")[
                "manifest_path"
            ],
            "performance_selection_allowed": False,
        },
        "selection": {
            "parameter_selection": None,
            "blocker": "independent_review_pending",
        },
        "evidence_boundary": {
            "official_commands_require_public_exchange_market_metadata_network": True,
            "verification_replays_committed_hashes_and_local_scans_without_network": True,
            "live_execution_tested": False,
        },
    }
    (staging / "report.json").write_bytes(json_bytes(report))
    (staging / "report.md").write_text(report_markdown(report), encoding="utf-8", newline="\n")
    inputs = _mapping(config.get("inputs"), "inputs")
    source_files = [
        config_path,
        project_root / "scripts/build_anti_cheat_report.py",
        project_root / "src/alphamind/research/anti_cheat.py",
        repo_path(project_root, _string(inputs["production_strategy"], "production_strategy")),
        repo_path(project_root, _string(inputs["analysis_strategy"], "analysis_strategy")),
        repo_path(project_root, _string(inputs["freqtrade_config"], "freqtrade_config")),
        repo_path(project_root, _string(inputs["walk_forward_summary"], "walk_forward_summary")),
        repo_path(project_root, _string(inputs["trial_registry"], "trial_registry")),
        repo_path(
            project_root,
            _string(config["alternate_exchange"]["manifest_path"], "alternate manifest_path"),
        ),
    ]
    evidence_files = [staging / "report.json", staging / "report.md", *sorted(raw.iterdir())]
    files = []
    for path in [*source_files, *evidence_files]:
        if path.is_relative_to(staging):
            relative = (final_root / path.relative_to(staging)).relative_to(project_root)
        else:
            relative = path.relative_to(project_root)
        files.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    manifest = {"schema_version": 1, "model_version": "p2-06-v1", "files": files}
    (staging / "manifest.json").write_bytes(json_bytes(manifest))
    staging.rename(final_root)
    return final_root


def verify_report(
    project_root: Path, config_path: Path, config: Mapping[str, Any]
) -> dict[str, Any]:
    output = _mapping(config.get("output"), "output")
    report_root = repo_path(project_root, _string(output.get("report_root"), "report_root"))
    manifest = _mapping(json.loads((report_root / "manifest.json").read_text()), "manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("anti-cheat manifest has no files")
    for item in files:
        record = _mapping(item, "manifest file")
        path = repo_path(project_root, _string(record.get("path"), "manifest path"))
        if file_sha256(path) != record.get("sha256"):
            raise RuntimeError(f"anti-cheat artifact hash mismatch: {path}")
    alternate = _mapping(config.get("alternate_exchange"), "alternate_exchange")
    alternate_manifest = _mapping(
        json.loads(repo_path(project_root, alternate["manifest_path"]).read_text()),
        "alternate manifest",
    )
    verify_alternate_manifest(project_root, alternate_manifest)
    checks = static_and_registry_checks(project_root, config)
    dataset = dataset_scans(project_root, config, alternate_manifest, checks["production_type"])
    report = _mapping(json.loads((report_root / "report.json").read_text()), "report")
    lookahead = _mapping(
        _mapping(report["official_analysis"], "official")["lookahead"], "lookahead"
    )
    if lookahead.get("has_bias") is not False:
        raise RuntimeError("committed lookahead result is not clean")
    parse_recursive_output((report_root / "raw/recursive-analysis.txt").read_text())
    parse_lookahead_csv(
        report_root / "raw/lookahead-analysis.csv",
        int(_mapping(config["official_analysis"], "official")["minimum_trade_amount"]),
    )
    return {
        "status": "verified",
        "assessment": report.get("assessment"),
        "manifest_file_count": len(files),
        "prefix_dataset_count": len(dataset["bybit"]) + len(dataset["okx"]),
        "config_sha256": file_sha256(config_path),
    }


def build(project_root: Path, config_path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    alternate_manifest, alternate = build_alternate_snapshot(project_root, config)
    checks = static_and_registry_checks(project_root, config)
    inputs = _mapping(config.get("inputs"), "inputs")
    clean_root = repo_path(project_root, _string(inputs.get("clean_root"), "clean_root"))
    pairs = _string_list(config.get("pairs"), "pairs")
    with tempfile.TemporaryDirectory(prefix="alphamind-p2-06-") as temporary:
        temporary_root = Path(temporary)
        staged_data = temporary_root / "bybit-data"
        stage_freqtrade_data(clean_root, staged_data, pairs)
        official = official_analysis(project_root, config, staged_data, temporary_root)
    dataset = dataset_scans(project_root, config, alternate_manifest, checks["production_type"])
    report_root = write_report(
        project_root,
        config_path,
        config,
        alternate_manifest,
        alternate["evidence"],
        official,
        checks,
        dataset,
    )
    return {
        "status": "built",
        "assessment": "PASS",
        "report_root": report_root.relative_to(project_root).as_posix(),
        "lookahead_signals": official["lookahead"]["result"]["total_signals"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path, default=Path("configs/research/anti-cheat-v1.toml"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    config_path = args.config if args.config.is_absolute() else project_root / args.config
    config_path = config_path.resolve()
    config = load_config(config_path)
    result = (
        build(project_root, config_path, config)
        if args.build
        else verify_report(project_root, config_path, config)
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
