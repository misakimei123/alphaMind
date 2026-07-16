"""在锁定的 Freqtrade 容器内核对策略 callback 参数合同。"""

from __future__ import annotations

import importlib
import inspect
import json
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


def main() -> int:
    interface = importlib.import_module("freqtrade.strategy.interface")
    actual = verify_callback_parameters(interface.IStrategy)
    print(json.dumps({"status": "ok", "callbacks": actual}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
