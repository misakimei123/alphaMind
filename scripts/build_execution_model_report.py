"""生成 P2-04 成本、成交与压力假设报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from alphamind.research.execution import ExecutionCostModel, build_p2_04_scenarios

ROOT_KEYS = {
    "schema_version",
    "model_version",
    "market_type",
    "timeframe",
    "fill_timing",
    "limit_fill_policy",
    "costs",
    "stress",
}
COST_KEYS = {
    "maker_fee_rate",
    "taker_fee_rate",
    "fee_source",
    "fee_source_checked_at_utc",
    "half_spread_rate",
    "slippage_rate_per_side",
}
STRESS_KEYS = {
    "fee_multiplier",
    "slippage_multiplier",
    "parameter_multipliers",
    "daily_price_shocks",
    "missing_candle_action",
    "delay_periods",
}


def _require_exact_keys(table: Mapping[str, Any], expected: set[str], location: str) -> None:
    missing = expected - set(table)
    unknown = set(table) - expected
    if missing:
        raise ValueError(f"{location} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{location} contains unknown keys: {', '.join(sorted(unknown))}")


def _require_table(value: object, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be a TOML table")
    return value


def _decimal(value: object, location: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{location} must be a valid decimal string") from error
    if not result.is_finite():
        raise ValueError(f"{location} must be finite")
    return result


def _decimal_list(value: object, location: str) -> tuple[Decimal, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{location} must be an array")
    return tuple(_decimal(item, f"{location}[]") for item in value)


def _non_empty_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a non-empty string")
    return value


def load_config(path: Path) -> Mapping[str, Any]:
    with path.open("rb") as config_file:
        config = tomllib.load(config_file)
    _require_exact_keys(config, ROOT_KEYS, "root")
    costs = _require_table(config["costs"], "costs")
    stress = _require_table(config["stress"], "stress")
    _require_exact_keys(costs, COST_KEYS, "costs")
    _require_exact_keys(stress, STRESS_KEYS, "stress")

    # bool 是 int 的子类，必须使用精确类型检查，避免 true 冒充版本号 1。
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    expected_strings = {
        "model_version": "p2-04-v1",
        "market_type": "spot",
        "timeframe": "4h",
        "fill_timing": "next_candle_open",
        "limit_fill_policy": "explicit_confirmation",
    }
    for name, expected in expected_strings.items():
        if config[name] != expected:
            raise ValueError(f"{name} must be {expected}")
    _non_empty_string(costs["fee_source"], "costs.fee_source")
    _non_empty_string(costs["fee_source_checked_at_utc"], "costs.fee_source_checked_at_utc")
    return config


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def build_report(config_path: Path) -> dict[str, object]:
    config = load_config(config_path)
    costs = _require_table(config["costs"], "costs")
    stress = _require_table(config["stress"], "stress")
    cost_model = ExecutionCostModel(
        maker_fee_rate=_decimal(costs["maker_fee_rate"], "costs.maker_fee_rate"),
        taker_fee_rate=_decimal(costs["taker_fee_rate"], "costs.taker_fee_rate"),
        half_spread_rate=_decimal(costs["half_spread_rate"], "costs.half_spread_rate"),
        slippage_rate_per_side=_decimal(
            costs["slippage_rate_per_side"], "costs.slippage_rate_per_side"
        ),
    )
    scenarios = build_p2_04_scenarios()
    by_id = {item.scenario_id: item for item in scenarios}
    if (
        _decimal(stress["fee_multiplier"], "stress.fee_multiplier")
        != by_id["fee_2x"].fee_multiplier
    ):
        raise ValueError("config fee multiplier differs from scenario contract")
    if (
        _decimal(stress["slippage_multiplier"], "stress.slippage_multiplier")
        != by_id["slippage_3x"].slippage_multiplier
    ):
        raise ValueError("config slippage multiplier differs from scenario contract")
    parameter_multipliers = _decimal_list(
        stress["parameter_multipliers"], "stress.parameter_multipliers"
    )
    scenario_parameters = tuple(
        item.parameter_multiplier for item in scenarios if item.parameter_multiplier is not None
    )
    if parameter_multipliers != scenario_parameters:
        raise ValueError("config parameter multipliers differ from scenario contract")
    daily_shocks = _decimal_list(stress["daily_price_shocks"], "stress.daily_price_shocks")
    scenario_shocks = tuple(
        item.daily_price_shock for item in scenarios if item.daily_price_shock is not None
    )
    if daily_shocks != scenario_shocks:
        raise ValueError("config daily shocks differ from scenario contract")
    delay_periods = stress["delay_periods"]
    # TOML boolean 与整数在 Python 中可等值，延迟周期也必须拒绝 bool 和 float。
    valid_delay = (
        isinstance(delay_periods, list)
        and len(delay_periods) == 1
        and type(delay_periods[0]) is int
        and delay_periods[0] == 1
    )
    if stress["missing_candle_action"] != "unfilled" or not valid_delay:
        raise ValueError("missing candle or delay contract mismatch")
    serialized_costs = _json_value(asdict(cost_model))
    if not isinstance(serialized_costs, dict):
        raise TypeError("serialized cost model must be an object")

    return {
        "schema_version": 1,
        "model_version": config["model_version"],
        "config_path": "configs/research/execution-model-v1.toml",
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "market_type": config["market_type"],
        "timeframe": config["timeframe"],
        "fill_contract": {
            "timing": config["fill_timing"],
            "limit_fill_policy": config["limit_fill_policy"],
            "same_candle_fill_allowed": False,
            "partial_fill_claimed": False,
        },
        "costs": {
            **serialized_costs,
            "fee_source": costs["fee_source"],
            "fee_source_checked_at_utc": costs["fee_source_checked_at_utc"],
            "assumption_boundary": (
                "fee 来自公开 Non-VIP spot rate；spread/slippage 是固定工程假设，不证明真实历史成交"
            ),
        },
        "scenarios": [_json_value(asdict(item)) for item in scenarios],
        "evidence_boundary": {
            "historical_backtest": "验证确定性成本敏感度，不证明真实 fill 或 partial fill",
            "dry_run": "验证模拟订单行为，不证明交易所接单",
            "live_canary": "首次验证受限真实写路径与实际成交偏差",
        },
    }


def render_markdown(report: Mapping[str, object]) -> str:
    costs = _require_table(report["costs"], "report.costs")
    scenarios = report["scenarios"]
    if not isinstance(scenarios, list):
        raise ValueError("report.scenarios must be an array")
    lines = [
        "# P2-04 Execution Model",
        "",
        f"- Model: `{report['model_version']}`",
        f"- Config SHA-256: `{report['config_sha256']}`",
        f"- Market/timeframe: `{report['market_type']}` / `{report['timeframe']}`",
        "- Fill: signal candle close -> next candle open; same-candle fill forbidden",
        "- Limit: candle touch alone is insufficient; explicit confirmation/assumption is required",
        "",
        "## Costs",
        "",
        f"- Maker fee: `{costs['maker_fee_rate']}`",
        f"- Taker fee: `{costs['taker_fee_rate']}`",
        f"- Half spread: `{costs['half_spread_rate']}`",
        f"- Slippage per side: `{costs['slippage_rate_per_side']}`",
        f"- Boundary: {costs['assumption_boundary']}",
        "",
        "## Scenario Matrix",
        "",
        "| Scenario | Fee x | Slippage x | Parameter x | Daily shock | "
        "Missing | Delay | Disclosure |",
        "|---|---:|---:|---:|---:|---|---:|---|",
    ]
    for raw_scenario in scenarios:
        scenario = _require_table(raw_scenario, "report.scenarios[]")
        lines.append(
            "| {scenario_id} | {fee_multiplier} | {slippage_multiplier} | "
            "{parameter_multiplier} | {daily_price_shock} | {missing_candle} | "
            "{execution_delay_periods} | {disclosure} |".format(
                **{key: "" if value is None else value for key, value in scenario.items()}
            )
        )
    lines.extend(
        [
            "",
            "## Evidence Boundary",
            "",
            "Historical backtest only validates deterministic cost sensitivity. It does not prove "
            "real fills, partial fills, exchange acceptance, or production write permissions.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    config_path = project_root / "configs/research/execution-model-v1.toml"
    output_root = project_root / "research/reports/execution-model/p2-04-v1"
    output_root.mkdir(parents=True, exist_ok=True)
    report = build_report(config_path)
    scenarios = report["scenarios"]
    if not isinstance(scenarios, list):
        raise TypeError("report scenarios must be an array")
    (output_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output_root / "report.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
    print(json.dumps({"scenario_count": len(scenarios), "status": "ok"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
