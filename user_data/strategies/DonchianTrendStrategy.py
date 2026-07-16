"""P2-02 Freqtrade 2026.6 Donchian 信号 adapter。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy
from pandas import DataFrame, Series


class DonchianTrendStrategy(IStrategy):
    """把冻结的 Donchian 20/10 纯信号映射到 Freqtrade dataframe。"""

    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    startup_candle_count = 120
    process_only_new_candles = True

    minimal_roi: ClassVar[dict[str, float]] = {}
    stoploss = -0.99
    trailing_stop = False
    position_adjustment_enable = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    ENTRY_WINDOW = 20
    EXIT_WINDOW = 10
    ACTIVE_WINDOW = max(ENTRY_WINDOW, EXIT_WINDOW)
    ENTRY_TAG = "entry_breakout"
    EXIT_TAG = "exit_breakout"

    def version(self) -> str:
        return "0.1.0"

    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        """计算严格滞后一根的 channel，并冻结 active window 数据健康状态。"""

        del metadata
        required_columns = ("open", "high", "low", "close", "volume")
        numeric = dataframe.loc[:, required_columns].apply(pd.to_numeric, errors="coerce")
        finite = Series(
            np.isfinite(numeric.to_numpy()).all(axis=1),
            index=dataframe.index,
            dtype=bool,
        )
        price_valid = numeric.loc[:, ("open", "high", "low", "close")].gt(0).all(axis=1)
        volume_valid = numeric["volume"].ge(0)
        high_valid = numeric["high"].ge(numeric.loc[:, ("open", "low", "close")].max(axis=1))
        low_valid = numeric["low"].le(numeric.loc[:, ("open", "high", "close")].min(axis=1))
        if "is_closed" in dataframe.columns:
            closed = dataframe["is_closed"].eq(True)
        else:
            # Freqtrade populate_* 只提供已完成 candle；测试 fixture 可显式传 is_closed。
            closed = Series(True, index=dataframe.index, dtype=bool)
        row_valid = finite & price_valid & volume_valid & high_valid & low_valid & closed

        dates = pd.to_datetime(dataframe["date"], errors="coerce", utc=True)
        interval_valid = dates.diff().eq(pd.Timedelta(self.timeframe))
        contiguous_window = (
            interval_valid.rolling(self.ACTIVE_WINDOW, min_periods=self.ACTIVE_WINDOW)
            .sum()
            .eq(self.ACTIVE_WINDOW)
        )
        valid_row_window = (
            row_valid.rolling(self.ACTIVE_WINDOW + 1, min_periods=self.ACTIVE_WINDOW + 1)
            .sum()
            .eq(self.ACTIVE_WINDOW + 1)
        )

        # shift(1) 是 point-in-time 边界：signal candle 不能抬高或压低自身阈值。
        dataframe["donchian_entry_high"] = (
            numeric["high"].rolling(self.ENTRY_WINDOW, min_periods=self.ENTRY_WINDOW).max().shift(1)
        )
        dataframe["donchian_exit_low"] = (
            numeric["low"].rolling(self.EXIT_WINDOW, min_periods=self.EXIT_WINDOW).min().shift(1)
        )
        # 信号比较统一使用已归一化数值，避免可转换字符串通过校验后触发类型比较异常。
        dataframe["donchian_close"] = numeric["close"]
        dataframe["donchian_data_valid"] = valid_row_window & contiguous_window & dates.notna()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        del metadata
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        entry = dataframe["donchian_data_valid"].eq(True) & dataframe["donchian_close"].gt(
            dataframe["donchian_entry_high"]
        )
        dataframe.loc[entry, ["enter_long", "enter_tag"]] = (1, self.ENTRY_TAG)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        del metadata
        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        exit_signal = dataframe["donchian_data_valid"].eq(True) & dataframe["donchian_close"].lt(
            dataframe["donchian_exit_low"]
        )
        dataframe.loc[exit_signal, ["exit_long", "exit_tag"]] = (1, self.EXIT_TAG)
        return dataframe

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        """P3-02 风险快照接入前固定拒绝执行，P2-02 只验证信号映射。"""

        del pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side, kwargs
        return False
