"""在锁定的 Freqtrade 容器内核对策略 callback 参数合同。"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

EXPECTED_CALLBACK_PARAMETERS = {
    "bot_start": ("self", "kwargs"),
    "bot_loop_start": ("self", "current_time", "kwargs"),
    "populate_indicators": ("self", "dataframe", "metadata"),
    "populate_entry_trend": ("self", "dataframe", "metadata"),
    "populate_exit_trend": ("self", "dataframe", "metadata"),
    "custom_stake_amount": (
        "self",
        "pair",
        "current_time",
        "current_rate",
        "proposed_stake",
        "min_stake",
        "max_stake",
        "leverage",
        "entry_tag",
        "side",
        "kwargs",
    ),
    "custom_stoploss": (
        "self",
        "pair",
        "trade",
        "current_time",
        "current_rate",
        "current_profit",
        "after_fill",
        "kwargs",
    ),
    "confirm_trade_entry": (
        "self",
        "pair",
        "order_type",
        "amount",
        "rate",
        "time_in_force",
        "current_time",
        "entry_tag",
        "side",
        "kwargs",
    ),
    "order_filled": ("self", "pair", "trade", "order", "current_time", "kwargs"),
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = PROJECT_ROOT / "user_data/strategies/DonchianTrendStrategy.py"
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))


def verify_callback_parameters(strategy_type: Any) -> dict[str, list[str]]:
    """严格比较参数名和顺序，避免后续 adapter 静默使用错误版本接口。"""

    actual: dict[str, list[str]] = {}
    for callback_name, expected_parameters in EXPECTED_CALLBACK_PARAMETERS.items():
        callback = getattr(strategy_type, callback_name)
        parameters = tuple(inspect.signature(callback).parameters)
        if parameters != expected_parameters:
            raise RuntimeError(
                f"Freqtrade callback mismatch for {callback_name}: "
                f"expected {expected_parameters}, got {parameters}"
            )
        actual[callback_name] = list(parameters)
    return actual


def _load_strategy_type(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("alphamind_freqtrade_strategy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load strategy module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DonchianTrendStrategy


def _pure_signal_columns(records: list[dict[str, Any]]) -> tuple[list[int], list[int]]:
    """使用 P2-01 纯函数独立重算每个 dataframe row 的 entry/exit signal。"""

    if str(PROJECT_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
    research = importlib.import_module("alphamind.research.donchian")
    candles = [
        research.Candle(
            timestamp=record["date"].to_pydatetime(),
            open=Decimal(str(record["open"])),
            high=Decimal(str(record["high"])),
            low=Decimal(str(record["low"])),
            close=Decimal(str(record["close"])),
            volume=Decimal(str(record["volume"])),
        )
        for record in records
    ]
    parameters = research.DonchianParameters(
        entry_window=20,
        exit_window=10,
        expected_interval=timedelta(hours=4),
    )
    entry: list[int] = []
    exit_signal: list[int] = []
    for index in range(len(candles)):
        visible = candles[: index + 1]
        entry_decision = research.evaluate_donchian(
            visible,
            parameters,
            in_position=False,
        )
        exit_decision = research.evaluate_donchian(
            visible,
            parameters,
            in_position=True,
        )
        entry.append(int(entry_decision.signal is research.DonchianSignal.OPEN_LONG))
        exit_signal.append(int(exit_decision.signal is research.DonchianSignal.CLOSE_LONG))
    return entry, exit_signal


def _run_adapter(strategy: Any, dataframe: Any) -> Any:
    metadata = {"pair": "BTC/USDT"}
    analyzed = strategy.populate_indicators(dataframe.copy(), metadata)
    analyzed = strategy.populate_entry_trend(analyzed, metadata)
    return strategy.populate_exit_trend(analyzed, metadata)


def verify_strategy_adapter(strategy_type: Any) -> dict[str, object]:
    """在锁定镜像中比较 adapter dataframe 信号与 P2-01 纯函数。"""

    expected_settings = {
        "INTERFACE_VERSION": 3,
        "can_short": False,
        "exit_profit_only": False,
        "ignore_roi_if_entry_signal": False,
        "minimal_roi": {},
        "order_types": {
            "entry": "market",
            "exit": "market",
            "emergency_exit": "market",
            "force_entry": "market",
            "force_exit": "market",
            "stoploss": "market",
            "stoploss_on_exchange": False,
        },
        "position_adjustment_enable": False,
        "protections": [{"method": "CooldownPeriod", "stop_duration_candles": 1}],
        "process_only_new_candles": True,
        "startup_candle_count": 120,
        "stoploss": -0.99,
        "timeframe": "4h",
        "trailing_stop": False,
        "use_custom_stoploss": True,
        "use_exit_signal": True,
    }
    actual_settings = {name: getattr(strategy_type, name) for name in expected_settings}
    if actual_settings != expected_settings:
        raise RuntimeError(
            "Freqtrade strategy settings mismatch: "
            f"expected {expected_settings}, got {actual_settings}"
        )
    verify_callback_parameters(strategy_type)
    source = STRATEGY_PATH.read_text(encoding="utf-8")
    if ".iloc[" in source or "shift(-" in source:
        raise RuntimeError("strategy contains a forbidden future/absolute dataframe access pattern")

    pandas = importlib.import_module("pandas")
    rows = [
        {
            "close": 100,
            "date": timestamp,
            "high": 110,
            "low": 90,
            "open": 100,
            "volume": 1,
        }
        for timestamp in pandas.date_range(
            "2026-01-01T00:00:00Z",
            periods=25,
            freq="4h",
        )
    ]
    rows[20].update({"close": 120, "high": 121, "low": 100, "open": 100})
    rows[24].update({"close": 80, "high": 85, "low": 75, "open": 82})

    strategy = strategy_type({})
    analyzed = _run_adapter(strategy, pandas.DataFrame(rows))
    expected_entry, expected_exit = _pure_signal_columns(rows)
    actual_entry = [int(value) for value in analyzed["enter_long"].tolist()]
    actual_exit = [int(value) for value in analyzed["exit_long"].tolist()]
    if actual_entry != expected_entry or actual_exit != expected_exit:
        raise RuntimeError("Freqtrade dataframe signals differ from P2-01 pure function")
    if analyzed.at[20, "donchian_entry_high"] != 110:
        raise RuntimeError("signal candle leaked into the entry threshold")
    if abs(float(analyzed.at[20, "donchian_signal_atr"]) - 20.05) > 1e-12:
        raise RuntimeError("Wilder ATR differs from the frozen P2-05 recurrence")
    if analyzed.at[20, "enter_tag"] != "entry_breakout":
        raise RuntimeError("entry tag mismatch")
    if analyzed.at[24, "exit_tag"] != "exit_breakout":
        raise RuntimeError("exit tag mismatch")

    string_rows = [
        {
            name: str(value) if name in {"open", "high", "low", "close", "volume"} else value
            for name, value in row.items()
        }
        for row in rows
    ]
    string_analyzed = _run_adapter(strategy, pandas.DataFrame(string_rows))
    if [int(value) for value in string_analyzed["enter_long"].tolist()] != expected_entry:
        raise RuntimeError("numeric string entry behavior differs from normalized OHLCV")
    if [int(value) for value in string_analyzed["exit_long"].tolist()] != expected_exit:
        raise RuntimeError("numeric string exit behavior differs from normalized OHLCV")

    invalid_rows = [dict(row) for row in rows]
    invalid_rows[20]["high"] = 119
    invalid_analyzed = _run_adapter(strategy, pandas.DataFrame(invalid_rows))
    if int(invalid_analyzed.at[20, "enter_long"]) != 0:
        raise RuntimeError("invalid OHLC candle must fail closed")

    unclosed_rows = [dict(row) for row in rows]
    for row in unclosed_rows:
        row["is_closed"] = True
    unclosed_rows[20]["is_closed"] = False
    unclosed_analyzed = _run_adapter(strategy, pandas.DataFrame(unclosed_rows))
    if int(unclosed_analyzed.at[20, "enter_long"]) != 0:
        raise RuntimeError("unclosed signal candle must fail closed")

    gap_rows = rows[:10] + rows[11:]
    gap_analyzed = _run_adapter(strategy, pandas.DataFrame(gap_rows))
    gap_entry, gap_exit = _pure_signal_columns(gap_rows)
    if [int(value) for value in gap_analyzed["enter_long"].tolist()] != gap_entry:
        raise RuntimeError("gap entry behavior differs from P2-01 pure function")
    if [int(value) for value in gap_analyzed["exit_long"].tolist()] != gap_exit:
        raise RuntimeError("gap exit behavior differs from P2-01 pure function")

    if strategy.confirm_trade_entry(
        "BTC/USDT",
        "limit",
        1.0,
        100.0,
        "GTC",
        datetime(2026, 1, 1, tzinfo=UTC),
        "entry_breakout",
        "long",
    ):
        raise RuntimeError("P3-02 must reject execution without a cached risk approval")
    return {
        "entry_signal_rows": [index for index, value in enumerate(actual_entry) if value],
        "exit_signal_rows": [index for index, value in enumerate(actual_exit) if value],
        "execution_requires_cached_risk_approval": True,
        "settings": actual_settings,
        "strategy_version": strategy.version(),
    }


def verify_risk_callbacks(strategy_type: Any) -> dict[str, object]:
    """在锁定镜像中执行定仓、最终确认、fill 持久化与固定止损 callback。"""

    risk = importlib.import_module("alphamind.risk")
    risk_limits = importlib.import_module("alphamind.config.risk_limits")
    pandas = importlib.import_module("pandas")
    strategy = strategy_type({})
    adapter_config = risk.load_freqtrade_risk_config(
        PROJECT_ROOT / "configs/common/freqtrade-risk-adapter.toml"
    )
    snapshot_path = Path("/tmp/alphamind-contract-risk-snapshot.json")
    generated_at = datetime(2026, 7, 17, 12, tzinfo=UTC)
    observation = risk.WatchdogObservation(
        generated_at_utc=generated_at,
        market_observed_at_utc=generated_at - timedelta(seconds=2),
        market_complete=True,
        account=risk.AccountRuntimeObservation(
            account_id="contract-paper",
            accounting_currency="USDT",
            observed_at_utc=generated_at - timedelta(seconds=3),
            quote_cash=Decimal("500"),
            available_balance_quote=Decimal("500"),
            positions=(),
            accrued_fees=Decimal("0"),
            known_liabilities=Decimal("0"),
            unexplained_balance_difference=Decimal("0"),
            pending_entry_exposure_quote=Decimal("0"),
            account_complete=True,
            runtime_reconciled=True,
        ),
        accounting_state=risk.RiskAccountingState(
            approved_capital_baseline=Decimal("500"),
            cumulative_external_cash_flow_before=Decimal("0"),
            daily_external_cash_flow_before=Decimal("0"),
            weekly_external_cash_flow_before=Decimal("0"),
            cashflow_adjusted_high_water_mark_before=Decimal("500"),
            daily_boundary=risk.PeriodBoundary(
                observed_at_utc=generated_at.replace(hour=0), opening_nav=Decimal("500")
            ),
            weekly_boundary=risk.PeriodBoundary(
                observed_at_utc=datetime(2026, 7, 13, tzinfo=UTC),
                opening_nav=Decimal("500"),
            ),
            external_cash_flow_review_pending=False,
        ),
    )
    limits_path = PROJECT_ROOT / "configs/common/risk-limits.toml"
    payload = risk.build_risk_snapshot(
        observation,
        risk_limits.load_risk_limits(limits_path),
        risk_config_sha256=risk.risk_config_sha256(limits_path),
        producer_version="0.2.0",
    )
    risk.atomic_publish_snapshot(payload, snapshot_path)
    strategy._risk_config = replace(adapter_config, snapshot_path=snapshot_path)
    current_time = generated_at + timedelta(seconds=10)
    strategy.bot_loop_start(current_time=current_time)
    if strategy._risk_snapshot is None or not strategy._risk_snapshot.entry_allowed:
        raise RuntimeError("bot_loop_start did not load the valid atomic RiskSnapshot")

    class FakeDataProvider:
        def get_analyzed_dataframe(self, *, pair: str, timeframe: str) -> tuple[Any, None]:
            if pair != "ETH/USDT" or timeframe != "4h":
                raise RuntimeError("unexpected callback market context")
            return (
                pandas.DataFrame(
                    [
                        {
                            "donchian_data_valid": True,
                            "donchian_signal_atr": 2.5,
                            "enter_long": 1,
                            "enter_tag": strategy.ENTRY_TAG,
                        }
                    ]
                ),
                None,
            )

    strategy.dp = FakeDataProvider()
    stake = strategy.custom_stake_amount(
        "ETH/USDT",
        current_time,
        100.0,
        300.0,
        5.0,
        300.0,
        1.0,
        strategy.ENTRY_TAG,
        "long",
    )
    approval = strategy._entry_approvals.get("ETH/USDT")
    if approval is None or stake != float(approval.approved_stake):
        raise RuntimeError("runtime risk sizing did not create the expected cached approval")
    if not strategy.confirm_trade_entry(
        "ETH/USDT",
        "market",
        float(approval.approved_quantity),
        100.0,
        "GTC",
        current_time,
        strategy.ENTRY_TAG,
        "long",
    ):
        raise RuntimeError("cached risk approval was rejected")
    if strategy.confirm_trade_entry(
        "ETH/USDT",
        "market",
        float(approval.approved_quantity + Decimal("0.00001")),
        100.0,
        "GTC",
        current_time,
        strategy.ENTRY_TAG,
        "long",
    ):
        raise RuntimeError("confirm_trade_entry enlarged the approved quantity")

    class FakeTrade:
        pair = "ETH/USDT"
        entry_side = "buy"
        exit_side = "sell"
        is_short = False
        open_rate = 100.0

        def __init__(self) -> None:
            self.data: dict[str, object] = {}

        def set_custom_data(self, key: str, value: object) -> None:
            self.data[key] = value

        def get_custom_data(self, key: str) -> object:
            return self.data.get(key)

    trade = FakeTrade()
    order = type("FakeOrder", (), {"ft_order_side": "buy"})()
    strategy.order_filled("ETH/USDT", trade, order, current_time)
    if trade.data.get(strategy.INITIAL_STOP_CUSTOM_DATA_KEY) != "95.00":
        raise RuntimeError("entry fill did not persist the ATR initial stop")
    stoploss = strategy.custom_stoploss("ETH/USDT", trade, current_time, 100.0, 0.0, True)
    if stoploss is None or abs(stoploss - 0.05) > 1e-12:
        raise RuntimeError("custom_stoploss did not preserve the fixed absolute stop")
    if not strategy.confirm_trade_exit(
        "ETH/USDT",
        trade,
        "market",
        1.0,
        100.0,
        "GTC",
        "exit_signal",
        current_time,
    ):
        raise RuntimeError("risk snapshot state must not block safe exits")

    strategy.dp = object()
    failed_closed_stake = strategy.custom_stake_amount(
        "BTC/USDT",
        current_time,
        100.0,
        250.0,
        5.0,
        300.0,
        1.0,
        strategy.ENTRY_TAG,
        "long",
    )
    if failed_closed_stake != 0.0:
        raise RuntimeError("callback error must return zero instead of proposed stake")
    return {
        "approved_quantity": str(approval.approved_quantity),
        "approved_stake": str(approval.approved_stake),
        "initial_stop": trade.data[strategy.INITIAL_STOP_CUSTOM_DATA_KEY],
        "safe_exit_allowed": True,
    }


def verify_backtest_fill_contract(backtesting_module: Any) -> dict[str, bool]:
    """实测锁定版本只把 candle high/low 内的请求价格视为可成交。"""

    row: list[Any] = [None] * (max(backtesting_module.LOW_IDX, backtesting_module.HIGH_IDX) + 1)
    row[backtesting_module.LOW_IDX] = 95.0
    row[backtesting_module.HIGH_IDX] = 105.0
    callback = backtesting_module.Backtesting._get_order_filled
    within_range = callback(None, 100.0, tuple(row))
    below_range = callback(None, 90.0, tuple(row))
    above_range = callback(None, 110.0, tuple(row))
    if within_range is not True or below_range is not False or above_range is not False:
        raise RuntimeError("Freqtrade backtest candle-touch fill contract changed")
    return {
        "within_candle_range_fills": within_range,
        "below_candle_range_fills": below_range,
        "above_candle_range_fills": above_range,
    }


def main() -> int:
    interface = importlib.import_module("freqtrade.strategy.interface")
    actual = verify_callback_parameters(interface.IStrategy)
    backtesting = importlib.import_module("freqtrade.optimize.backtesting")
    fill_contract = verify_backtest_fill_contract(backtesting)
    strategy_type = _load_strategy_type(STRATEGY_PATH)
    strategy = verify_strategy_adapter(strategy_type)
    risk_callbacks = verify_risk_callbacks(strategy_type)
    print(
        json.dumps(
            {
                "status": "ok",
                "backtest_fill_contract": fill_contract,
                "callbacks": actual,
                "risk_callbacks": risk_callbacks,
                "strategy": strategy,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
