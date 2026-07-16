"""P2-05 确定性 Walk-Forward 回测与统计校正纯函数。"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import pairwise
from statistics import NormalDist

from alphamind.risk.position_sizing import (
    PositionSizeContext,
    PositionSizeRequest,
    RiskContextSource,
    calculate_position_size,
)

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class MarketBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        for name in ("open", "high", "low", "close", "volume"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if min(self.open, self.high, self.low, self.close) <= ZERO:
            raise ValueError("OHLC prices must be positive")
        if self.volume < ZERO:
            raise ValueError("volume must not be negative")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must contain OHLC")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must contain OHLC")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_id: str
    train_start: datetime
    train_end_exclusive: datetime
    validation_start: datetime
    validation_end_exclusive: datetime

    def __post_init__(self) -> None:
        for name in (
            "train_start",
            "train_end_exclusive",
            "validation_start",
            "validation_end_exclusive",
        ):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be timezone-aware UTC")
        if not self.fold_id:
            raise ValueError("fold_id must not be empty")
        if not self.train_start < self.train_end_exclusive <= self.validation_start:
            raise ValueError("train must end before validation starts")
        if self.validation_start >= self.validation_end_exclusive:
            raise ValueError("validation range must not be empty")


@dataclass(frozen=True, slots=True)
class DonchianTrial:
    trial_index: int
    trial_id: str
    entry_window: int
    exit_window: int
    atr_period: int
    stop_multiple: Decimal
    changed_parameter: str | None = None

    def __post_init__(self) -> None:
        if type(self.trial_index) is not int or self.trial_index <= 0:
            raise ValueError("trial_index must be a positive integer")
        if not self.trial_id:
            raise ValueError("trial_id must not be empty")
        if min(self.entry_window, self.exit_window, self.atr_period) <= 0:
            raise ValueError("indicator periods must be positive")
        if not isinstance(self.stop_multiple, Decimal):
            raise TypeError("stop_multiple must be Decimal")
        if not self.stop_multiple.is_finite() or self.stop_multiple <= ZERO:
            raise ValueError("stop_multiple must be finite and positive")


@dataclass(frozen=True, slots=True)
class CostAssumptions:
    fee_rate_per_side: Decimal
    half_spread_rate: Decimal
    slippage_rate_per_side: Decimal
    gap_buffer_rate: Decimal

    def __post_init__(self) -> None:
        for name in (
            "fee_rate_per_side",
            "half_spread_rate",
            "slippage_rate_per_side",
            "gap_buffer_rate",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.per_side_rate >= ONE or self.gap_buffer_rate >= ONE:
            raise ValueError("cost and gap rates must be below 100%")

    @property
    def per_side_rate(self) -> Decimal:
        return self.fee_rate_per_side + self.half_spread_rate + self.slippage_rate_per_side


@dataclass(frozen=True, slots=True)
class MarketConstraints:
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal

    def __post_init__(self) -> None:
        for name in ("price_tick", "quantity_step", "minimum_quantity", "minimum_notional"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.price_tick <= ZERO or self.quantity_step <= ZERO:
            raise ValueError("price_tick and quantity_step must be positive")


@dataclass(frozen=True, slots=True)
class BacktestSettings:
    initial_equity: Decimal
    risk_fraction: Decimal
    symbol_exposure_fraction: Decimal
    directional_exposure_fraction: Decimal
    volatility_cap_fraction: Decimal
    maximum_unit_loss_fraction: Decimal
    event_cluster_hours: int

    def __post_init__(self) -> None:
        for name in (
            "initial_equity",
            "risk_fraction",
            "symbol_exposure_fraction",
            "directional_exposure_fraction",
            "volatility_cap_fraction",
            "maximum_unit_loss_fraction",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be finite and positive")
        if self.initial_equity <= ONE:
            raise ValueError("initial_equity must be greater than one quote unit")
        if any(
            value > ONE
            for value in (
                self.risk_fraction,
                self.symbol_exposure_fraction,
                self.directional_exposure_fraction,
                self.volatility_cap_fraction,
                self.maximum_unit_loss_fraction,
            )
        ):
            raise ValueError("risk and exposure fractions must not exceed one")
        if type(self.event_cluster_hours) is not int or self.event_cluster_hours <= 0:
            raise ValueError("event_cluster_hours must be a positive integer")


@dataclass(frozen=True, slots=True)
class TradeRecord:
    pair: str
    entry_signal_timestamp: datetime
    entry_timestamp: datetime
    exit_timestamp: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    net_pnl_quote: Decimal
    return_r: Decimal
    exit_reason: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    initial_equity: Decimal
    final_equity: Decimal
    net_return: Decimal
    maximum_drawdown: Decimal
    trades: tuple[TradeRecord, ...]
    independent_event_count: int
    expectancy_r: Decimal | None
    period_returns: tuple[float, ...]


@dataclass(slots=True)
class _Position:
    quantity: Decimal
    entry_price: Decimal
    entry_cash_out: Decimal
    entry_risk_cash: Decimal
    stop_price: Decimal
    entry_signal_timestamp: datetime
    entry_timestamp: datetime


def validate_expanding_folds(folds: tuple[WalkForwardFold, ...]) -> None:
    """验证 train 只扩张、validation 连续且绝不随机重叠。"""

    if not folds:
        raise ValueError("walk-forward folds must not be empty")
    first_train_start = folds[0].train_start
    previous_validation_end: datetime | None = None
    previous_train_end: datetime | None = None
    for fold in folds:
        if fold.train_start != first_train_start:
            raise ValueError("expanding folds must share one train start")
        if previous_train_end is not None and fold.train_end_exclusive <= previous_train_end:
            raise ValueError("expanding train end must increase")
        if previous_validation_end is not None and fold.validation_start != previous_validation_end:
            raise ValueError("validation folds must be contiguous")
        previous_train_end = fold.train_end_exclusive
        previous_validation_end = fold.validation_end_exclusive


def _align_price(price: Decimal, tick: Decimal, *, upward: bool) -> Decimal:
    rounding = ROUND_CEILING if upward else ROUND_FLOOR
    return (price / tick).to_integral_value(rounding=rounding) * tick


def _atr_series(bars: tuple[MarketBar, ...], period: int) -> tuple[Decimal | None, ...]:
    true_ranges: list[Decimal] = []
    for index, bar in enumerate(bars):
        if index == 0:
            true_range = bar.high - bar.low
        else:
            previous_close = bars[index - 1].close
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        true_ranges.append(true_range)
    values: list[Decimal | None] = [None] * len(bars)
    if len(bars) < period:
        return tuple(values)
    atr = sum(true_ranges[:period], ZERO) / Decimal(period)
    values[period - 1] = atr
    for index in range(period, len(bars)):
        # Wilder RMA 只递推到当前已完成 candle，不读取未来数据。
        atr = (atr * Decimal(period - 1) + true_ranges[index]) / Decimal(period)
        values[index] = atr
    return tuple(values)


def _validate_market_data(
    bars_by_pair: dict[str, tuple[MarketBar, ...]], expected_interval: timedelta
) -> tuple[str, ...]:
    if expected_interval <= timedelta(0):
        raise ValueError("expected_interval must be positive")
    if not bars_by_pair:
        raise ValueError("bars_by_pair must not be empty")
    pairs = tuple(sorted(bars_by_pair))
    reference_timestamps: tuple[datetime, ...] | None = None
    for pair in pairs:
        bars = bars_by_pair[pair]
        if len(bars) < 2:
            raise ValueError(f"{pair} must contain at least two bars")
        timestamps = tuple(bar.timestamp for bar in bars)
        if any(
            current - previous != expected_interval for previous, current in pairwise(timestamps)
        ):
            raise ValueError(f"{pair} bars must follow the expected timeframe grid")
        if reference_timestamps is None:
            reference_timestamps = timestamps
        elif timestamps != reference_timestamps:
            raise ValueError("all pairs must use exactly aligned timestamps")
    return pairs


def _event_count(trades: list[TradeRecord], cluster_hours: int) -> int:
    entries = sorted(trade.entry_timestamp for trade in trades)
    if not entries:
        return 0
    count = 1
    cluster = timedelta(hours=cluster_hours)
    previous = entries[0]
    for timestamp in entries[1:]:
        if timestamp - previous > cluster:
            count += 1
        previous = timestamp
    return count


def _maximum_drawdown(equities: list[Decimal]) -> Decimal:
    peak = equities[0]
    maximum = ZERO
    for equity in equities[1:]:
        peak = max(peak, equity)
        maximum = max(maximum, ONE - equity / peak)
    return maximum


def run_portfolio_backtest(
    bars_by_pair: dict[str, tuple[MarketBar, ...]],
    *,
    evaluation_start: datetime,
    evaluation_end_exclusive: datetime,
    expected_interval: timedelta,
    trial: DonchianTrial,
    costs: CostAssumptions,
    constraints: dict[str, MarketConstraints],
    settings: BacktestSettings,
) -> BacktestResult:
    """运行 flat-start、next-open 成交的多标的 long/flat 确定性回测。"""

    pairs = _validate_market_data(bars_by_pair, expected_interval)
    if set(constraints) != set(pairs):
        raise ValueError("constraints must match bars_by_pair")
    if evaluation_start.tzinfo is None or evaluation_start.utcoffset() != timedelta(0):
        raise ValueError("evaluation_start must be UTC")
    if evaluation_end_exclusive <= evaluation_start:
        raise ValueError("evaluation range must not be empty")

    timestamps = tuple(bar.timestamp for bar in bars_by_pair[pairs[0]])
    active_indices = [
        index
        for index, timestamp in enumerate(timestamps)
        if evaluation_start <= timestamp < evaluation_end_exclusive
    ]
    if not active_indices:
        raise ValueError("evaluation range contains no bars")
    if timestamps[active_indices[0]] != evaluation_start:
        raise ValueError("evaluation_start must align to an available candle")

    atr_by_pair = {pair: _atr_series(bars_by_pair[pair], trial.atr_period) for pair in pairs}
    cash = settings.initial_equity
    positions: dict[str, _Position] = {}
    pending_entries: dict[str, tuple[datetime, Decimal]] = {}
    pending_exits: set[str] = set()
    trades: list[TradeRecord] = []
    equities = [settings.initial_equity]

    def marked_nav(index: int, *, use_close: bool) -> Decimal:
        marked = cash
        for pair, position in positions.items():
            bar = bars_by_pair[pair][index]
            marked += position.quantity * (bar.close if use_close else bar.open)
        return marked

    def close_position(pair: str, price: Decimal, timestamp: datetime, reason: str) -> None:
        nonlocal cash
        position = positions.pop(pair)
        constraint = constraints[pair]
        exit_price = _align_price(price, constraint.price_tick, upward=False)
        gross = position.quantity * exit_price
        cash_in = gross * (ONE - costs.per_side_rate)
        cash += cash_in
        pnl = cash_in - position.entry_cash_out
        trades.append(
            TradeRecord(
                pair=pair,
                entry_signal_timestamp=position.entry_signal_timestamp,
                entry_timestamp=position.entry_timestamp,
                exit_timestamp=timestamp,
                entry_price=position.entry_price,
                exit_price=exit_price,
                quantity=position.quantity,
                net_pnl_quote=pnl,
                return_r=pnl / position.entry_risk_cash,
                exit_reason=reason,
            )
        )

    for active_offset, index in enumerate(active_indices):
        timestamp = timestamps[index]

        # 上一根已完成 candle 的退出信号先于本根 open 新入场执行，避免瞬时超暴露。
        for pair in sorted(pending_exits):
            if pair in positions:
                close_position(pair, bars_by_pair[pair][index].open, timestamp, "channel_exit")
        pending_exits.clear()

        for pair in sorted(pending_entries):
            if pair in positions:
                continue
            signal_timestamp, signal_atr = pending_entries[pair]
            bar = bars_by_pair[pair][index]
            constraint = constraints[pair]
            entry_price = _align_price(bar.open, constraint.price_tick, upward=True)
            raw_stop = entry_price - signal_atr * trial.stop_multiple
            stop_price = _align_price(raw_stop, constraint.price_tick, upward=False)
            if stop_price <= ZERO or stop_price >= entry_price:
                continue
            nav = marked_nav(index, use_close=False)
            directional_exposure = sum(
                (
                    position.quantity * bars_by_pair[open_pair][index].open
                    for open_pair, position in positions.items()
                ),
                ZERO,
            )
            fee_buffer = (entry_price + stop_price) * costs.fee_rate_per_side
            slippage_buffer = (entry_price + stop_price) * (
                costs.half_spread_rate + costs.slippage_rate_per_side
            )
            request = PositionSizeRequest(
                nav=nav,
                risk_fraction=settings.risk_fraction,
                entry_price=entry_price,
                stop_price=stop_price,
                minimum_stop_distance=constraint.price_tick,
                fee_buffer_per_unit=fee_buffer,
                slippage_buffer_per_unit=slippage_buffer,
                gap_buffer_per_unit=entry_price * costs.gap_buffer_rate,
                maximum_unit_loss=entry_price * settings.maximum_unit_loss_fraction,
                volatility_cap_quantity=nav * settings.volatility_cap_fraction / entry_price,
                symbol_exposure_limit_quote=nav * settings.symbol_exposure_fraction,
                current_symbol_exposure_quote=ZERO,
                pending_symbol_entry_exposure_quote=ZERO,
                directional_exposure_limit_quote=nav * settings.directional_exposure_fraction,
                current_directional_exposure_quote=directional_exposure,
                pending_directional_entry_exposure_quote=ZERO,
                available_balance_cap_quantity=cash / (entry_price * (ONE + costs.per_side_rate)),
                price_tick=constraint.price_tick,
                quantity_step=constraint.quantity_step,
                minimum_quantity=constraint.minimum_quantity,
                minimum_notional=constraint.minimum_notional,
            )
            decision = calculate_position_size(
                PositionSizeContext(1, RiskContextSource.BACKTEST, None, request)
            )
            if not decision.approved:
                continue
            quantity = decision.approved_quantity
            gross = quantity * entry_price
            cash_out = gross * (ONE + costs.per_side_rate)
            if cash_out > cash:
                raise RuntimeError("approved quantity exceeds available cash")
            cash -= cash_out
            positions[pair] = _Position(
                quantity=quantity,
                entry_price=entry_price,
                entry_cash_out=cash_out,
                entry_risk_cash=decision.risk_cash,
                stop_price=stop_price,
                entry_signal_timestamp=signal_timestamp,
                entry_timestamp=timestamp,
            )
        pending_entries.clear()

        # 保守处理 stop gap：open 已低于 stop 时按更差的 open，否则按 stop 成交。
        for pair in tuple(sorted(positions)):
            position = positions[pair]
            bar = bars_by_pair[pair][index]
            if bar.low <= position.stop_price:
                stop_fill = min(bar.open, position.stop_price)
                close_position(pair, stop_fill, timestamp, "initial_stop")

        is_final_bar = active_offset == len(active_indices) - 1
        if is_final_bar:
            for pair in tuple(sorted(positions)):
                close_position(pair, bars_by_pair[pair][index].close, timestamp, "fold_end")

        close_equity = marked_nav(index, use_close=True)
        if close_equity <= ZERO:
            raise RuntimeError("portfolio equity must remain positive")
        equities.append(close_equity)

        if is_final_bar:
            continue
        for pair in pairs:
            bars = bars_by_pair[pair]
            if pair in positions:
                if index >= trial.exit_window:
                    threshold = min(bar.low for bar in bars[index - trial.exit_window : index])
                    if bars[index].close < threshold:
                        pending_exits.add(pair)
            elif index >= trial.entry_window:
                threshold = max(bar.high for bar in bars[index - trial.entry_window : index])
                candidate_atr = atr_by_pair[pair][index]
                if bars[index].close > threshold and candidate_atr is not None:
                    pending_entries[pair] = (timestamp, candidate_atr)

    period_returns = tuple(
        float(current / previous - ONE) for previous, current in pairwise(equities)
    )
    expectancy = (
        sum((trade.return_r for trade in trades), ZERO) / Decimal(len(trades)) if trades else None
    )
    return BacktestResult(
        initial_equity=settings.initial_equity,
        final_equity=equities[-1],
        net_return=equities[-1] / settings.initial_equity - ONE,
        maximum_drawdown=_maximum_drawdown(equities),
        trades=tuple(trades),
        independent_event_count=_event_count(trades, settings.event_cluster_hours),
        expectancy_r=expectancy,
        period_returns=period_returns,
    )


def bootstrap_mean_confidence_interval(
    values: tuple[Decimal, ...],
    *,
    confidence_level: Decimal,
    resamples: int,
    seed: int,
) -> tuple[Decimal, Decimal] | None:
    """使用固定 seed 的 percentile bootstrap 估计均值置信区间。"""

    if not values:
        return None
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
        raise ValueError("bootstrap values must be finite Decimal values")
    if not ZERO < confidence_level < ONE:
        raise ValueError("confidence_level must be in (0, 1)")
    if type(resamples) is not int or resamples < 100:
        raise ValueError("resamples must be an integer of at least 100")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    random_source = random.Random(seed)
    count = len(values)
    means = sorted(
        sum((values[random_source.randrange(count)] for _ in range(count)), ZERO) / Decimal(count)
        for _ in range(resamples)
    )
    tail = (ONE - confidence_level) / Decimal("2")
    lower_index = int((Decimal(resamples - 1) * tail).to_integral_value(rounding=ROUND_FLOOR))
    upper_index = int(
        (Decimal(resamples - 1) * (ONE - tail)).to_integral_value(rounding=ROUND_CEILING)
    )
    return means[lower_index], means[upper_index]


def nonannualized_sharpe(returns: tuple[float, ...]) -> float | None:
    if len(returns) < 2:
        return None
    volatility = statistics.pstdev(returns)
    return statistics.fmean(returns) / volatility if volatility > 0 else None


def deflated_sharpe_probability(
    returns: tuple[float, ...], trial_sharpes: tuple[float, ...]
) -> tuple[float, float] | None:
    """返回 DSR 概率与多重测试期望最大 Sharpe 阈值。

    公式对应 Bailey 与 López de Prado (2014) Eq. (1)-(2)。OAT trial 相关性会使
    有效 trial 数小于原始数量；这里使用完整计数是显式保守上界。
    """

    observed = nonannualized_sharpe(returns)
    if observed is None or len(returns) < 3 or len(trial_sharpes) < 2:
        return None
    trial_standard_deviation = statistics.pstdev(trial_sharpes)
    trial_count = len(trial_sharpes)
    euler_mascheroni = 0.5772156649015329
    normal = NormalDist()
    expected_maximum = trial_standard_deviation * (
        (1 - euler_mascheroni) * normal.inv_cdf(1 - 1 / trial_count)
        + euler_mascheroni * normal.inv_cdf(1 - 1 / (trial_count * math.e))
    )
    mean = statistics.fmean(returns)
    variance = statistics.fmean((value - mean) ** 2 for value in returns)
    if variance <= 0:
        return None
    deviation = math.sqrt(variance)
    skewness = statistics.fmean(((value - mean) / deviation) ** 3 for value in returns)
    kurtosis = statistics.fmean(((value - mean) / deviation) ** 4 for value in returns)
    denominator_squared = 1 - skewness * observed + ((kurtosis - 1) / 4) * observed * observed
    if denominator_squared <= 0:
        return None
    statistic = (
        (observed - expected_maximum) * math.sqrt(len(returns) - 1) / math.sqrt(denominator_squared)
    )
    return normal.cdf(statistic), expected_maximum


def profit_concentration(trades: tuple[TradeRecord, ...]) -> dict[str, object]:
    """披露 Top 5、正盈利 HHI 和各标的净贡献，不用单一阈值替代判断。"""

    profitable = sorted(
        (trade for trade in trades if trade.net_pnl_quote > ZERO),
        key=lambda trade: trade.net_pnl_quote,
        reverse=True,
    )
    gross_profit = sum((trade.net_pnl_quote for trade in profitable), ZERO)
    hhi = (
        sum((trade.net_pnl_quote / gross_profit) ** 2 for trade in profitable)
        if gross_profit > ZERO
        else None
    )
    pair_net: dict[str, Decimal] = {}
    for trade in trades:
        pair_net[trade.pair] = pair_net.get(trade.pair, ZERO) + trade.net_pnl_quote
    return {
        "gross_profit_quote": gross_profit,
        "positive_profit_hhi": hhi,
        "top_5_profit_contribution": (
            sum((trade.net_pnl_quote for trade in profitable[:5]), ZERO) / gross_profit
            if gross_profit > ZERO
            else None
        ),
        "top_5_trades": tuple(profitable[:5]),
        "pair_net_pnl_quote": pair_net,
    }
