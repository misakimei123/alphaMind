"""登记并运行 P2-05 Walk-Forward 参数矩阵，生成可复核研究产物。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import tomllib
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from alphamind.research.experiment_registry import (
    file_sha256,
    finalize_experiment,
    locate_experiment,
    register_experiment,
    registration_sha256,
)
from alphamind.research.walk_forward import (
    BacktestResult,
    BacktestSettings,
    CostAssumptions,
    DonchianTrial,
    MarketBar,
    MarketConstraints,
    TradeRecord,
    WalkForwardFold,
    bootstrap_mean_confidence_interval,
    deflated_sharpe_probability,
    nonannualized_sharpe,
    profit_concentration,
    run_portfolio_backtest,
    validate_expanding_folds,
)

CONFIG_PATH = Path("configs/research/walk-forward-v1.toml")
REGISTRY_PATH = Path("research/experiments/trial-registry.json")
REPORT_ROOT = Path("research/reports/walk-forward/p2-05-v1")
TIMEFRAME_INTERVAL = {"4h": timedelta(hours=4), "1d": timedelta(days=1)}
DECIMAL_PRECISION = Decimal("0.000000000001")


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{name} must be an object with string keys")
    return value


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return value


def _string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be a non-empty string")
    return value


def _integer(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _decimal(document: dict[str, Any], key: str) -> Decimal:
    raw = _string(document, key)
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{key} must be an exact decimal string") from error
    if not value.is_finite():
        raise ValueError(f"{key} must be finite")
    return value


def _utc(value: str, name: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError(f"{name} must end with Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return parsed


def _text(value: Decimal | float | None) -> str | None:
    if value is None:
        return None
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    rounded = decimal_value.quantize(DECIMAL_PRECISION)
    if rounded == 0:
        rounded = Decimal("0")
    return format(rounded, "f")


def _write_json(path: Path, value: object, *, exclusive: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    with path.open(mode, encoding="utf-8", newline="\n") as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        output.write("\n")


def _load_config(root: Path) -> dict[str, Any]:
    with (root / CONFIG_PATH).open("rb") as source:
        config = tomllib.load(source)
    expected = {
        "schema_version",
        "model_version",
        "dataset_id",
        "dataset_manifest_path",
        "quality_report_path",
        "regime_manifest_path",
        "exchange_metadata_path",
        "strategy_project_commit",
        "registered_at_utc",
        "completed_at_utc",
        "random_seed",
        "initial_equity",
        "statistics",
        "risk",
        "costs",
        "walk_forward",
        "trials",
    }
    if set(config) != expected:
        raise ValueError("walk-forward config has unexpected or missing top-level keys")
    if _integer(config, "schema_version") != 1 or _string(config, "model_version") != "p2-05-v1":
        raise ValueError("unsupported walk-forward config version")
    _utc(_string(config, "registered_at_utc"), "registered_at_utc")
    _utc(_string(config, "completed_at_utc"), "completed_at_utc")
    if _utc(_string(config, "completed_at_utc"), "completed_at_utc") < _utc(
        _string(config, "registered_at_utc"), "registered_at_utc"
    ):
        raise ValueError("completed_at_utc must not precede registered_at_utc")
    _validate_cross_config(root, config)
    return config


def _validate_cross_config(root: Path, config: dict[str, Any]) -> None:
    """防止 Walk-Forward 复制值与 Strategy Card、风险和成本权威配置分叉。"""

    yaml = importlib.import_module("yaml")
    card = _mapping(
        yaml.safe_load(
            (root / "research/strategy_cards/donchian_trend_v0.1.0.yaml").read_text(
                encoding="utf-8"
            )
        ),
        "strategy card",
    )
    card_trials = _mapping(card.get("parameter_trials"), "strategy_card.parameter_trials")
    baseline = _mapping(card_trials.get("baseline"), "strategy_card.parameter_trials.baseline")
    perturbations = _mapping(
        card_trials.get("perturbations"), "strategy_card.parameter_trials.perturbations"
    )
    configured_trials = _mapping(config.get("trials"), "trials")
    trial_pairs = (
        ("baseline_entry_window", baseline.get("entry_window")),
        ("baseline_exit_window", baseline.get("exit_window")),
        ("baseline_atr_period", baseline.get("atr_period")),
        ("baseline_stop_multiple", baseline.get("stop_multiple")),
        ("entry_windows", perturbations.get("entry_window")),
        ("exit_windows", perturbations.get("exit_window")),
        ("stop_multiples", perturbations.get("stop_multiple")),
        ("maximum_trials", card_trials.get("maximum_parameterized_trials")),
        ("cartesian_product_allowed", card_trials.get("cartesian_product_allowed")),
    )
    for key, expected in trial_pairs:
        if configured_trials.get(key) != expected:
            raise ValueError(f"trials.{key} differs from the frozen Strategy Card")

    card_risk = _mapping(card.get("risk"), "strategy_card.risk")
    configured_risk = _mapping(config.get("risk"), "risk")
    if configured_risk.get("risk_fraction") != card_risk.get("planned_risk_fraction"):
        raise ValueError("risk_fraction differs from the frozen Strategy Card")
    if configured_risk.get("symbol_exposure_fraction") != card_risk.get(
        "max_symbol_notional_fraction_of_nav"
    ):
        raise ValueError("symbol exposure differs from the frozen Strategy Card")
    if configured_risk.get("directional_exposure_fraction") != card_risk.get(
        "max_directional_notional_fraction_of_nav"
    ):
        raise ValueError("directional exposure differs from the frozen Strategy Card")
    with (root / "configs/common/risk-limits.toml").open("rb") as source:
        risk_limits = tomllib.load(source)
    trade_limits = _mapping(risk_limits.get("trade_limits"), "risk_limits.trade_limits")
    if configured_risk.get("risk_fraction") != trade_limits.get("risk_fraction"):
        raise ValueError("risk_fraction differs from risk-limits.toml")

    configured_costs = _mapping(config.get("costs"), "costs")
    with (root / _string(configured_costs, "config_path")).open("rb") as source:
        execution = tomllib.load(source)
    execution_costs = _mapping(execution.get("costs"), "execution.costs")
    execution_stress = _mapping(execution.get("stress"), "execution.stress")
    cost_pairs = (
        ("fee_rate_per_side", execution_costs.get("taker_fee_rate")),
        ("half_spread_rate", execution_costs.get("half_spread_rate")),
        ("slippage_rate_per_side", execution_costs.get("slippage_rate_per_side")),
        ("stress_fee_multiplier", execution_stress.get("fee_multiplier")),
        ("stress_slippage_multiplier", execution_stress.get("slippage_multiplier")),
    )
    for key, expected in cost_pairs:
        if configured_costs.get(key) != expected:
            raise ValueError(f"costs.{key} differs from execution-model-v1.toml")


def _load_yaml(path: Path) -> dict[str, Any]:
    yaml = importlib.import_module("yaml")
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))


def _folds(root: Path, config: dict[str, Any]) -> tuple[WalkForwardFold, ...]:
    manifest = _load_yaml(root / _string(config, "regime_manifest_path"))
    raw_walk_forward = _mapping(manifest.get("walk_forward"), "regime.walk_forward")
    configured = _mapping(config.get("walk_forward"), "config.walk_forward")
    if configured.get("method") != raw_walk_forward.get("method"):
        raise ValueError("walk-forward method differs from regime manifest")
    if configured.get("random_split_allowed") is not False:
        raise ValueError("random_split_allowed must be false")
    for key in ("validation_months", "step_months"):
        if _integer(configured, key) != raw_walk_forward.get(key):
            raise ValueError(f"walk-forward {key} differs from regime manifest")
    values: list[WalkForwardFold] = []
    for raw in _list(raw_walk_forward.get("folds"), "walk_forward.folds"):
        item = _mapping(raw, "walk_forward.fold")
        values.append(
            WalkForwardFold(
                _string(item, "id"),
                _utc(_string(item, "train_start"), "train_start"),
                _utc(_string(item, "train_end_exclusive"), "train_end_exclusive"),
                _utc(_string(item, "validation_start"), "validation_start"),
                _utc(
                    _string(item, "validation_end_exclusive"),
                    "validation_end_exclusive",
                ),
            )
        )
    folds = tuple(values)
    validate_expanding_folds(folds)
    return folds


def _trials(config: dict[str, Any]) -> tuple[DonchianTrial, ...]:
    raw = _mapping(config.get("trials"), "trials")
    baseline_entry = _integer(raw, "baseline_entry_window")
    baseline_exit = _integer(raw, "baseline_exit_window")
    baseline_atr = _integer(raw, "baseline_atr_period")
    baseline_stop = _decimal(raw, "baseline_stop_multiple")
    values = [
        DonchianTrial(1, "baseline", baseline_entry, baseline_exit, baseline_atr, baseline_stop)
    ]
    index = 2
    for entry_window in _list(raw.get("entry_windows"), "entry_windows"):
        if type(entry_window) is not int:
            raise TypeError("entry_windows must contain integers")
        values.append(
            DonchianTrial(
                index,
                f"entry_{entry_window}",
                entry_window,
                baseline_exit,
                baseline_atr,
                baseline_stop,
                "entry_window",
            )
        )
        index += 1
    for exit_window in _list(raw.get("exit_windows"), "exit_windows"):
        if type(exit_window) is not int:
            raise TypeError("exit_windows must contain integers")
        values.append(
            DonchianTrial(
                index,
                f"exit_{exit_window}",
                baseline_entry,
                exit_window,
                baseline_atr,
                baseline_stop,
                "exit_window",
            )
        )
        index += 1
    for raw_stop in _list(raw.get("stop_multiples"), "stop_multiples"):
        stop_multiple = Decimal(str(raw_stop))
        values.append(
            DonchianTrial(
                index,
                f"stop_{str(raw_stop).replace('.', '_')}",
                baseline_entry,
                baseline_exit,
                baseline_atr,
                stop_multiple,
                "stop_multiple",
            )
        )
        index += 1
    trials = tuple(values)
    if len(trials) != _integer(raw, "expected_parameter_trials"):
        raise ValueError("parameter trial matrix size differs from config")
    if _integer(raw, "maximum_trials") < len(trials):
        raise ValueError("parameter trial matrix exceeds maximum_trials")
    if raw.get("cartesian_product_allowed") is not False:
        raise ValueError("cartesian_product_allowed must be false")
    if len({trial.trial_id for trial in trials}) != len(trials):
        raise ValueError("trial ids must be unique")
    return trials


def _settings(config: dict[str, Any]) -> BacktestSettings:
    risk = _mapping(config.get("risk"), "risk")
    statistics_config = _mapping(config.get("statistics"), "statistics")
    return BacktestSettings(
        initial_equity=_decimal(config, "initial_equity"),
        risk_fraction=_decimal(risk, "risk_fraction"),
        symbol_exposure_fraction=_decimal(risk, "symbol_exposure_fraction"),
        directional_exposure_fraction=_decimal(risk, "directional_exposure_fraction"),
        volatility_cap_fraction=_decimal(risk, "volatility_cap_fraction"),
        maximum_unit_loss_fraction=_decimal(risk, "maximum_unit_loss_fraction"),
        event_cluster_hours=_integer(statistics_config, "independent_event_clustering_hours"),
    )


def _costs(config: dict[str, Any], *, stressed: bool = False) -> CostAssumptions:
    raw = _mapping(config.get("costs"), "costs")
    fee = _decimal(raw, "fee_rate_per_side")
    slippage = _decimal(raw, "slippage_rate_per_side")
    if stressed:
        fee *= _decimal(raw, "stress_fee_multiplier")
        slippage *= _decimal(raw, "stress_slippage_multiplier")
    return CostAssumptions(
        fee,
        _decimal(raw, "half_spread_rate"),
        slippage,
        _decimal(_mapping(config.get("risk"), "risk"), "gap_buffer_rate"),
    )


def _constraints(root: Path, config: dict[str, Any]) -> dict[str, MarketConstraints]:
    path = root / _string(config, "exchange_metadata_path")
    metadata = _mapping(json.loads(path.read_text(encoding="utf-8")), "exchange metadata")
    constraints: dict[str, MarketConstraints] = {}
    for raw in _list(metadata.get("markets"), "markets"):
        market = _mapping(raw, "market")
        pair = _string(market, "symbol")
        precision = _mapping(market.get("precision"), "precision")
        limits = _mapping(market.get("limits"), "limits")
        amount = _mapping(limits.get("amount"), "limits.amount")
        cost = _mapping(limits.get("cost"), "limits.cost")
        constraints[pair] = MarketConstraints(
            Decimal(str(precision["price"])),
            Decimal(str(precision["amount"])),
            Decimal(str(amount["min"])),
            Decimal(str(cost["min"])),
        )
    if set(constraints) != {"BTC/USDT", "ETH/USDT"}:
        raise ValueError("exchange metadata must contain exactly BTC/USDT and ETH/USDT")
    return constraints


def _load_bars(root: Path, config: dict[str, Any]) -> dict[tuple[str, str], tuple[MarketBar, ...]]:
    report_path = root / _string(config, "quality_report_path")
    quality = _mapping(json.loads(report_path.read_text(encoding="utf-8")), "quality report")
    if (
        quality.get("status") != "ACCEPTED"
        or quality.get("downstream_experiment_allowed") is not True
    ):
        raise RuntimeError("quality report does not allow downstream experiments")
    if quality.get("dataset_id") != config.get("dataset_id"):
        raise RuntimeError("quality report dataset_id differs from config")
    clean_root = root / _string(quality, "clean_root")
    pd = importlib.import_module("pandas")
    loaded: dict[tuple[str, str], tuple[MarketBar, ...]] = {}
    for raw in _list(quality.get("clean_partitions"), "clean_partitions"):
        partition = _mapping(raw, "clean partition")
        pair = _string(partition, "pair")
        timeframe = _string(partition, "timeframe")
        relative_path = _string(partition, "relative_path")
        frame = pd.read_feather(clean_root / relative_path)
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        loaded[(pair, timeframe)] = tuple(
            MarketBar(
                timestamp.to_pydatetime(),
                Decimal(str(open_price)),
                Decimal(str(high)),
                Decimal(str(low)),
                Decimal(str(close)),
                Decimal(str(volume)),
            )
            for timestamp, open_price, high, low, close, volume in zip(
                timestamps,
                frame["open"],
                frame["high"],
                frame["low"],
                frame["close"],
                frame["volume"],
                strict=True,
            )
        )
    expected = {
        (pair, timeframe) for pair in ("BTC/USDT", "ETH/USDT") for timeframe in ("4h", "1d")
    }
    if set(loaded) != expected:
        raise RuntimeError("clean data must contain exactly BTC/ETH 4h/1d")
    return loaded


def _experiment_id(registered_at: str, trial: DonchianTrial) -> str:
    timestamp = registered_at.replace("-", "").replace(":", "")
    suffix = hashlib.sha256(trial.trial_id.encode()).hexdigest()[:12]
    return f"exp-{timestamp}-{suffix}"


def _registration(
    root: Path,
    config: dict[str, Any],
    fold_values: tuple[WalkForwardFold, ...],
    trial: DonchianTrial,
) -> dict[str, object]:
    hypothesis_path = Path("research/hypotheses/donchian_trend_v1.yaml")
    strategy_path = Path("research/strategy_cards/donchian_trend_v0.1.0.yaml")
    runtime_path = Path("configs/common/runtime-versions.toml")
    dataset_path = Path(_string(config, "dataset_manifest_path"))
    cost_path = Path(_string(_mapping(config.get("costs"), "costs"), "config_path"))
    config_path = CONFIG_PATH
    hypothesis = _load_yaml(root / hypothesis_path)
    registered_at = _string(config, "registered_at_utc")
    experiment: dict[str, object] = {
        "artifacts": [],
        "completed_at_utc": None,
        "cost_model": {
            "all_costs_nonnegative": True,
            "config_path": cost_path.as_posix(),
            "config_sha256": file_sha256(root / cost_path),
            "fee_rate": _string(_mapping(config.get("costs"), "costs"), "fee_rate_per_side"),
            "gap_buffer_rate": _string(_mapping(config.get("risk"), "risk"), "gap_buffer_rate"),
            "slippage_rate": format(
                _decimal(_mapping(config.get("costs"), "costs"), "half_spread_rate")
                + _decimal(_mapping(config.get("costs"), "costs"), "slippage_rate_per_side"),
                "f",
            ),
            "version": "p2-04-v1",
        },
        "created_at_utc": registered_at,
        "dataset": {
            "dataset_id": _string(config, "dataset_id"),
            "development_end_exclusive": "2026-07-01T00:00:00Z",
            "development_start": "2022-01-01T00:00:00Z",
            "feature_version": "ohlcv-v1",
            "final_holdout_end_exclusive": "2026-07-01T00:00:00Z",
            "final_holdout_start": "2025-07-01T00:00:00Z",
            "holdout_access_count": 1,
            "holdout_first_access_commit": "630380e7dcf03744f3419366a7874bae3c0d8002",
            "holdout_state": "DEGRADED_TO_DEVELOPMENT",
            "manifest_path": dataset_path.as_posix(),
            "manifest_sha256": file_sha256(root / dataset_path),
        },
        "evidence_layer": "historical_backtest",
        "experiment_id": _experiment_id(registered_at, trial),
        "hypothesis": {
            "economic_rationale": _string(hypothesis, "economic_rationale"),
            "falsification_conditions": _list(
                hypothesis.get("falsification_conditions"), "falsification_conditions"
            ),
            "hypothesis_id": _string(hypothesis, "hypothesis_id"),
            "hypothesis_path": hypothesis_path.as_posix(),
            "hypothesis_sha256": file_sha256(root / hypothesis_path),
            "pass_condition": _string(hypothesis, "pass_condition"),
            "primary_metric": _string(hypothesis, "primary_metric"),
            "statement": _string(hypothesis, "statement"),
        },
        "owner": "alphamind-research",
        "registration_sha256": "",
        "result": None,
        "review_result": "PENDING",
        "runtime": {
            "ccxt_version": "4.5.61",
            "freqtrade_version": "2026.6",
            "python_version": "3.12.9",
            "random_seed": _integer(config, "random_seed"),
            "runtime_lock_path": runtime_path.as_posix(),
            "runtime_lock_sha256": file_sha256(root / runtime_path),
        },
        "schema_version": 1,
        "started_at_utc": None,
        "status": "PRE_REGISTERED",
        "strategy": {
            "parameters": {
                "atr_period": trial.atr_period,
                "entry_window": trial.entry_window,
                "exit_window": trial.exit_window,
                "stop_multiple": format(trial.stop_multiple, "f"),
                "trial_id": trial.trial_id,
            },
            "project_commit": _string(config, "strategy_project_commit"),
            "strategy_card_path": strategy_path.as_posix(),
            "strategy_card_sha256": file_sha256(root / strategy_path),
            "strategy_config_path": config_path.as_posix(),
            "strategy_config_sha256": file_sha256(root / config_path),
            "strategy_id": "donchian_trend",
            "strategy_version": "0.1.0",
        },
        "trial_budget": {
            "maximum_trials": _integer(_mapping(config.get("trials"), "trials"), "maximum_trials"),
            "parameter_selection_allowed": True,
            "prior_result_used": False,
            "trial_index": trial.trial_index,
        },
        "validation": {
            "folds": [fold.fold_id for fold in fold_values],
            "lookahead_analysis_required": True,
            "metrics": [
                "expectancy_r",
                "maximum_drawdown",
                "trade_count",
                "independent_event_count",
                "bootstrap_confidence_interval",
                "deflated_sharpe_probability",
                "profit_concentration",
            ],
            "recursive_analysis_required": True,
            "regime_reporting_required": True,
            "slices": {
                "holdout": [],
                "stress": [
                    {
                        "end_exclusive": end,
                        "slice_id": slice_id,
                        "start": start,
                    }
                    for slice_id, start, end in (
                        ("calendar_stress_2022_q2", "2022-05-01T00:00:00Z", "2022-07-01T00:00:00Z"),
                        ("calendar_stress_2022_q4", "2022-11-01T00:00:00Z", "2023-01-01T00:00:00Z"),
                        ("calendar_trend_2024_q1", "2024-02-01T00:00:00Z", "2024-04-15T00:00:00Z"),
                        (
                            "calendar_stress_2024_aug",
                            "2024-08-01T00:00:00Z",
                            "2024-09-01T00:00:00Z",
                        ),
                    )
                ],
                "train": [
                    {
                        "end_exclusive": fold.train_end_exclusive.isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "slice_id": f"{fold.fold_id}-train",
                        "start": fold.train_start.isoformat().replace("+00:00", "Z"),
                    }
                    for fold in fold_values
                ],
                "validation": [
                    {
                        "end_exclusive": fold.validation_end_exclusive.isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "slice_id": f"{fold.fold_id}-validation",
                        "start": fold.validation_start.isoformat().replace("+00:00", "Z"),
                    }
                    for fold in fold_values
                ],
            },
            "walk_forward_manifest": "data/manifests/regime-manifest.yaml",
        },
    }
    experiment["registration_sha256"] = registration_sha256(experiment)
    return experiment


def _run_fold(
    bars: dict[tuple[str, str], tuple[MarketBar, ...]],
    timeframe: str,
    start: datetime,
    end: datetime,
    trial: DonchianTrial,
    costs: CostAssumptions,
    constraints: dict[str, MarketConstraints],
    settings: BacktestSettings,
) -> BacktestResult:
    return run_portfolio_backtest(
        {pair: bars[(pair, timeframe)] for pair in ("BTC/USDT", "ETH/USDT")},
        evaluation_start=start,
        evaluation_end_exclusive=end,
        expected_interval=TIMEFRAME_INTERVAL[timeframe],
        trial=trial,
        costs=costs,
        constraints=constraints,
        settings=settings,
    )


def _fold_record(fold_id: str, result: BacktestResult) -> dict[str, object]:
    return {
        "expectancy_r": _text(result.expectancy_r),
        "final_equity": _text(result.final_equity),
        "fold_id": fold_id,
        "independent_event_count": result.independent_event_count,
        "maximum_drawdown": _text(result.maximum_drawdown),
        "net_return": _text(result.net_return),
        "trade_count": len(result.trades),
    }


def _trade_record(trade: TradeRecord, fold_id: str) -> dict[str, object]:
    record = asdict(trade)
    for key in ("entry_signal_timestamp", "entry_timestamp", "exit_timestamp"):
        timestamp = record[key]
        if not isinstance(timestamp, datetime):
            raise TypeError(f"trade {key} must be datetime")
        record[key] = timestamp.isoformat().replace("+00:00", "Z")
    for key in ("entry_price", "exit_price", "quantity", "net_pnl_quote", "return_r"):
        value = record[key]
        if not isinstance(value, Decimal):
            raise TypeError(f"trade {key} must be Decimal")
        record[key] = format(value, "f")
    record["fold_id"] = fold_id
    return record


def _aggregate(
    fold_results: list[tuple[str, BacktestResult]], config: dict[str, Any], seed_offset: int
) -> tuple[dict[str, object], list[object], tuple[float, ...]]:
    trades = [(fold_id, trade) for fold_id, result in fold_results for trade in result.trades]
    returns_r = tuple(trade.return_r for _, trade in trades)
    statistics_config = _mapping(config.get("statistics"), "statistics")
    interval = bootstrap_mean_confidence_interval(
        returns_r,
        confidence_level=_decimal(statistics_config, "confidence_level"),
        resamples=_integer(statistics_config, "bootstrap_resamples"),
        seed=_integer(config, "random_seed") + seed_offset,
    )
    period_returns = tuple(value for _, result in fold_results for value in result.period_returns)
    compounded = math.prod(1 + result.net_return for _, result in fold_results) - Decimal("1")
    aggregate: dict[str, object] = {
        "bootstrap_expectancy_r_ci": (
            {"lower": _text(interval[0]), "upper": _text(interval[1])}
            if interval is not None
            else None
        ),
        "expectancy_r": (
            _text(sum(returns_r, Decimal("0")) / Decimal(len(returns_r))) if returns_r else None
        ),
        "fold_count": len(fold_results),
        "independent_event_count": sum(
            result.independent_event_count for _, result in fold_results
        ),
        "maximum_drawdown": _text(max(result.maximum_drawdown for _, result in fold_results)),
        "net_return_compounded_across_reset_folds": _text(compounded),
        "nonannualized_sharpe": _text(nonannualized_sharpe(period_returns)),
        "positive_expectancy_fold_count": sum(
            1
            for _, result in fold_results
            if result.expectancy_r is not None and result.expectancy_r > 0
        ),
        "trade_count": len(trades),
    }
    return aggregate, [_trade_record(trade, fold_id) for fold_id, trade in trades], period_returns


def _concentration_record(trades: list[tuple[str, TradeRecord]]) -> dict[str, object]:
    result = profit_concentration(tuple(trade for _, trade in trades))
    top_five = result["top_5_trades"]
    pair_net = result["pair_net_pnl_quote"]
    if not isinstance(top_five, tuple) or not isinstance(pair_net, dict):
        raise TypeError("profit concentration returned an invalid shape")
    return {
        "gross_profit_quote": _text(result["gross_profit_quote"]),  # type: ignore[arg-type]
        "pair_net_pnl_quote": {str(key): _text(value) for key, value in pair_net.items()},
        "positive_profit_hhi": _text(result["positive_profit_hhi"]),  # type: ignore[arg-type]
        "top_5_profit_contribution": _text(result["top_5_profit_contribution"]),  # type: ignore[arg-type]
        "top_5_trades": [
            _trade_record(trade, next(fold for fold, item in trades if item is trade))
            for trade in top_five
        ],
    }


def _calendar_slices(trades: list[tuple[str, TradeRecord]]) -> list[dict[str, object]]:
    slices = (
        (
            "calendar_stress_2022_q2",
            datetime(2022, 5, 1, tzinfo=UTC),
            datetime(2022, 7, 1, tzinfo=UTC),
        ),
        (
            "calendar_stress_2022_q4",
            datetime(2022, 11, 1, tzinfo=UTC),
            datetime(2023, 1, 1, tzinfo=UTC),
        ),
        (
            "calendar_trend_2024_q1",
            datetime(2024, 2, 1, tzinfo=UTC),
            datetime(2024, 4, 15, tzinfo=UTC),
        ),
        (
            "calendar_stress_2024_aug",
            datetime(2024, 8, 1, tzinfo=UTC),
            datetime(2024, 9, 1, tzinfo=UTC),
        ),
    )
    records: list[dict[str, object]] = []
    for slice_id, start, end in slices:
        selected = [trade for _, trade in trades if start <= trade.entry_timestamp < end]
        records.append(
            {
                "expectancy_r": (
                    _text(
                        sum((trade.return_r for trade in selected), Decimal("0"))
                        / Decimal(len(selected))
                    )
                    if selected
                    else None
                ),
                "net_pnl_quote": _text(
                    sum((trade.net_pnl_quote for trade in selected), Decimal("0"))
                ),
                "slice_id": slice_id,
                "trade_count": len(selected),
            }
        )
    return records


def _compute(root: Path, config: dict[str, Any]) -> dict[str, dict[str, object]]:
    folds = _folds(root, config)
    trials = _trials(config)
    bars = _load_bars(root, config)
    constraints = _constraints(root, config)
    settings = _settings(config)
    base_costs = _costs(config)
    computed: dict[str, dict[str, object]] = {}
    raw_validation: dict[str, list[tuple[str, BacktestResult]]] = {}
    raw_trades: dict[str, list[tuple[str, TradeRecord]]] = {}
    period_returns_by_trial: dict[str, tuple[float, ...]] = {}

    for trial in trials:
        train_results: list[tuple[str, BacktestResult]] = []
        validation_results: list[tuple[str, BacktestResult]] = []
        for fold in folds:
            train_results.append(
                (
                    fold.fold_id,
                    _run_fold(
                        bars,
                        "4h",
                        fold.train_start,
                        fold.train_end_exclusive,
                        trial,
                        base_costs,
                        constraints,
                        settings,
                    ),
                )
            )
            validation_results.append(
                (
                    fold.fold_id,
                    _run_fold(
                        bars,
                        "4h",
                        fold.validation_start,
                        fold.validation_end_exclusive,
                        trial,
                        base_costs,
                        constraints,
                        settings,
                    ),
                )
            )
        aggregate, trades, period_returns = _aggregate(
            validation_results, config, trial.trial_index
        )
        flat_trades = [
            (fold_id, trade) for fold_id, result in validation_results for trade in result.trades
        ]
        computed[trial.trial_id] = {
            "aggregate_validation": aggregate,
            "calendar_slices": _calendar_slices(flat_trades),
            "concentration": _concentration_record(flat_trades),
            "parameters": {
                "atr_period": trial.atr_period,
                "changed_parameter": trial.changed_parameter,
                "entry_window": trial.entry_window,
                "exit_window": trial.exit_window,
                "stop_multiple": format(trial.stop_multiple, "f"),
            },
            "train_folds": [_fold_record(fold_id, result) for fold_id, result in train_results],
            "trial_id": trial.trial_id,
            "trial_index": trial.trial_index,
            "trades": trades,
            "validation_folds": [
                _fold_record(fold_id, result) for fold_id, result in validation_results
            ],
        }
        raw_validation[trial.trial_id] = validation_results
        raw_trades[trial.trial_id] = flat_trades
        period_returns_by_trial[trial.trial_id] = period_returns

    sharpes = tuple(
        sharpe
        for trial in trials
        if (sharpe := nonannualized_sharpe(period_returns_by_trial[trial.trial_id])) is not None
    )
    if len(sharpes) != len(trials):
        raise RuntimeError("all parameter trials must produce a defined Sharpe estimate")
    expected_dsr_trials = _integer(
        _mapping(config.get("statistics"), "statistics"), "deflated_sharpe_trial_count"
    )
    if expected_dsr_trials != len(sharpes):
        raise RuntimeError("DSR trial count differs from the complete parameter matrix")
    for trial in trials:
        correction = deflated_sharpe_probability(period_returns_by_trial[trial.trial_id], sharpes)
        computed[trial.trial_id]["deflated_sharpe"] = (
            {
                "effective_trial_count_policy": "raw_13_conservative_upper_bound",
                "expected_maximum_nonannualized_sharpe": _text(correction[1]),
                "probability": _text(correction[0]),
            }
            if correction is not None
            else None
        )

    baseline = trials[0]
    stressed_results = [
        (
            fold.fold_id,
            _run_fold(
                bars,
                "4h",
                fold.validation_start,
                fold.validation_end_exclusive,
                baseline,
                _costs(config, stressed=True),
                constraints,
                settings,
            ),
        )
        for fold in folds
    ]
    stress_aggregate, _, _ = _aggregate(stressed_results, config, 1000)
    robustness_results = [
        (
            fold.fold_id,
            _run_fold(
                bars,
                "1d",
                fold.validation_start,
                fold.validation_end_exclusive,
                baseline,
                base_costs,
                constraints,
                settings,
            ),
        )
        for fold in folds
    ]
    robustness_aggregate, _, _ = _aggregate(robustness_results, config, 2000)
    computed["baseline"]["stress_cost_validation"] = stress_aggregate
    computed["baseline"]["robustness_1d_validation"] = robustness_aggregate

    neighbor_aggregates = [
        _mapping(
            computed[trial.trial_id].get("aggregate_validation"),
            f"{trial.trial_id}.aggregate_validation",
        )
        for trial in trials[1:]
    ]
    nonnegative_neighbors = sum(
        1
        for aggregate in neighbor_aggregates
        if aggregate.get("expectancy_r") is not None
        and Decimal(str(aggregate["expectancy_r"])) >= 0
    )
    neighbor_ratio = Decimal(nonnegative_neighbors) / Decimal(len(trials) - 1)
    baseline_aggregate = _mapping(
        computed["baseline"].get("aggregate_validation"), "baseline.aggregate_validation"
    )
    evidence_thresholds = {
        "minimum_independent_breakout_events": 12,
        "minimum_out_of_sample_completed_trades": 40,
    }
    failures: list[str] = []
    if int(baseline_aggregate["positive_expectancy_fold_count"]) < 2:
        failures.append("majority_walk_forward_folds_have_non_positive_net_expectancy")
    stressed_expectancy = stress_aggregate.get("expectancy_r")
    if stressed_expectancy is None or Decimal(str(stressed_expectancy)) <= 0:
        failures.append("stressed_aggregate_oos_expectancy_is_non_positive")
    if neighbor_ratio < Decimal("0.70"):
        failures.append("fewer_than_70_percent_neighbor_trials_keep_non_negative_expectancy")
    if Decimal(str(baseline_aggregate["maximum_drawdown"])) >= _decimal(
        _mapping(config.get("risk"), "risk"), "maximum_drawdown_fraction"
    ):
        failures.append("cashflow_adjusted_drawdown_reaches_5_percent_kill_switch")
    insufficient = []
    if (
        int(baseline_aggregate["trade_count"])
        < evidence_thresholds["minimum_out_of_sample_completed_trades"]
    ):
        insufficient.append("insufficient_out_of_sample_completed_trades")
    if (
        int(baseline_aggregate["independent_event_count"])
        < evidence_thresholds["minimum_independent_breakout_events"]
    ):
        insufficient.append("insufficient_independent_breakout_events")
    computed["baseline"]["p2_05_assessment"] = {
        "evidence_thresholds": evidence_thresholds,
        "falsification_reason_codes": failures,
        "independent_review_status": "PENDING",
        "insufficient_evidence_reason_codes": insufficient,
        "neighbor_nonnegative_count": nonnegative_neighbors,
        "neighbor_nonnegative_ratio": _text(neighbor_ratio),
        "parameter_selection": None,
        "parameter_selection_blocker": "independent_review_and_p2_06_pending",
        "status": "FAIL" if failures else "INCONCLUSIVE" if insufficient else "PASS",
    }
    return computed


def _trial_outcome(metrics: dict[str, object]) -> tuple[str, list[str]]:
    aggregate = _mapping(metrics.get("aggregate_validation"), "aggregate_validation")
    expectancy = aggregate.get("expectancy_r")
    if expectancy is None:
        return "INCONCLUSIVE", ["no_out_of_sample_trades"]
    if Decimal(str(expectancy)) < 0:
        return "FAIL", ["negative_out_of_sample_expectancy"]
    return "PASS", ["nonnegative_out_of_sample_expectancy"]


def _summary(
    root: Path, config: dict[str, Any], computed: dict[str, dict[str, object]]
) -> dict[str, object]:
    trials = _trials(config)
    return {
        "config_path": CONFIG_PATH.as_posix(),
        "config_sha256": file_sha256(root / CONFIG_PATH),
        "dataset_manifest_path": _string(config, "dataset_manifest_path"),
        "dataset_manifest_sha256": file_sha256(root / _string(config, "dataset_manifest_path")),
        "evidence_boundary": {
            "anti_cheat": "P2-06 pending; no parameter is selectable",
            "final_holdout": "not used; validation ends at 2025-07-01",
            "live_execution": "not tested",
            "review": "all experiment review_result values remain PENDING",
        },
        "folds": [
            {
                "fold_id": fold.fold_id,
                "train_start": fold.train_start.isoformat().replace("+00:00", "Z"),
                "train_end_exclusive": fold.train_end_exclusive.isoformat().replace("+00:00", "Z"),
                "validation_start": fold.validation_start.isoformat().replace("+00:00", "Z"),
                "validation_end_exclusive": fold.validation_end_exclusive.isoformat().replace(
                    "+00:00", "Z"
                ),
            }
            for fold in _folds(root, config)
        ],
        "model_version": _string(config, "model_version"),
        "registry_path": REGISTRY_PATH.as_posix(),
        "registry_sha256": file_sha256(root / REGISTRY_PATH),
        "schema_version": 1,
        "selection": computed["baseline"]["p2_05_assessment"],
        "trial_count": len(trials),
        "trials": [
            {
                "deflated_sharpe": computed[trial.trial_id]["deflated_sharpe"],
                "experiment_id": _experiment_id(_string(config, "registered_at_utc"), trial),
                "metrics_path": (
                    f"research/experiments/"
                    f"{_experiment_id(_string(config, 'registered_at_utc'), trial)}/metrics.json"
                ),
                "outcome": _trial_outcome(computed[trial.trial_id])[0],
                "parameters": computed[trial.trial_id]["parameters"],
                "trial_id": trial.trial_id,
                "trial_index": trial.trial_index,
                "validation": computed[trial.trial_id]["aggregate_validation"],
            }
            for trial in trials
        ],
    }


def _markdown(summary: dict[str, object]) -> str:
    trials = _list(summary.get("trials"), "summary.trials")
    selection = _mapping(summary.get("selection"), "summary.selection")
    lines = [
        "# P2-05 Walk-Forward report",
        "",
        f"- Model: `{summary['model_version']}`",
        f"- Trials: `{summary['trial_count']}` preregistered OAT candidates",
        "- Folds: expanding train, three contiguous six-month validation windows",
        "- Final holdout: not used; all validation ends before `2025-07-01`",
        f"- Assessment: `{selection['status']}`",
        "- Selection: blocked until independent review and P2-06 anti-cheat checks",
        "",
        "| Trial | Parameters | OOS expectancy R | Trades | Events | MDD | DSR probability |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for raw in trials:
        trial = _mapping(raw, "summary.trial")
        parameters = _mapping(trial.get("parameters"), "trial.parameters")
        validation = _mapping(trial.get("validation"), "trial.validation")
        correction = _mapping(trial.get("deflated_sharpe"), "trial.deflated_sharpe")
        lines.append(
            f"| {trial['trial_id']} | {parameters['entry_window']}/"
            f"{parameters['exit_window']}/ATR {parameters['atr_period']} x "
            f"{parameters['stop_multiple']} | {validation['expectancy_r']} | "
            f"{validation['trade_count']} | {validation['independent_event_count']} | "
            f"{validation['maximum_drawdown']} | {correction['probability']} |"
        )
    lines.extend(
        [
            "",
            "## Assessment",
            "",
            f"- Neighbor nonnegative ratio: `{selection['neighbor_nonnegative_ratio']}`",
            f"- Falsification reasons: `{selection['falsification_reason_codes']}`",
            f"- Evidence gaps: `{selection['insufficient_evidence_reason_codes']}`",
            "- Profit concentration, Top 5 trades, pair contribution, fold metrics, "
            "bootstrap CI and calendar slices are retained in each trial `metrics.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _publish_summary(root: Path, summary: dict[str, object]) -> None:
    summary_path = root / REPORT_ROOT / "summary.json"
    markdown_path = root / REPORT_ROOT / "summary.md"
    _write_json(summary_path, summary, exclusive=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    with markdown_path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(_markdown(summary))
    manifest = {
        "files": [
            {
                "path": summary_path.relative_to(root).as_posix(),
                "sha256": file_sha256(summary_path),
            },
            {
                "path": markdown_path.relative_to(root).as_posix(),
                "sha256": file_sha256(markdown_path),
            },
            {"path": REGISTRY_PATH.as_posix(), "sha256": file_sha256(root / REGISTRY_PATH)},
            {
                "path": "scripts/build_walk_forward_report.py",
                "sha256": file_sha256(root / "scripts/build_walk_forward_report.py"),
            },
            {
                "path": "src/alphamind/research/walk_forward.py",
                "sha256": file_sha256(root / "src/alphamind/research/walk_forward.py"),
            },
            {"path": CONFIG_PATH.as_posix(), "sha256": file_sha256(root / CONFIG_PATH)},
            {
                "path": str(summary["dataset_manifest_path"]),
                "sha256": file_sha256(root / str(summary["dataset_manifest_path"])),
            },
        ],
        "model_version": "p2-05-v1",
        "schema_version": 1,
    }
    _write_json(root / REPORT_ROOT / "manifest.json", manifest, exclusive=True)


def _verify_trial_outputs(
    root: Path,
    config: dict[str, Any],
    computed: dict[str, dict[str, object]],
) -> None:
    for trial in _trials(config):
        experiment_id = _experiment_id(_string(config, "registered_at_utc"), trial)
        locate_experiment(root, experiment_id)
        experiment_root = root / "research/experiments" / experiment_id
        expected_trades = _list(computed[trial.trial_id].pop("trades"), "trades")
        actual_trades = _list(
            json.loads((experiment_root / "trades.json").read_text(encoding="utf-8")),
            "trades.json",
        )
        actual_metrics = _mapping(
            json.loads((experiment_root / "metrics.json").read_text(encoding="utf-8")),
            "metrics.json",
        )
        if actual_trades != expected_trades or actual_metrics != computed[trial.trial_id]:
            raise RuntimeError(f"recomputed result differs for {trial.trial_id}")


def build(root: Path) -> dict[str, object]:
    config = _load_config(root)
    folds = _folds(root, config)
    trials = _trials(config)
    registry = _mapping(
        json.loads((root / REGISTRY_PATH).read_text(encoding="utf-8")), "trial registry"
    )
    if registry.get("entries") != []:
        raise RuntimeError("P2-05 build requires an empty trial registry")

    # 所有参数候选必须在读取 clean candle 前一次性登记，避免结果驱动追加 trial。
    for trial in trials:
        register_experiment(root, _registration(root, config, folds, trial))
    computed = _compute(root, config)
    for trial in trials:
        metrics = computed[trial.trial_id]
        outcome, reason_codes = _trial_outcome(metrics)
        experiment_id = _experiment_id(_string(config, "registered_at_utc"), trial)
        finalize_experiment(
            root,
            experiment_id,
            status="REJECTED" if outcome == "FAIL" else "COMPLETED",
            started_at_utc=_string(config, "registered_at_utc"),
            completed_at_utc=_string(config, "completed_at_utc"),
            result={
                "outcome": outcome,
                "primary_metric_value": _mapping(
                    metrics.get("aggregate_validation"), "aggregate_validation"
                ).get("expectancy_r"),
                "production_write_path_verified": False,
                "reason_codes": reason_codes,
            },
            trades=_list(metrics.pop("trades"), "trades"),
            metrics=metrics,
        )
    summary = _summary(root, config, computed)
    _publish_summary(root, summary)
    return {"status": "built", "trial_count": len(trials), "assessment": summary["selection"]}


def resume_report(root: Path) -> dict[str, object]:
    """仅在 trial 已完整落盘而 summary 尚未发布时复核并恢复最终汇总。"""

    for name in ("summary.json", "summary.md", "manifest.json"):
        if (root / REPORT_ROOT / name).exists():
            raise RuntimeError(f"resume requires missing report artifact: {name}")
    config = _load_config(root)
    computed = _compute(root, config)
    _verify_trial_outputs(root, config, computed)
    summary = _summary(root, config, computed)
    _publish_summary(root, summary)
    return {"status": "resumed", "trial_count": len(_trials(config))}


def verify(root: Path) -> dict[str, object]:
    config = _load_config(root)
    trials = _trials(config)
    computed = _compute(root, config)
    _verify_trial_outputs(root, config, computed)
    expected_summary = _summary(root, config, computed)
    actual_summary = _mapping(
        json.loads((root / REPORT_ROOT / "summary.json").read_text(encoding="utf-8")),
        "summary.json",
    )
    if actual_summary != expected_summary or (root / REPORT_ROOT / "summary.md").read_text(
        encoding="utf-8"
    ) != _markdown(expected_summary):
        raise RuntimeError("walk-forward summary differs from recomputed result")
    manifest = _mapping(
        json.loads((root / REPORT_ROOT / "manifest.json").read_text(encoding="utf-8")),
        "manifest.json",
    )
    for raw in _list(manifest.get("files"), "manifest.files"):
        item = _mapping(raw, "manifest.file")
        if file_sha256(root / _string(item, "path")) != _string(item, "sha256"):
            raise RuntimeError(f"manifest hash mismatch: {item['path']}")
    return {"status": "verified", "trial_count": len(trials)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true")
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--resume-report", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    if args.build:
        result = build(root)
    elif args.resume_report:
        result = resume_report(root)
    else:
        result = verify(root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
