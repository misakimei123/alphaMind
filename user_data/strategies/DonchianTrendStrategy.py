"""P3-02 Freqtrade 2026.6 Donchian 信号与风险 callback adapter。"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy
from pandas import DataFrame, Series

from alphamind.audit import (
    AuditExecutionContext,
    AuditOutbox,
    AuditRuntimeConfig,
    build_risk_decision_event,
    load_audit_runtime_config,
)
from alphamind.risk.freqtrade_adapter import (
    FreqtradeRiskConfig,
    RuntimeEntryApproval,
    calculate_initial_stop_price,
    calculate_runtime_entry_approval,
    fixed_stoploss_ratio,
    load_freqtrade_risk_config,
)
from alphamind.risk.watchdog import SnapshotReadResult, load_risk_snapshot

logger = logging.getLogger(__name__)
ZERO = Decimal("0")


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
    use_custom_stoploss = True
    position_adjustment_enable = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    order_types: ClassVar[dict[str, str | bool]] = {
        "entry": "market",
        "exit": "market",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    # Freqtrade 本身会在退出后锁定一根 candle；显式 protection 固化同一冷却语义。
    protections: ClassVar[list[dict[str, str | int]]] = [
        {"method": "CooldownPeriod", "stop_duration_candles": 1}
    ]

    ENTRY_WINDOW = 20
    EXIT_WINDOW = 10
    ATR_PERIOD = 20
    ACTIVE_WINDOW = max(ENTRY_WINDOW, EXIT_WINDOW)
    ENTRY_TAG = "entry_breakout"
    EXIT_TAG = "exit_breakout"
    RISK_ADAPTER_CONFIG_PATH = "/freqtrade/common/freqtrade-risk-adapter.toml"
    AUDIT_CONFIG_PATH = "/freqtrade/common/audit-outbox.toml"
    INITIAL_STOP_CUSTOM_DATA_KEY = "alphamind_initial_stop"
    SIGNAL_ATR_CUSTOM_DATA_KEY = "alphamind_signal_atr"
    SNAPSHOT_ID_CUSTOM_DATA_KEY = "alphamind_risk_snapshot_id"
    EMERGENCY_STOPLOSS_RATIO = Decimal("0.001")

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._risk_config: FreqtradeRiskConfig | None = None
        self._risk_snapshot: SnapshotReadResult | None = None
        self._entry_approvals: dict[str, RuntimeEntryApproval] = {}
        self._audit_config: AuditRuntimeConfig | None = None
        self._audit_outbox: AuditOutbox | None = None
        self._audit_sequence = 0

    def version(self) -> str:
        return "0.3.0"

    def _audit_execution_context(self) -> AuditExecutionContext:
        """把锁定 Freqtrade runmode 映射为 schema v1 evidence layer。"""

        runmode = self.config.get("runmode", "dry_run")
        mode = str(getattr(runmode, "value", runmode))
        contexts = {
            "backtest": AuditExecutionContext(
                "backtest", "historical_backtest", "none", False, False
            ),
            "dry_run": AuditExecutionContext(
                "dry_run", "freqtrade_dry_run", "dry_run", False, False
            ),
            "live": AuditExecutionContext(
                "live_canary", "live_canary", "production_trade", True, False
            ),
        }
        if mode not in contexts:
            raise ValueError(f"unsupported audit runmode: {mode}")
        return contexts[mode]

    def bot_start(self, **kwargs: Any) -> None:
        """启动时一次性加载适配配置；配置错误必须阻止 bot 进入可交易状态。"""

        del kwargs
        config_path = os.environ.get(
            "ALPHAMIND_RISK_ADAPTER_CONFIG_PATH", self.RISK_ADAPTER_CONFIG_PATH
        )
        audit_config_path = os.environ.get("ALPHAMIND_AUDIT_CONFIG_PATH", self.AUDIT_CONFIG_PATH)
        self._risk_config = load_freqtrade_risk_config(config_path)
        self._audit_config = load_audit_runtime_config(audit_config_path)
        self._audit_execution_context()
        if self._audit_outbox is not None:
            self._audit_outbox.close()
        self._audit_outbox = AuditOutbox(self._audit_config.storage.outbox_path)
        self._audit_sequence = self._audit_outbox.next_sequence(
            producer_component="freqtrade_strategy",
            producer_instance_id=self._audit_config.storage.producer_instance_id,
        )
        self._risk_snapshot = None
        self._entry_approvals.clear()

    def bot_loop_start(self, current_time: datetime, **kwargs: Any) -> None:
        """在非下单关键位置读取并严格验证原子 RiskSnapshot。"""

        del kwargs
        if self._risk_config is None:
            self._risk_snapshot = None
            self._entry_approvals.clear()
            return
        previous_id = None
        if self._risk_snapshot is not None and self._risk_snapshot.snapshot is not None:
            previous_id = self._risk_snapshot.snapshot.get("snapshot_id")
        snapshot = load_risk_snapshot(self._risk_config.snapshot_path, now_utc=current_time)
        current_id = snapshot.snapshot.get("snapshot_id") if snapshot.snapshot is not None else None
        if current_id != previous_id or not snapshot.entry_allowed:
            # 新快照必须重新定仓；旧批准不能跨 snapshot 或 fail-closed 状态复用。
            self._entry_approvals.clear()
        self._risk_snapshot = snapshot

    @classmethod
    def _wilder_atr(cls, true_range: Series, row_valid: Series) -> Series:
        """使用简单均值种子和 Wilder 递推，遇到无效输入后重新预热。"""

        values = np.full(len(true_range), np.nan, dtype=float)
        seed: list[float] = []
        atr: float | None = None
        for index, (raw_range, valid) in enumerate(zip(true_range, row_valid, strict=True)):
            if not bool(valid) or not np.isfinite(raw_range):
                seed.clear()
                atr = None
                continue
            current_range = float(raw_range)
            if atr is None:
                seed.append(current_range)
                if len(seed) == cls.ATR_PERIOD:
                    atr = float(np.mean(seed))
                    values[index] = atr
                continue
            atr = (atr * (cls.ATR_PERIOD - 1) + current_range) / cls.ATR_PERIOD
            values[index] = atr
        return Series(values, index=true_range.index, dtype=float)

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

        previous_close = numeric["close"].shift(1)
        true_range = pd.concat(
            (
                numeric["high"] - numeric["low"],
                (numeric["high"] - previous_close).abs(),
                (numeric["low"] - previous_close).abs(),
            ),
            axis=1,
        ).max(axis=1)
        # ATR 的首行允许 high-low；其余行要求当前与前一行都有效，避免跨数据缺口递推。
        atr_input_valid = row_valid & row_valid.shift(1, fill_value=True)
        dataframe["donchian_signal_atr"] = self._wilder_atr(true_range, atr_input_valid)

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

    def _latest_signal_atr(self, pair: str, entry_tag: str | None) -> Decimal | None:
        """只读取 Freqtrade 已分析的最新已完成 candle，不在关键路径重新计算指标。"""

        if entry_tag != self.ENTRY_TAG or not hasattr(self, "dp"):
            return None
        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        latest = dataframe.tail(1)
        if latest.empty:
            return None
        row = latest.squeeze(axis=0)
        if (
            not bool(row.get("donchian_data_valid", False))
            or int(row.get("enter_long", 0)) != 1
            or row.get("enter_tag") != self.ENTRY_TAG
        ):
            return None
        try:
            atr = Decimal(str(row["donchian_signal_atr"]))
        except (InvalidOperation, KeyError):
            return None
        return atr if atr.is_finite() and atr > 0 else None

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        """用 P2-03 同源纯函数计算 quote stake；任何异常都显式返回 0。"""

        del proposed_stake, kwargs
        try:
            if (
                self._risk_config is None
                or self._risk_snapshot is None
                or self._audit_config is None
                or self._audit_outbox is None
                or side != "long"
                or leverage != 1.0
            ):
                return 0.0
            # 先检查冻结背压阈值，避免在审计不可持续时继续增加新风险。
            if self._audit_outbox.metrics(now=current_time).entry_backpressure:
                self._entry_approvals.pop(pair, None)
                return 0.0
            signal_atr = self._latest_signal_atr(pair, entry_tag)
            if signal_atr is None:
                return 0.0
            approval = calculate_runtime_entry_approval(
                self._risk_snapshot,
                self._risk_config,
                pair=pair,
                current_rate=Decimal(str(current_rate)),
                signal_atr=signal_atr,
                min_stake=Decimal(str(min_stake)) if min_stake is not None else None,
                max_stake=Decimal(str(max_stake)),
            )
            if approval is None:
                self._entry_approvals.pop(pair, None)
                return 0.0
            sequence = self._audit_sequence
            self._audit_sequence += 1
            event = build_risk_decision_event(
                event_id=uuid.uuid4(),
                producer_instance_id=self._audit_config.storage.producer_instance_id,
                producer_sequence=sequence,
                occurred_at=current_time,
                recorded_at=datetime.now(UTC),
                execution_context=self._audit_execution_context(),
                provenance=self._audit_config.provenance,
                risk_snapshot_id=approval.snapshot_id,
                pair=pair,
                approved_quantity=approval.approved_quantity,
                approved_stake=approval.approved_stake,
                reference_rate=approval.reference_rate,
                limiting_cap=approval.position_decision.limiting_cap.value,
            )
            # 非零 stake 只能在风险批准事实已经 durable append 后返回。
            self._audit_outbox.append(event, event_class="ENTRY", now=current_time)
            self._entry_approvals[pair] = approval
            return float(approval.approved_stake)
        except Exception:
            # Freqtrade 会在 callback 抛错时回退 proposed stake；风险边界必须主动吞错并拒绝。
            logger.exception("Risk sizing failed closed for %s", pair)
            self._entry_approvals.pop(pair, None)
            return 0.0

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
        """常数时间复核已审计的缓存批准；该 callback 不读文件、网络或数据库。"""

        del time_in_force, kwargs
        approval = self._entry_approvals.get(pair)
        snapshot = self._risk_snapshot
        if (
            approval is None
            or snapshot is None
            or not snapshot.entry_allowed
            or snapshot.snapshot is None
            or side != "long"
            or entry_tag != self.ENTRY_TAG
            or order_type != self.order_types["entry"]
            or current_time >= approval.expires_at_utc
            or snapshot.snapshot.get("snapshot_id") != approval.snapshot_id
        ):
            return False
        try:
            requested_amount = Decimal(str(amount))
            requested_rate = Decimal(str(rate))
        except InvalidOperation:
            return False
        return (
            requested_amount.is_finite()
            and requested_rate.is_finite()
            and ZERO < requested_amount <= approval.approved_quantity
            and requested_rate == approval.reference_rate
        )

    def order_filled(
        self,
        pair: str,
        trade: Any,
        order: Any,
        current_time: datetime,
        **kwargs: Any,
    ) -> None:
        """entry fill 后持久化固定绝对止损，供重启后的 custom_stoploss 恢复。"""

        del current_time, kwargs
        approval = self._entry_approvals.get(pair)
        if (
            approval is None
            or self._risk_config is None
            or getattr(order, "ft_order_side", None) != getattr(trade, "entry_side", None)
        ):
            return
        try:
            average_entry_rate = Decimal(str(trade.open_rate))
        except (AttributeError, InvalidOperation):
            return
        initial_stop = calculate_initial_stop_price(
            self._risk_config,
            pair=pair,
            average_entry_rate=average_entry_rate,
            signal_atr=approval.signal_atr,
        )
        if initial_stop is None:
            return
        # Trade custom data 由 Freqtrade Runtime DB 持久化，不创建第二订单或持仓真相。
        trade.set_custom_data(self.INITIAL_STOP_CUSTOM_DATA_KEY, str(initial_stop))
        trade.set_custom_data(self.SIGNAL_ATR_CUSTOM_DATA_KEY, str(approval.signal_atr))
        trade.set_custom_data(self.SNAPSHOT_ID_CUSTOM_DATA_KEY, approval.snapshot_id)
        self._entry_approvals.pop(pair, None)

    def custom_stoploss(
        self,
        pair: str,
        trade: Any,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: Any,
    ) -> float | None:
        """维持实际平均成交价减 2 ATR 的固定 bot-managed stoploss。"""

        del current_time, current_profit, after_fill, kwargs
        if pair != getattr(trade, "pair", pair) or bool(getattr(trade, "is_short", False)):
            return float(self.EMERGENCY_STOPLOSS_RATIO)
        try:
            initial_stop = Decimal(str(trade.get_custom_data(self.INITIAL_STOP_CUSTOM_DATA_KEY)))
            current = Decimal(str(current_rate))
        except (AttributeError, InvalidOperation):
            return float(self.EMERGENCY_STOPLOSS_RATIO)
        ratio = fixed_stoploss_ratio(initial_stop_price=initial_stop, current_rate=current)
        if ratio is None or ratio <= ZERO:
            # 缺失/损坏持久化数据或价格已穿透 stop 时收紧到当前价附近，禁止保留 -99%。
            return float(self.EMERGENCY_STOPLOSS_RATIO)
        return float(ratio)
