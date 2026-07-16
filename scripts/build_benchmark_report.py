"""从 P1-04 clean snapshot 生成并复核 P1-05 确定性基准报告。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import tomllib
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from alphamind.research.benchmarks import (
    BenchmarkCurve,
    PriceBar,
    TransactionCostModel,
    build_buy_and_hold_benchmark,
    build_cash_benchmark,
    build_equal_weight_buy_and_hold_benchmark,
    build_simple_moving_average_benchmark,
)
from alphamind.research.performance import calculate_performance
from scripts.build_clean_dataset import canonical_markdown_sha256, verify_clean_report
from scripts.create_source_snapshot import file_sha256, json_bytes, publish_snapshot, write_new_file

REPORT_ROOT = Path("research/reports/benchmarks")
PERIODS_PER_YEAR = {"4h": 2190, "1d": 365}
METRIC_PRECISION = Decimal("0.000000000001")


def _required_mapping(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise TypeError(f"{key} must be an object")
    return value


def _required_str(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be a non-empty string")
    return value


def _required_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value


def _required_decimal(document: dict[str, Any], key: str) -> Decimal:
    raw = _required_str(document, key)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{key} must be an exact decimal string") from exc
    if not value.is_finite():
        raise ValueError(f"{key} must be finite")
    return value


def _load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("rb") as source:
        config = tomllib.load(source)
    if set(config) != {
        "schema_version",
        "benchmark_version",
        "dataset_id",
        "initial_equity",
        "transaction_costs",
        "simple_moving_average",
    }:
        raise ValueError("benchmark config has unexpected or missing top-level keys")
    if _required_int(config, "schema_version") != 1:
        raise ValueError("unsupported benchmark config schema_version")
    _required_str(config, "benchmark_version")
    _required_str(config, "dataset_id")
    if _required_decimal(config, "initial_equity") <= 0:
        raise ValueError("initial_equity must be positive")

    costs = _required_mapping(config, "transaction_costs")
    if set(costs) != {
        "fee_rate_per_side",
        "fee_source",
        "fee_source_checked_at_utc",
        "half_spread_rate",
        "slippage_rate_per_side",
    }:
        raise ValueError("transaction_costs has unexpected or missing keys")
    TransactionCostModel(
        _required_decimal(costs, "fee_rate_per_side"),
        _required_decimal(costs, "half_spread_rate"),
        _required_decimal(costs, "slippage_rate_per_side"),
    )
    _required_str(costs, "fee_source")
    checked_at = datetime.fromisoformat(
        _required_str(costs, "fee_source_checked_at_utc").replace("Z", "+00:00")
    )
    if checked_at.utcoffset() != UTC.utcoffset(checked_at):
        raise ValueError("fee_source_checked_at_utc must be UTC")

    moving_average = _required_mapping(config, "simple_moving_average")
    if set(moving_average) != {"window_periods"}:
        raise ValueError("simple_moving_average has unexpected or missing keys")
    if _required_int(moving_average, "window_periods") <= 1:
        raise ValueError("simple moving-average window must be greater than one")
    return config


def _canonical_report_sha256(report: dict[str, object]) -> str:
    canonical = copy.deepcopy(report)
    canonical.pop("report_content_sha256", None)
    canonical.pop("report_markdown_sha256", None)
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _metric_text(value: float | None) -> str | None:
    if value is None:
        return None
    rounded = Decimal(str(value)).quantize(METRIC_PRECISION)
    if rounded == 0:
        rounded = Decimal("0")
    return format(rounded, "f")


def _money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")


def _load_clean_bars(
    project_root: Path, quality_report: dict[str, Any]
) -> dict[tuple[str, str], tuple[PriceBar, ...]]:
    clean_root = project_root / _required_str(quality_report, "clean_root")
    partitions = quality_report.get("clean_partitions")
    if not isinstance(partitions, list):
        raise TypeError("quality report clean_partitions must be a list")
    pd = importlib.import_module("pandas")
    loaded: dict[tuple[str, str], tuple[PriceBar, ...]] = {}
    for partition in partitions:
        if not isinstance(partition, dict):
            raise TypeError("quality report partition must be an object")
        pair = _required_str(partition, "pair")
        timeframe = _required_str(partition, "timeframe")
        relative_path = _required_str(partition, "relative_path")
        frame = pd.read_feather(clean_root / relative_path, columns=["timestamp", "open", "close"])
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        bars = tuple(
            PriceBar(
                timestamp.to_pydatetime(),
                Decimal(str(open_price)),
                Decimal(str(close_price)),
            )
            for timestamp, open_price, close_price in zip(
                timestamps, frame["open"], frame["close"], strict=True
            )
        )
        loaded[(pair, timeframe)] = bars
    expected = {
        (pair, timeframe) for pair in ("BTC/USDT", "ETH/USDT") for timeframe in PERIODS_PER_YEAR
    }
    if set(loaded) != expected:
        raise RuntimeError("clean snapshot must contain exactly BTC/ETH 4h/1d partitions")
    return loaded


def _curve_record(
    name: str,
    scope: str,
    timeframe: str,
    curve: BenchmarkCurve,
) -> dict[str, object]:
    metrics = calculate_performance(
        curve.initial_equity,
        curve.observations,
        curve.trade_pnls,
        periods_per_year=PERIODS_PER_YEAR[timeframe],
    )
    metric_values = {
        key: value if isinstance(value, int) else _metric_text(value)
        for key, value in asdict(metrics).items()
    }
    return {
        "name": name,
        "scope": scope,
        "timeframe": timeframe,
        "periods_per_year": PERIODS_PER_YEAR[timeframe],
        "period_count": len(curve.observations),
        "trade_count": len(curve.trade_pnls),
        "first_candle_utc": curve.observations[0].timestamp.isoformat().replace("+00:00", "Z"),
        "last_candle_utc": curve.observations[-1].timestamp.isoformat().replace("+00:00", "Z"),
        "initial_equity": _money_text(curve.initial_equity),
        "final_equity": _money_text(curve.observations[-1].equity),
        "metrics": metric_values,
    }


def _assemble_report(
    project_root: Path, config_path: Path, quality_report_path: Path
) -> dict[str, object]:
    config = _load_config(config_path)
    verification = verify_clean_report(project_root, quality_report_path)
    if verification["status"] != "verified" or not verification["downstream_experiment_allowed"]:
        raise RuntimeError("quality report does not allow downstream benchmark use")
    quality_report = json.loads(quality_report_path.read_text(encoding="utf-8"))
    if not isinstance(quality_report, dict):
        raise TypeError("quality report must be a JSON object")
    dataset_id = _required_str(config, "dataset_id")
    if dataset_id != _required_str(quality_report, "dataset_id"):
        raise RuntimeError("benchmark config dataset_id does not match quality report")

    initial_equity = _required_decimal(config, "initial_equity")
    raw_costs = _required_mapping(config, "transaction_costs")
    costs = TransactionCostModel(
        _required_decimal(raw_costs, "fee_rate_per_side"),
        _required_decimal(raw_costs, "half_spread_rate"),
        _required_decimal(raw_costs, "slippage_rate_per_side"),
    )
    window = _required_int(_required_mapping(config, "simple_moving_average"), "window_periods")
    bars = _load_clean_bars(project_root, quality_report)

    records: list[dict[str, object]] = []
    for timeframe in ("4h", "1d"):
        btc = bars[("BTC/USDT", timeframe)]
        eth = bars[("ETH/USDT", timeframe)]
        records.extend(
            [
                _curve_record(
                    "cash",
                    "USDT",
                    timeframe,
                    build_cash_benchmark(btc, timeframe=timeframe, initial_equity=initial_equity),
                ),
                _curve_record(
                    "buy_and_hold",
                    "BTC/USDT",
                    timeframe,
                    build_buy_and_hold_benchmark(
                        btc,
                        timeframe=timeframe,
                        initial_equity=initial_equity,
                        costs=costs,
                    ),
                ),
                _curve_record(
                    "buy_and_hold",
                    "ETH/USDT",
                    timeframe,
                    build_buy_and_hold_benchmark(
                        eth,
                        timeframe=timeframe,
                        initial_equity=initial_equity,
                        costs=costs,
                    ),
                ),
                _curve_record(
                    "equal_weight_buy_and_hold",
                    "BTC/USDT+ETH/USDT",
                    timeframe,
                    build_equal_weight_buy_and_hold_benchmark(
                        btc,
                        eth,
                        timeframe=timeframe,
                        initial_equity=initial_equity,
                        costs=costs,
                    ),
                ),
                _curve_record(
                    f"sma_{window}_long_flat",
                    "BTC/USDT",
                    timeframe,
                    build_simple_moving_average_benchmark(
                        btc,
                        timeframe=timeframe,
                        initial_equity=initial_equity,
                        window=window,
                        costs=costs,
                    ),
                ),
                _curve_record(
                    f"sma_{window}_long_flat",
                    "ETH/USDT",
                    timeframe,
                    build_simple_moving_average_benchmark(
                        eth,
                        timeframe=timeframe,
                        initial_equity=initial_equity,
                        window=window,
                        costs=costs,
                    ),
                ),
            ]
        )

    benchmark_version = _required_str(config, "benchmark_version")
    report_id = f"{dataset_id}-{benchmark_version}"
    report_root = REPORT_ROOT / report_id
    report: dict[str, object] = {
        "schema_version": 1,
        "report_id": report_id,
        "benchmark_version": benchmark_version,
        "dataset_id": dataset_id,
        "quality_report_path": quality_report_path.relative_to(project_root).as_posix(),
        "quality_report_content_sha256": _required_str(quality_report, "report_content_sha256"),
        "clean_snapshot_sha256": _required_str(quality_report, "clean_snapshot_sha256"),
        "development_start": _required_str(quality_report, "development_start"),
        "development_end_exclusive": _required_str(quality_report, "development_end_exclusive"),
        "config_path": config_path.relative_to(project_root).as_posix(),
        "config_sha256": file_sha256(config_path),
        "initial_equity": _required_str(config, "initial_equity"),
        "cost_model": {
            "fee_rate_per_side": _required_str(raw_costs, "fee_rate_per_side"),
            "half_spread_rate": _required_str(raw_costs, "half_spread_rate"),
            "slippage_rate_per_side": _required_str(raw_costs, "slippage_rate_per_side"),
            "fee_source": _required_str(raw_costs, "fee_source"),
            "fee_source_checked_at_utc": _required_str(raw_costs, "fee_source_checked_at_utc"),
            "assumption_boundary": (
                "fee 使用公开 Non-VIP spot rate；点差和滑点为固定工程假设，不代表真实历史成交"
            ),
        },
        "moving_average_contract": {
            "window_periods": window,
            "signal": "completed_close_strictly_above_sma",
            "execution": "next_candle_open",
            "final_liquidation": "last_candle_close",
            "selection_role": "engineering_benchmark_only",
        },
        "metric_contract": {
            "returns": "simple_period_returns_from_initial_equity",
            "annualization": "365_days_with_2190_4h_or_365_1d_periods",
            "sharpe": "zero_risk_free_population_standard_deviation",
            "sortino": "zero_target_downside_deviation",
            "profit_factor": "gross_realized_net_trade_profit_over_gross_realized_net_trade_loss",
            "cvar_95": "mean_of_worst_5_percent_period_returns_minimum_one",
            "turnover": "sum_absolute_traded_notional_over_average_equity",
            "time_under_water": "periods_below_prior_equity_high",
            "exposure": "mean_period_capital_exposure_fraction",
            "undefined_ratio": "null",
        },
        "benchmarks": records,
        "report_json_path": (report_root / "report.json").as_posix(),
        "report_markdown_path": (report_root / "report.md").as_posix(),
        "report_content_sha256": "0" * 64,
        "report_markdown_sha256": "0" * 64,
    }
    report["report_content_sha256"] = _canonical_report_sha256(report)
    return report


def _report_markdown(report: dict[str, object]) -> bytes:
    records = report.get("benchmarks")
    if not isinstance(records, list):
        raise TypeError("report benchmarks must be a list")
    lines = [
        f"# Benchmark report: {report['report_id']}",
        "",
        f"- Dataset: `{report['dataset_id']}`",
        f"- Clean snapshot SHA-256: `{report['clean_snapshot_sha256']}`",
        f"- Scope: `{report['development_start']}` to `{report['development_end_exclusive']}`",
        f"- Report SHA-256: `{report['report_content_sha256']}`",
        "- SMA contract: completed close signal, next candle open execution, "
        "final close liquidation",
        "- Cost boundary: public Non-VIP fee plus fixed engineering spread/slippage assumptions",
        "",
        "| Timeframe | Benchmark | Scope | Net return | MDD | Sharpe | Profit factor | "
        "CVaR 95% | Turnover | TUW | Exposure |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("metrics"), dict):
            raise TypeError("benchmark record must contain metrics")
        metrics = record["metrics"]

        lines.append(
            "| "
            + " | ".join(
                str(item)
                for item in (
                    record["timeframe"],
                    record["name"],
                    record["scope"],
                    metrics.get("net_return") or "N/A",
                    metrics.get("maximum_drawdown") or "N/A",
                    metrics.get("sharpe") or "N/A",
                    metrics.get("profit_factor") or "N/A",
                    metrics.get("cvar_95") or "N/A",
                    metrics.get("turnover") or "N/A",
                    metrics.get("time_under_water_fraction") or "N/A",
                    metrics.get("exposure_fraction") or "N/A",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "`N/A` 表示数学上无定义，报告不会用 Infinity 或零值掩盖。",
            "SMA(200) 是固定工程基准，不属于 Donchian 候选或参数试验。",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def build_benchmark_report(
    project_root: Path, config_path: Path, quality_report_path: Path
) -> dict[str, object]:
    report = _assemble_report(project_root, config_path, quality_report_path)
    markdown = _report_markdown(report)
    report["report_markdown_sha256"] = hashlib.sha256(markdown).hexdigest()
    report_id = _required_str(report, "report_id")
    records = report.get("benchmarks")
    if not isinstance(records, list):
        raise TypeError("report benchmarks must be a list")
    final_root = project_root / REPORT_ROOT / report_id
    staging_root = project_root / REPORT_ROOT / f".staging-{report_id}-{uuid4().hex[:8]}"
    write_new_file(staging_root / "report.json", json_bytes(report))
    write_new_file(staging_root / "report.md", markdown)
    final_root.parent.mkdir(parents=True, exist_ok=True)
    publish_snapshot(staging_root, final_root)
    return {
        "status": "built",
        "report_id": report_id,
        "benchmark_count": len(records),
        "report_content_sha256": report["report_content_sha256"],
        "report_json_path": report["report_json_path"],
    }


def verify_benchmark_report(project_root: Path, report_path: Path) -> dict[str, object]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise TypeError("benchmark report must be a JSON object")
    recorded_hash = _required_str(report, "report_content_sha256")
    if _canonical_report_sha256(report) != recorded_hash:
        raise RuntimeError("benchmark report content hash mismatch")
    expected_path = project_root / _required_str(report, "report_json_path")
    if expected_path.resolve() != report_path.resolve():
        raise RuntimeError("benchmark report path does not match recorded path")
    markdown_path = project_root / _required_str(report, "report_markdown_path")
    if canonical_markdown_sha256(markdown_path) != _required_str(report, "report_markdown_sha256"):
        raise RuntimeError("benchmark Markdown hash mismatch")

    config_path = project_root / _required_str(report, "config_path")
    quality_report_path = project_root / _required_str(report, "quality_report_path")
    rebuilt = _assemble_report(project_root, config_path.resolve(), quality_report_path.resolve())
    if rebuilt["report_content_sha256"] != recorded_hash:
        raise RuntimeError("benchmark report does not match recomputed clean-data results")
    records = report.get("benchmarks")
    return {
        "status": "verified",
        "report_id": report["report_id"],
        "benchmark_count": len(records) if isinstance(records, list) else 0,
        "clean_snapshot_sha256": report["clean_snapshot_sha256"],
        "report_content_sha256": recorded_hash,
        "report_markdown_sha256": report["report_markdown_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("configs/research/benchmark-v1.toml"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--quality-report", type=Path)
    mode.add_argument("--verify-report", type=Path)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    if args.verify_report is not None:
        report_path = args.verify_report
        if not report_path.is_absolute():
            report_path = project_root / report_path
        result = verify_benchmark_report(project_root, report_path.resolve())
    else:
        config_path = args.config
        if not config_path.is_absolute():
            config_path = project_root / config_path
        quality_report_path = args.quality_report
        assert quality_report_path is not None
        if not quality_report_path.is_absolute():
            quality_report_path = project_root / quality_report_path
        result = build_benchmark_report(
            project_root, config_path.resolve(), quality_report_path.resolve()
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
