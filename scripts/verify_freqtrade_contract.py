"""在锁定的 Freqtrade 容器内核对策略 callback 参数合同。"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

EXPECTED_CALLBACK_PARAMETERS = {
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
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = PROJECT_ROOT / "user_data/strategies/DonchianTrendStrategy.py"


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
        "position_adjustment_enable": False,
        "process_only_new_candles": True,
        "startup_candle_count": 120,
        "stoploss": -0.99,
        "timeframe": "4h",
        "trailing_stop": False,
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
        raise RuntimeError("P2-02 must reject execution before P3-02 risk callbacks")
    return {
        "entry_signal_rows": [index for index, value in enumerate(actual_entry) if value],
        "exit_signal_rows": [index for index, value in enumerate(actual_exit) if value],
        "execution_enabled": False,
        "settings": actual_settings,
        "strategy_version": strategy.version(),
    }


def main() -> int:
    interface = importlib.import_module("freqtrade.strategy.interface")
    actual = verify_callback_parameters(interface.IStrategy)
    strategy_type = _load_strategy_type(STRATEGY_PATH)
    strategy = verify_strategy_adapter(strategy_type)
    print(
        json.dumps(
            {"status": "ok", "callbacks": actual, "strategy": strategy},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
