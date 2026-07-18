"""R1-04 spot/futures 只读风险观测、会计决策与 RiskSnapshot v2 原子发布。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from alphamind.config.instruments import InstrumentRegistry, MarketKind
from alphamind.config.risk_limits import RiskLimitsConfig
from alphamind.market.capabilities import MarketCapabilitySnapshot
from alphamind.risk.account_loss import AccountPnlObservation, evaluate_absolute_loss

ZERO = Decimal("0")
SNAPSHOT_TTL = timedelta(seconds=60)
WATCHDOG_TARGET_INTERVAL = timedelta(seconds=15)
MAXIMUM_SOURCE_AGE = timedelta(seconds=30)
MAXIMUM_FUTURE_CLOCK_SKEW = timedelta(seconds=5)
SPOT_PAIR_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,15}/USDT$")
FUTURES_PAIR_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,15}/USDT:USDT$")


class CashFlowKind(StrEnum):
    """不计入策略收益的账户外部现金流类别。"""

    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    REBATE = "rebate"
    REWARD = "reward"


class RiskState(StrEnum):
    ENTRY_ALLOWED = "ENTRY_ALLOWED"
    CLOSE_ONLY = "CLOSE_ONLY"
    KILLED_MANUAL_REVIEW = "KILLED_MANUAL_REVIEW"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderIntent(StrEnum):
    ENTRY = "entry"
    REDUCE = "reduce"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


@dataclass(frozen=True, slots=True)
class ExternalCashFlow:
    event_id: str
    occurred_at_utc: datetime
    kind: CashFlowKind
    amount: Decimal


@dataclass(frozen=True, slots=True)
class PositionObservation:
    """现货 long 持仓观测。"""

    pair: str
    base_quantity: Decimal
    best_bid: Decimal
    last_trade: Decimal


@dataclass(frozen=True, slots=True)
class FuturesPositionObservation:
    """USDT 线性永续持仓；PnL、保证金和 funding 均为交易所只读事实。"""

    pair: str
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal
    leverage: Decimal
    position_margin_quote: Decimal
    maintenance_margin_quote: Decimal
    unrealized_pnl_quote: Decimal
    funding_rate: Decimal
    accrued_funding_quote: Decimal
    next_funding_at_utc: datetime


@dataclass(frozen=True, slots=True)
class OpenOrderObservation:
    """未完成普通单、条件单或保护单的统一只读观测。"""

    order_id: str
    pair: str
    market: MarketKind
    side: OrderSide
    position_side: PositionSide
    intent: OrderIntent
    order_type: str
    quantity: Decimal
    filled_quantity: Decimal
    reference_price: Decimal
    reduce_only: bool
    created_at_utc: datetime
    updated_at_utc: datetime
    trigger_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AccountRuntimeObservation:
    """交易所账户与 Freqtrade Runtime DB 对账后的只读观测。"""

    account_id: str
    accounting_currency: str
    observed_at_utc: datetime
    quote_cash: Decimal
    available_balance_quote: Decimal
    positions: tuple[PositionObservation | FuturesPositionObservation, ...]
    open_orders: tuple[OpenOrderObservation, ...]
    accrued_fees: Decimal
    known_liabilities: Decimal
    unexplained_balance_difference: Decimal
    available_margin_quote: Decimal
    used_margin_quote: Decimal
    orders_observed_at_utc: datetime
    orders_complete: bool
    account_complete: bool
    runtime_reconciled: bool


@dataclass(frozen=True, slots=True)
class PeriodBoundary:
    observed_at_utc: datetime
    opening_nav: Decimal


@dataclass(frozen=True, slots=True)
class RiskAccountingState:
    """上次已发布状态与本日/本周边界，禁止用晚到观测代替边界。"""

    approved_capital_baseline: Decimal
    cumulative_external_cash_flow_before: Decimal
    daily_external_cash_flow_before: Decimal
    weekly_external_cash_flow_before: Decimal
    cashflow_adjusted_high_water_mark_before: Decimal
    daily_boundary: PeriodBoundary
    weekly_boundary: PeriodBoundary
    external_cash_flow_review_pending: bool


@dataclass(frozen=True, slots=True)
class WatchdogObservation:
    generated_at_utc: datetime
    market_observed_at_utc: datetime
    market_complete: bool
    account: AccountRuntimeObservation
    accounting_state: RiskAccountingState
    external_cash_flows: tuple[ExternalCashFlow, ...] = ()
    manual_kill_switch: bool = False


@dataclass(frozen=True, slots=True)
class SnapshotReadResult:
    """消费者可直接使用的常数时间决策；失败时始终保留安全退出。"""

    snapshot: dict[str, Any] | None
    entry_allowed: bool
    close_only: bool
    kill_switch: bool
    safe_exit_allowed: bool
    reason_codes: tuple[str, ...]


def _require_utc(value: datetime, *, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")


def _require_decimal(value: Decimal, *, field_name: str, positive: bool = False) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if positive and value <= ZERO:
        raise ValueError(f"{field_name} must be positive")


def _decimal_text(value: Decimal) -> str:
    # 风险快照禁止指数形式和负零，确保 JSON 中始终是精确十进制字符串。
    if value == ZERO:
        return "0"
    return format(value, "f")


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _day_boundary(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_boundary(value: datetime) -> datetime:
    day = _day_boundary(value)
    return day - timedelta(days=day.weekday())


def _normalize_allowed_pairs(
    allowed_pairs: Collection[str],
    *,
    market: MarketKind,
) -> frozenset[str]:
    normalized = frozenset(allowed_pairs)
    pattern = SPOT_PAIR_PATTERN if market is MarketKind.SPOT else FUTURES_PAIR_PATTERN
    if len(normalized) > 50:
        raise ValueError("allowed pairs must contain at most 50 unique pairs")
    if len(normalized) != len(allowed_pairs) or any(
        not isinstance(pair, str) or pattern.fullmatch(pair) is None for pair in normalized
    ):
        raise ValueError(f"allowed pairs must contain unique {market.value} USDT pairs")
    return normalized


def _instrument_id_for_pair(pair: str) -> str:
    return pair.split("/", maxsplit=1)[0]


def _validate_observation(
    observation: WatchdogObservation,
    *,
    instrument_registry: InstrumentRegistry,
    market_capabilities: MarketCapabilitySnapshot,
) -> None:
    _require_utc(observation.generated_at_utc, field_name="generated_at_utc")
    _require_utc(observation.market_observed_at_utc, field_name="market_observed_at_utc")
    account = observation.account
    state = observation.accounting_state
    _require_utc(account.observed_at_utc, field_name="account.observed_at_utc")
    if (
        type(account.account_complete) is not bool
        or type(account.runtime_reconciled) is not bool
        or type(observation.market_complete) is not bool
        or type(observation.manual_kill_switch) is not bool
    ):
        raise TypeError("observation completeness and control flags must be bool")
    if not account.account_id.strip() or len(account.account_id) > 128:
        raise ValueError("account_id must contain 1 to 128 non-blank characters")

    for field_name, value in (
        ("quote_cash", account.quote_cash),
        ("available_balance_quote", account.available_balance_quote),
        ("accrued_fees", account.accrued_fees),
        ("known_liabilities", account.known_liabilities),
        ("unexplained_balance_difference", account.unexplained_balance_difference),
        ("available_margin_quote", account.available_margin_quote),
        ("used_margin_quote", account.used_margin_quote),
        ("approved_capital_baseline", state.approved_capital_baseline),
        ("cumulative_external_cash_flow_before", state.cumulative_external_cash_flow_before),
        ("daily_external_cash_flow_before", state.daily_external_cash_flow_before),
        ("weekly_external_cash_flow_before", state.weekly_external_cash_flow_before),
        (
            "cashflow_adjusted_high_water_mark_before",
            state.cashflow_adjusted_high_water_mark_before,
        ),
        ("daily_boundary.opening_nav", state.daily_boundary.opening_nav),
        ("weekly_boundary.opening_nav", state.weekly_boundary.opening_nav),
    ):
        _require_decimal(
            value,
            field_name=field_name,
            positive=field_name
            in {
                "approved_capital_baseline",
                "cashflow_adjusted_high_water_mark_before",
                "daily_boundary.opening_nav",
                "weekly_boundary.opening_nav",
            },
        )
    if any(
        value < ZERO
        for value in (
            account.quote_cash,
            account.available_balance_quote,
            account.accrued_fees,
            account.known_liabilities,
            account.available_margin_quote,
            account.used_margin_quote,
        )
    ):
        raise ValueError("cash, fees, liabilities and exposure must not be negative")

    _require_utc(state.daily_boundary.observed_at_utc, field_name="daily_boundary")
    _require_utc(state.weekly_boundary.observed_at_utc, field_name="weekly_boundary")
    if state.daily_boundary.observed_at_utc != _day_boundary(observation.generated_at_utc):
        raise ValueError("daily boundary must be exactly 00:00:00 UTC for the current day")
    if state.weekly_boundary.observed_at_utc != _week_boundary(observation.generated_at_utc):
        raise ValueError("weekly boundary must be exactly Monday 00:00:00 UTC")
    if type(state.external_cash_flow_review_pending) is not bool:
        raise TypeError("external_cash_flow_review_pending must be bool")

    if market_capabilities.instrument_registry_sha256 != instrument_registry.source_sha256:
        raise ValueError("Market Capability does not match Instrument Registry")
    supported_spot = frozenset(market_capabilities.available_pairs(MarketKind.SPOT))
    supported_futures = frozenset(market_capabilities.available_pairs(MarketKind.FUTURES))
    if len(account.positions) > 100:
        raise ValueError("positions exceed the configured maximum")
    position_keys: set[tuple[str, str, str]] = set()
    observed_margin = ZERO
    for position in account.positions:
        if isinstance(position, PositionObservation):
            key = (MarketKind.SPOT.value, position.pair, PositionSide.LONG.value)
            if position.pair not in supported_spot:
                raise ValueError("positions must use available Instrument Registry spot pairs")
            for field_name, value in (
                ("base_quantity", position.base_quantity),
                ("best_bid", position.best_bid),
                ("last_trade", position.last_trade),
            ):
                _require_decimal(value, field_name=f"{position.pair}.{field_name}")
            if position.base_quantity <= ZERO:
                raise ValueError("position base_quantity must be positive")
            if position.best_bid <= ZERO or position.last_trade <= ZERO:
                raise ValueError("position marks must be positive")
        elif isinstance(position, FuturesPositionObservation):
            key = (MarketKind.FUTURES.value, position.pair, position.side.value)
            if position.pair not in supported_futures:
                raise ValueError("positions must use available Instrument Registry futures pairs")
            configured = instrument_registry.instrument_for_pair(position.pair, MarketKind.FUTURES)
            capability = market_capabilities.capability_for_pair(position.pair, MarketKind.FUTURES)
            if configured is None or capability is None or not capability.available:
                raise ValueError("futures position market is unavailable")
            if (position.side is PositionSide.LONG and not configured.futures.allow_long) or (
                position.side is PositionSide.SHORT and not configured.futures.allow_short
            ):
                raise ValueError("futures position direction is disabled by Instrument Registry")
            for field_name, value in (
                ("quantity", position.quantity),
                ("entry_price", position.entry_price),
                ("mark_price", position.mark_price),
                ("liquidation_price", position.liquidation_price),
                ("leverage", position.leverage),
                ("position_margin_quote", position.position_margin_quote),
                ("maintenance_margin_quote", position.maintenance_margin_quote),
                ("unrealized_pnl_quote", position.unrealized_pnl_quote),
                ("funding_rate", position.funding_rate),
                ("accrued_funding_quote", position.accrued_funding_quote),
            ):
                _require_decimal(value, field_name=f"{position.pair}.{field_name}")
            if (
                any(
                    value <= ZERO
                    for value in (
                        position.quantity,
                        position.entry_price,
                        position.mark_price,
                        position.liquidation_price,
                        position.leverage,
                        position.position_margin_quote,
                    )
                )
                or position.maintenance_margin_quote < ZERO
            ):
                raise ValueError("futures quantity, prices, leverage and margin are invalid")
            if capability.effective_max_leverage is None or (
                position.leverage > capability.effective_max_leverage
            ):
                raise ValueError("futures leverage exceeds effective market capability")
            expected_pnl = (
                (position.mark_price - position.entry_price) * position.quantity
                if position.side is PositionSide.LONG
                else (position.entry_price - position.mark_price) * position.quantity
            )
            if position.unrealized_pnl_quote != expected_pnl:
                raise ValueError("futures unrealized PnL formula mismatch")
            if (
                position.side is PositionSide.LONG
                and position.liquidation_price >= position.mark_price
            ) or (
                position.side is PositionSide.SHORT
                and position.liquidation_price <= position.mark_price
            ):
                raise ValueError("futures liquidation price is not beyond the mark")
            _require_utc(
                position.next_funding_at_utc,
                field_name=f"{position.pair}.next_funding_at_utc",
            )
            if position.next_funding_at_utc <= observation.generated_at_utc:
                raise ValueError("next funding time must be after snapshot generation")
            observed_margin += position.position_margin_quote
        else:
            raise TypeError("positions must use supported observation types")
        if key in position_keys:
            raise ValueError("positions must be unique by market, pair and side")
        position_keys.add(key)
    if observed_margin != account.used_margin_quote:
        raise ValueError("used margin must equal futures position margin")

    _require_utc(account.orders_observed_at_utc, field_name="account.orders_observed_at_utc")
    if type(account.orders_complete) is not bool:
        raise TypeError("orders_complete must be bool")
    if len(account.open_orders) > 200:
        raise ValueError("open orders exceed the configured maximum")
    seen_order_ids: set[str] = set()
    for order in account.open_orders:
        if not isinstance(order, OpenOrderObservation):
            raise TypeError("open_orders must contain OpenOrderObservation")
        if (
            not order.order_id.strip()
            or len(order.order_id) > 128
            or order.order_id in seen_order_ids
        ):
            raise ValueError("open order ids must be unique non-empty strings")
        seen_order_ids.add(order.order_id)
        allowed = supported_spot if order.market is MarketKind.SPOT else supported_futures
        if order.pair not in allowed:
            raise ValueError("open order pair is unavailable or not configured")
        configured = instrument_registry.instrument_for_pair(order.pair, order.market)
        if configured is None:
            raise ValueError("open order pair is not in Instrument Registry")
        if order.market is MarketKind.SPOT and order.position_side is not PositionSide.LONG:
            raise ValueError("spot open orders must use long position_side")
        if order.market is MarketKind.FUTURES and (
            (order.position_side is PositionSide.LONG and not configured.futures.allow_long)
            or (order.position_side is PositionSide.SHORT and not configured.futures.allow_short)
        ):
            raise ValueError("open order direction is disabled by Instrument Registry")
        expected_side = (
            OrderSide.BUY
            if (order.intent is OrderIntent.ENTRY) == (order.position_side is PositionSide.LONG)
            else OrderSide.SELL
        )
        if order.side is not expected_side:
            raise ValueError("open order side is inconsistent with intent and position_side")
        if order.order_type not in {"limit", "market", "stop_limit", "stop_market"}:
            raise ValueError("open order type is unsupported")
        for field_name, value in (
            ("quantity", order.quantity),
            ("filled_quantity", order.filled_quantity),
            ("reference_price", order.reference_price),
        ):
            _require_decimal(value, field_name=f"{order.order_id}.{field_name}")
        if (
            order.quantity <= ZERO
            or order.filled_quantity < ZERO
            or order.filled_quantity >= order.quantity
            or order.reference_price <= ZERO
        ):
            raise ValueError("open order quantities and reference price are invalid")
        protection = order.intent in {OrderIntent.STOP_LOSS, OrderIntent.TAKE_PROFIT}
        if order.intent is OrderIntent.ENTRY:
            if order.reduce_only:
                raise ValueError("entry order cannot be reduce_only")
        elif not order.reduce_only:
            raise ValueError("reduce and protection orders must be reduce_only")
        if (protection or order.order_type.startswith("stop")) and order.trigger_price is None:
            raise ValueError("conditional or protection order requires trigger_price")
        if order.trigger_price is not None:
            _require_decimal(order.trigger_price, field_name=f"{order.order_id}.trigger_price")
            if order.trigger_price <= ZERO:
                raise ValueError("trigger_price must be positive")
        _require_utc(order.created_at_utc, field_name=f"{order.order_id}.created_at_utc")
        _require_utc(order.updated_at_utc, field_name=f"{order.order_id}.updated_at_utc")
        if not (order.created_at_utc <= order.updated_at_utc <= observation.generated_at_utc):
            raise ValueError("open order timestamps are invalid")

    event_ids: set[str] = set()
    for cash_flow in observation.external_cash_flows:
        if not cash_flow.event_id.strip() or cash_flow.event_id in event_ids:
            raise ValueError("external cash flow event ids must be unique and non-empty")
        event_ids.add(cash_flow.event_id)
        _require_utc(cash_flow.occurred_at_utc, field_name="cash_flow.occurred_at_utc")
        _require_decimal(cash_flow.amount, field_name="cash_flow.amount")
        if cash_flow.amount == ZERO:
            raise ValueError("external cash flow amount must not be zero")
        if cash_flow.occurred_at_utc > observation.generated_at_utc:
            raise ValueError("external cash flow must not occur after snapshot generation")
        if cash_flow.kind is CashFlowKind.WITHDRAWAL and cash_flow.amount > ZERO:
            raise ValueError("withdrawal amount must be negative from the account perspective")
        if cash_flow.kind is not CashFlowKind.WITHDRAWAL and cash_flow.amount < ZERO:
            raise ValueError("deposit, rebate and reward amounts must be positive")


def build_risk_snapshot(
    observation: WatchdogObservation,
    config: RiskLimitsConfig,
    instrument_registry: InstrumentRegistry,
    market_capabilities: MarketCapabilitySnapshot,
    *,
    risk_config_sha256: str,
    producer_version: str,
) -> dict[str, Any]:
    """从完整只读观测计算 schema v2 风险快照。"""

    allowed_spot_pairs = market_capabilities.available_pairs(MarketKind.SPOT)
    allowed_futures_pairs = market_capabilities.available_pairs(MarketKind.FUTURES)
    _validate_observation(
        observation,
        instrument_registry=instrument_registry,
        market_capabilities=market_capabilities,
    )
    if not re.fullmatch(r"[a-f0-9]{64}", risk_config_sha256):
        raise ValueError("risk_config_sha256 must be a lowercase SHA-256")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", producer_version):
        raise ValueError("producer_version must be semantic-version compatible")

    account = observation.account
    state = observation.accounting_state
    positions: list[dict[str, Any]] = []
    open_orders: list[dict[str, Any]] = []
    open_exposure = ZERO
    spot_exposure = ZERO
    futures_long_notional = ZERO
    futures_short_notional = ZERO
    futures_unrealized_pnl = ZERO
    accrued_funding = ZERO
    for position in sorted(
        account.positions,
        key=lambda item: (
            MarketKind.SPOT.value
            if isinstance(item, PositionObservation)
            else MarketKind.FUTURES.value,
            item.pair,
            PositionSide.LONG.value if isinstance(item, PositionObservation) else item.side.value,
        ),
    ):
        instrument_id = _instrument_id_for_pair(position.pair)
        if isinstance(position, PositionObservation):
            conservative_mark = min(position.best_bid, position.last_trade)
            marked_value = position.base_quantity * conservative_mark
            spot_exposure += marked_value
            open_exposure += marked_value
            positions.append(
                {
                    "instrument_id": instrument_id,
                    "market": "spot",
                    "pair": position.pair,
                    "side": "long",
                    "quantity": _decimal_text(position.base_quantity),
                    "best_bid": _decimal_text(position.best_bid),
                    "last_trade": _decimal_text(position.last_trade),
                    "conservative_exit_mark": _decimal_text(conservative_mark),
                    "marked_value": _decimal_text(marked_value),
                }
            )
        else:
            notional = position.quantity * position.mark_price
            liquidation_buffer = (
                (position.mark_price - position.liquidation_price) / position.mark_price
                if position.side is PositionSide.LONG
                else (position.liquidation_price - position.mark_price) / position.mark_price
            )
            open_exposure += notional
            futures_unrealized_pnl += position.unrealized_pnl_quote
            accrued_funding += position.accrued_funding_quote
            if position.side is PositionSide.LONG:
                futures_long_notional += notional
            else:
                futures_short_notional += notional
            positions.append(
                {
                    "instrument_id": instrument_id,
                    "market": "linear_perpetual",
                    "pair": position.pair,
                    "side": position.side.value,
                    "quantity": _decimal_text(position.quantity),
                    "entry_price": _decimal_text(position.entry_price),
                    "mark_price": _decimal_text(position.mark_price),
                    "liquidation_price": _decimal_text(position.liquidation_price),
                    "leverage": _decimal_text(position.leverage),
                    "notional_quote": _decimal_text(notional),
                    "position_margin_quote": _decimal_text(position.position_margin_quote),
                    "maintenance_margin_quote": _decimal_text(position.maintenance_margin_quote),
                    "unrealized_pnl_quote": _decimal_text(position.unrealized_pnl_quote),
                    "funding_rate": _decimal_text(position.funding_rate),
                    "accrued_funding_quote": _decimal_text(position.accrued_funding_quote),
                    "next_funding_at_utc": _timestamp_text(position.next_funding_at_utc),
                    "liquidation_buffer_fraction": _decimal_text(liquidation_buffer),
                }
            )

    pending_spot = ZERO
    pending_futures_long = ZERO
    pending_futures_short = ZERO
    for order in sorted(account.open_orders, key=lambda item: item.order_id):
        remaining_quantity = order.quantity - order.filled_quantity
        remaining_notional = remaining_quantity * order.reference_price
        if order.intent is OrderIntent.ENTRY:
            if order.market is MarketKind.SPOT:
                pending_spot += remaining_notional
            elif order.position_side is PositionSide.LONG:
                pending_futures_long += remaining_notional
            else:
                pending_futures_short += remaining_notional
        open_orders.append(
            {
                "order_id": order.order_id,
                "instrument_id": _instrument_id_for_pair(order.pair),
                "market": ("spot" if order.market is MarketKind.SPOT else "linear_perpetual"),
                "pair": order.pair,
                "side": order.side.value,
                "position_side": order.position_side.value,
                "intent": order.intent.value,
                "order_type": order.order_type,
                "quantity": _decimal_text(order.quantity),
                "filled_quantity": _decimal_text(order.filled_quantity),
                "remaining_quantity": _decimal_text(remaining_quantity),
                "reference_price": _decimal_text(order.reference_price),
                "trigger_price": (
                    _decimal_text(order.trigger_price) if order.trigger_price is not None else None
                ),
                "remaining_notional_quote": _decimal_text(remaining_notional),
                "reduce_only": order.reduce_only,
                "created_at_utc": _timestamp_text(order.created_at_utc),
                "updated_at_utc": _timestamp_text(order.updated_at_utc),
            }
        )
    pending_entry_exposure = pending_spot + pending_futures_long + pending_futures_short

    nav = (
        account.quote_cash
        + spot_exposure
        + futures_unrealized_pnl
        + accrued_funding
        - account.accrued_fees
        - account.known_liabilities
    )
    if nav <= ZERO:
        raise ValueError("calculated NAV must be positive")

    current_cash_flow = sum((event.amount for event in observation.external_cash_flows), ZERO)
    daily_current_cash_flow = sum(
        (
            event.amount
            for event in observation.external_cash_flows
            if event.occurred_at_utc >= state.daily_boundary.observed_at_utc
        ),
        ZERO,
    )
    weekly_current_cash_flow = sum(
        (
            event.amount
            for event in observation.external_cash_flows
            if event.occurred_at_utc >= state.weekly_boundary.observed_at_utc
        ),
        ZERO,
    )
    cumulative_cash_flow = state.cumulative_external_cash_flow_before + current_cash_flow
    daily_cash_flow = state.daily_external_cash_flow_before + daily_current_cash_flow
    weekly_cash_flow = state.weekly_external_cash_flow_before + weekly_current_cash_flow
    cumulative_pnl = nav - state.approved_capital_baseline - cumulative_cash_flow
    daily_pnl = nav - state.daily_boundary.opening_nav - daily_cash_flow
    weekly_pnl = nav - state.weekly_boundary.opening_nav - weekly_cash_flow
    adjusted_hwm = max(
        state.cashflow_adjusted_high_water_mark_before + current_cash_flow,
        nav,
    )
    if adjusted_hwm <= ZERO:
        raise ValueError("cashflow-adjusted high-water mark must remain positive")
    drawdown = max(Decimal("1") - nav / adjusted_hwm, ZERO)

    absolute_loss = evaluate_absolute_loss(
        AccountPnlObservation(
            accounting_currency=account.accounting_currency,
            cashflow_adjusted_capital_baseline=state.approved_capital_baseline,
            cashflow_adjusted_cumulative_pnl=cumulative_pnl,
        ),
        config,
    )

    close_reasons: list[str] = []
    kill_reasons: list[str] = []
    generated_at = observation.generated_at_utc
    account_age = generated_at - account.observed_at_utc
    market_age = generated_at - observation.market_observed_at_utc
    orders_age = generated_at - account.orders_observed_at_utc
    if not account.account_complete:
        close_reasons.append("account_source_incomplete")
    if not observation.market_complete:
        close_reasons.append("market_source_incomplete")
    if not account.orders_complete:
        close_reasons.append("open_orders_source_incomplete")
    if account_age > MAXIMUM_SOURCE_AGE:
        close_reasons.append("account_source_incomplete")
    if market_age > MAXIMUM_SOURCE_AGE:
        close_reasons.append("mark_price_stale")
    if orders_age > MAXIMUM_SOURCE_AGE:
        close_reasons.append("open_orders_stale")
    if (
        account_age < -MAXIMUM_FUTURE_CLOCK_SKEW
        or market_age < -MAXIMUM_FUTURE_CLOCK_SKEW
        or orders_age < -MAXIMUM_FUTURE_CLOCK_SKEW
    ):
        close_reasons.append("source_clock_skew")
    if max(-daily_pnl, ZERO) / state.daily_boundary.opening_nav >= config.daily_loss_fraction:
        close_reasons.append("daily_loss_limit_reached")
    if max(-weekly_pnl, ZERO) / state.weekly_boundary.opening_nav >= config.weekly_loss_fraction:
        close_reasons.append("weekly_loss_limit_reached")
    if observation.external_cash_flows or state.external_cash_flow_review_pending:
        close_reasons.append("external_cash_flow_pending_review")

    if account.accounting_currency != config.accounting_currency:
        kill_reasons.append("accounting_currency_mismatch")
    if account.known_liabilities > ZERO:
        kill_reasons.append("unknown_liability")
    if account.unexplained_balance_difference != ZERO:
        kill_reasons.append("unexplained_balance_difference")
    if not account.runtime_reconciled:
        kill_reasons.append("unreconciled_order_or_position")
    if drawdown >= config.drawdown_fraction:
        kill_reasons.append("drawdown_limit_reached")
    if absolute_loss.kill_switch and account.accounting_currency == config.accounting_currency:
        kill_reasons.append("absolute_loss_limit_reached")
    if observation.manual_kill_switch:
        kill_reasons.append("manual_kill_switch")

    # reason code 固定顺序并去重，保证相同观测生成相同快照和 snapshot id。
    close_reasons = list(dict.fromkeys(close_reasons))
    kill_reasons = list(dict.fromkeys(kill_reasons))
    if kill_reasons:
        risk_state = RiskState.KILLED_MANUAL_REVIEW
        reason_codes = kill_reasons + close_reasons
        manual_review_required = True
    elif close_reasons:
        risk_state = RiskState.CLOSE_ONLY
        reason_codes = close_reasons
        manual_review_required = any(
            reason in {"weekly_loss_limit_reached", "external_cash_flow_pending_review"}
            for reason in reason_codes
        )
    else:
        risk_state = RiskState.ENTRY_ALLOWED
        reason_codes = ["risk_checks_passed"]
        manual_review_required = False

    entry_allowed = risk_state is RiskState.ENTRY_ALLOWED
    kill_switch = risk_state is RiskState.KILLED_MANUAL_REVIEW
    payload: dict[str, Any] = {
        "schema_version": 2,
        "producer_version": producer_version,
        "generated_at_utc": _timestamp_text(generated_at),
        "expires_at_utc": _timestamp_text(generated_at + SNAPSHOT_TTL),
        "account_id": account.account_id,
        # 根字段表达快照采用的会计合同；实际账户币种不匹配通过 reason code 进入 Kill。
        "accounting_currency": config.accounting_currency,
        "risk_config_sha256": risk_config_sha256,
        "instrument_registry_sha256": instrument_registry.source_sha256,
        "market_capability_snapshot_sha256": market_capabilities.source_sha256,
        "source_freshness": {
            "account_observed_at_utc": _timestamp_text(account.observed_at_utc),
            "market_observed_at_utc": _timestamp_text(observation.market_observed_at_utc),
            "orders_observed_at_utc": _timestamp_text(account.orders_observed_at_utc),
            "maximum_source_age_seconds": 30,
            "maximum_future_clock_skew_seconds": 5,
            "account_complete": account.account_complete,
            "market_complete": observation.market_complete,
            "orders_complete": account.orders_complete,
        },
        "accounting": {
            "quote_cash": _decimal_text(account.quote_cash),
            "positions": positions,
            "accrued_fees": _decimal_text(account.accrued_fees),
            "known_liabilities": _decimal_text(account.known_liabilities),
            "futures_unrealized_pnl_quote": _decimal_text(futures_unrealized_pnl),
            "accrued_funding_quote": _decimal_text(accrued_funding),
            "nav": _decimal_text(nav),
            "approved_capital_baseline": _decimal_text(state.approved_capital_baseline),
            "cumulative_net_external_cash_flow": _decimal_text(cumulative_cash_flow),
            "cashflow_adjusted_cumulative_pnl": _decimal_text(cumulative_pnl),
            "daily_opening_nav": _decimal_text(state.daily_boundary.opening_nav),
            "daily_net_external_cash_flow": _decimal_text(daily_cash_flow),
            "daily_pnl": _decimal_text(daily_pnl),
            "weekly_opening_nav": _decimal_text(state.weekly_boundary.opening_nav),
            "weekly_net_external_cash_flow": _decimal_text(weekly_cash_flow),
            "weekly_pnl": _decimal_text(weekly_pnl),
            "cashflow_adjusted_high_water_mark": _decimal_text(adjusted_hwm),
            "drawdown_fraction": _decimal_text(drawdown),
            "unexplained_balance_difference": _decimal_text(account.unexplained_balance_difference),
        },
        "open_orders": open_orders,
        "exposure": {
            "open_exposure_quote": _decimal_text(open_exposure),
            "spot_open_exposure_quote": _decimal_text(spot_exposure),
            "futures_long_notional_quote": _decimal_text(futures_long_notional),
            "futures_short_notional_quote": _decimal_text(futures_short_notional),
            "pending_entry_exposure_quote": _decimal_text(pending_entry_exposure),
            "pending_spot_entry_exposure_quote": _decimal_text(pending_spot),
            "pending_futures_long_entry_exposure_quote": _decimal_text(pending_futures_long),
            "pending_futures_short_entry_exposure_quote": _decimal_text(pending_futures_short),
            "available_balance_quote": _decimal_text(account.available_balance_quote),
            "available_margin_quote": _decimal_text(account.available_margin_quote),
            "used_margin_quote": _decimal_text(account.used_margin_quote),
        },
        "thresholds": {
            "trade_risk_fraction": _decimal_text(config.risk_fraction),
            "daily_loss_fraction": _decimal_text(config.daily_loss_fraction),
            "weekly_loss_fraction": _decimal_text(config.weekly_loss_fraction),
            "drawdown_fraction": _decimal_text(config.drawdown_fraction),
            "maximum_absolute_loss_fraction": _decimal_text(config.maximum_absolute_loss_fraction),
            "maximum_absolute_loss": _decimal_text(config.maximum_absolute_loss),
            "effective_absolute_loss_limit": _decimal_text(
                absolute_loss.effective_loss_limit
                if absolute_loss.effective_loss_limit > ZERO
                else min(
                    state.approved_capital_baseline * config.maximum_absolute_loss_fraction,
                    config.maximum_absolute_loss,
                )
            ),
        },
        "decision": {
            "state": risk_state.value,
            "entry_allowed": entry_allowed,
            "close_only": not entry_allowed,
            "kill_switch": kill_switch,
            "cancel_pending_entries": not entry_allowed,
            "safe_exit_allowed": True,
            "manual_review_required": manual_review_required,
            "reason_codes": reason_codes,
        },
    }
    identity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    payload["snapshot_id"] = f"risk-{generated_at.strftime('%Y%m%dT%H%M%SZ')}-{identity}"
    validate_snapshot_payload(
        payload,
        allowed_pairs=allowed_spot_pairs,
        allowed_futures_pairs=allowed_futures_pairs,
        expected_registry_sha256=instrument_registry.source_sha256,
        expected_capability_sha256=market_capabilities.source_sha256,
    )
    return payload


def risk_config_sha256(path: str | Path) -> str:
    """计算实际风险配置文件 hash，快照不得接受调用方臆造的配置版本。"""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require_mapping(value: object, *, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], *, location: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(f"{location} fields do not match schema v2")


def _parse_timestamp(value: object, *, location: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{location} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError(f"{location} must be a UTC timestamp") from error
    _require_utc(parsed, field_name=location)
    return parsed


def _parse_decimal(
    value: object,
    *,
    location: str,
    nonnegative: bool = False,
    positive: bool = False,
) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise ValueError(f"{location} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{location} must be a decimal string") from error
    if not parsed.is_finite():
        raise ValueError(f"{location} must be finite")
    if nonnegative and parsed < ZERO:
        raise ValueError(f"{location} must not be negative")
    if positive and parsed <= ZERO:
        raise ValueError(f"{location} must be positive")
    return parsed


def validate_snapshot_payload(
    payload: Mapping[str, Any],
    *,
    allowed_pairs: Collection[str] | None = None,
    allowed_futures_pairs: Collection[str] | None = None,
    expected_registry_sha256: str | None = None,
    expected_capability_sha256: str | None = None,
    maximum_futures_leverage: Mapping[str, Decimal] | None = None,
) -> None:
    """不依赖运行时第三方库，严格复核 schema v2 的结构和关键公式。"""

    root = _require_mapping(payload, location="snapshot")
    _require_exact_keys(
        root,
        {
            "schema_version",
            "snapshot_id",
            "producer_version",
            "generated_at_utc",
            "expires_at_utc",
            "account_id",
            "accounting_currency",
            "risk_config_sha256",
            "instrument_registry_sha256",
            "market_capability_snapshot_sha256",
            "source_freshness",
            "accounting",
            "open_orders",
            "exposure",
            "thresholds",
            "decision",
        },
        location="snapshot",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 2:
        raise ValueError("schema_version must be 2")
    if not isinstance(root["snapshot_id"], str) or not re.fullmatch(
        r"risk-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", root["snapshot_id"]
    ):
        raise ValueError("snapshot_id does not match schema v2")
    if not isinstance(root["producer_version"], str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", root["producer_version"]
    ):
        raise ValueError("producer_version does not match schema v2")
    generated_at = _parse_timestamp(root["generated_at_utc"], location="generated_at_utc")
    expires_at = _parse_timestamp(root["expires_at_utc"], location="expires_at_utc")
    if expires_at != generated_at + SNAPSHOT_TTL:
        raise ValueError("expires_at_utc must equal generated_at_utc plus 60 seconds")
    if not isinstance(root["account_id"], str) or not 1 <= len(root["account_id"]) <= 128:
        raise ValueError("account_id does not match schema v2")
    if root["accounting_currency"] != "USDT":
        raise ValueError("accounting_currency must be USDT")
    if not isinstance(root["risk_config_sha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", root["risk_config_sha256"]
    ):
        raise ValueError("risk_config_sha256 must be a lowercase SHA-256")
    registry_sha = root["instrument_registry_sha256"]
    capability_sha = root["market_capability_snapshot_sha256"]
    if not isinstance(registry_sha, str) or re.fullmatch(r"[a-f0-9]{64}", registry_sha) is None:
        raise ValueError("instrument_registry_sha256 must be a lowercase SHA-256")
    if not isinstance(capability_sha, str) or re.fullmatch(r"[a-f0-9]{64}", capability_sha) is None:
        raise ValueError("market_capability_snapshot_sha256 must be a lowercase SHA-256")
    if expected_registry_sha256 is not None and registry_sha != expected_registry_sha256:
        raise ValueError("RiskSnapshot does not match Instrument Registry")
    if expected_capability_sha256 is not None and capability_sha != expected_capability_sha256:
        raise ValueError("RiskSnapshot does not match Market Capability snapshot")

    freshness = _require_mapping(root["source_freshness"], location="source_freshness")
    _require_exact_keys(
        freshness,
        {
            "account_observed_at_utc",
            "market_observed_at_utc",
            "orders_observed_at_utc",
            "maximum_source_age_seconds",
            "maximum_future_clock_skew_seconds",
            "account_complete",
            "market_complete",
            "orders_complete",
        },
        location="source_freshness",
    )
    account_observed_at = _parse_timestamp(
        freshness["account_observed_at_utc"], location="account_observed_at_utc"
    )
    market_observed_at = _parse_timestamp(
        freshness["market_observed_at_utc"], location="market_observed_at_utc"
    )
    orders_observed_at = _parse_timestamp(
        freshness["orders_observed_at_utc"], location="orders_observed_at_utc"
    )
    if freshness["maximum_source_age_seconds"] != 30:
        raise ValueError("maximum_source_age_seconds must be 30")
    if freshness["maximum_future_clock_skew_seconds"] != 5:
        raise ValueError("maximum_future_clock_skew_seconds must be 5")
    if (
        type(freshness["account_complete"]) is not bool
        or type(freshness["market_complete"]) is not bool
        or type(freshness["orders_complete"]) is not bool
    ):
        raise ValueError("source completeness flags must be booleans")

    accounting = _require_mapping(root["accounting"], location="accounting")
    _require_exact_keys(
        accounting,
        {
            "quote_cash",
            "positions",
            "accrued_fees",
            "known_liabilities",
            "futures_unrealized_pnl_quote",
            "accrued_funding_quote",
            "nav",
            "approved_capital_baseline",
            "cumulative_net_external_cash_flow",
            "cashflow_adjusted_cumulative_pnl",
            "daily_opening_nav",
            "daily_net_external_cash_flow",
            "daily_pnl",
            "weekly_opening_nav",
            "weekly_net_external_cash_flow",
            "weekly_pnl",
            "cashflow_adjusted_high_water_mark",
            "drawdown_fraction",
            "unexplained_balance_difference",
        },
        location="accounting",
    )
    if not isinstance(accounting["positions"], list) or len(accounting["positions"]) > 100:
        raise ValueError("accounting.positions must be a list with at most 100 positions")
    supported_spot = (
        _normalize_allowed_pairs(allowed_pairs, market=MarketKind.SPOT)
        if allowed_pairs is not None
        else None
    )
    supported_futures = (
        _normalize_allowed_pairs(allowed_futures_pairs, market=MarketKind.FUTURES)
        if allowed_futures_pairs is not None
        else None
    )
    spot_total = ZERO
    futures_long_total = ZERO
    futures_short_total = ZERO
    futures_pnl_total = ZERO
    funding_total = ZERO
    used_margin_total = ZERO
    seen_positions: set[tuple[str, str, str]] = set()
    for index, raw_position in enumerate(accounting["positions"]):
        position = _require_mapping(raw_position, location=f"positions[{index}]")
        market = position.get("market")
        pair = position["pair"]
        instrument_id = position.get("instrument_id")
        side = position.get("side")
        if not isinstance(pair, str) or instrument_id != _instrument_id_for_pair(pair):
            raise ValueError("position instrument_id and pair are inconsistent")
        position_key = (str(market), pair, str(side))
        if position_key in seen_positions:
            raise ValueError("snapshot positions must be unique by market, pair and side")
        seen_positions.add(position_key)
        if market == "spot":
            _require_exact_keys(
                position,
                {
                    "instrument_id",
                    "market",
                    "pair",
                    "side",
                    "quantity",
                    "best_bid",
                    "last_trade",
                    "conservative_exit_mark",
                    "marked_value",
                },
                location=f"positions[{index}]",
            )
            if (
                SPOT_PAIR_PATTERN.fullmatch(pair) is None
                or side != "long"
                or (supported_spot is not None and pair not in supported_spot)
            ):
                raise ValueError("snapshot spot position is not configured")
            quantity = _parse_decimal(
                position["quantity"], location=f"{pair}.quantity", positive=True
            )
            best_bid = _parse_decimal(
                position["best_bid"], location=f"{pair}.best_bid", positive=True
            )
            last_trade = _parse_decimal(
                position["last_trade"], location=f"{pair}.last_trade", positive=True
            )
            mark = _parse_decimal(
                position["conservative_exit_mark"],
                location=f"{pair}.conservative_exit_mark",
                positive=True,
            )
            marked_value = _parse_decimal(
                position["marked_value"], location=f"{pair}.marked_value", nonnegative=True
            )
            if mark != min(best_bid, last_trade) or marked_value != quantity * mark:
                raise ValueError("spot position mark or marked value formula mismatch")
            spot_total += marked_value
        elif market == "linear_perpetual":
            _require_exact_keys(
                position,
                {
                    "instrument_id",
                    "market",
                    "pair",
                    "side",
                    "quantity",
                    "entry_price",
                    "mark_price",
                    "liquidation_price",
                    "leverage",
                    "notional_quote",
                    "position_margin_quote",
                    "maintenance_margin_quote",
                    "unrealized_pnl_quote",
                    "funding_rate",
                    "accrued_funding_quote",
                    "next_funding_at_utc",
                    "liquidation_buffer_fraction",
                },
                location=f"positions[{index}]",
            )
            if (
                FUTURES_PAIR_PATTERN.fullmatch(pair) is None
                or side not in {"long", "short"}
                or (supported_futures is not None and pair not in supported_futures)
            ):
                raise ValueError("snapshot futures position is not configured")
            quantity = _parse_decimal(
                position["quantity"], location=f"{pair}.quantity", positive=True
            )
            entry_price = _parse_decimal(
                position["entry_price"], location=f"{pair}.entry_price", positive=True
            )
            mark_price = _parse_decimal(
                position["mark_price"], location=f"{pair}.mark_price", positive=True
            )
            liquidation_price = _parse_decimal(
                position["liquidation_price"],
                location=f"{pair}.liquidation_price",
                positive=True,
            )
            leverage = _parse_decimal(
                position["leverage"], location=f"{pair}.leverage", positive=True
            )
            if maximum_futures_leverage is not None and (
                pair not in maximum_futures_leverage or leverage > maximum_futures_leverage[pair]
            ):
                raise ValueError("futures leverage exceeds effective market capability")
            notional = _parse_decimal(
                position["notional_quote"], location=f"{pair}.notional_quote", positive=True
            )
            margin = _parse_decimal(
                position["position_margin_quote"],
                location=f"{pair}.position_margin_quote",
                positive=True,
            )
            _parse_decimal(
                position["maintenance_margin_quote"],
                location=f"{pair}.maintenance_margin_quote",
                nonnegative=True,
            )
            unrealized = _parse_decimal(
                position["unrealized_pnl_quote"], location=f"{pair}.unrealized_pnl_quote"
            )
            _parse_decimal(position["funding_rate"], location=f"{pair}.funding_rate")
            funding = _parse_decimal(
                position["accrued_funding_quote"], location=f"{pair}.accrued_funding_quote"
            )
            next_funding = _parse_timestamp(
                position["next_funding_at_utc"], location=f"{pair}.next_funding_at_utc"
            )
            buffer = _parse_decimal(
                position["liquidation_buffer_fraction"],
                location=f"{pair}.liquidation_buffer_fraction",
                positive=True,
            )
            expected_pnl = (
                (mark_price - entry_price) * quantity
                if side == "long"
                else (entry_price - mark_price) * quantity
            )
            expected_buffer = (
                (mark_price - liquidation_price) / mark_price
                if side == "long"
                else (liquidation_price - mark_price) / mark_price
            )
            if (
                notional != quantity * mark_price
                or unrealized != expected_pnl
                or buffer != expected_buffer
                or buffer <= ZERO
                or next_funding <= generated_at
            ):
                raise ValueError("futures position formula or timing mismatch")
            futures_pnl_total += unrealized
            funding_total += funding
            used_margin_total += margin
            if side == "long":
                futures_long_total += notional
            else:
                futures_short_total += notional
        else:
            raise ValueError("position market is unsupported")

    quote_cash = _parse_decimal(accounting["quote_cash"], location="quote_cash", nonnegative=True)
    fees = _parse_decimal(accounting["accrued_fees"], location="accrued_fees", nonnegative=True)
    liabilities = _parse_decimal(
        accounting["known_liabilities"], location="known_liabilities", nonnegative=True
    )
    reported_futures_pnl = _parse_decimal(
        accounting["futures_unrealized_pnl_quote"], location="futures_unrealized_pnl_quote"
    )
    reported_funding = _parse_decimal(
        accounting["accrued_funding_quote"], location="accrued_funding_quote"
    )
    nav = _parse_decimal(accounting["nav"], location="nav", positive=True)
    baseline = _parse_decimal(
        accounting["approved_capital_baseline"],
        location="approved_capital_baseline",
        positive=True,
    )
    cumulative_flow = _parse_decimal(
        accounting["cumulative_net_external_cash_flow"],
        location="cumulative_net_external_cash_flow",
    )
    cumulative_pnl = _parse_decimal(
        accounting["cashflow_adjusted_cumulative_pnl"],
        location="cashflow_adjusted_cumulative_pnl",
    )
    daily_open = _parse_decimal(
        accounting["daily_opening_nav"], location="daily_opening_nav", positive=True
    )
    daily_flow = _parse_decimal(
        accounting["daily_net_external_cash_flow"], location="daily_net_external_cash_flow"
    )
    daily_pnl = _parse_decimal(accounting["daily_pnl"], location="daily_pnl")
    weekly_open = _parse_decimal(
        accounting["weekly_opening_nav"], location="weekly_opening_nav", positive=True
    )
    weekly_flow = _parse_decimal(
        accounting["weekly_net_external_cash_flow"], location="weekly_net_external_cash_flow"
    )
    weekly_pnl = _parse_decimal(accounting["weekly_pnl"], location="weekly_pnl")
    hwm = _parse_decimal(
        accounting["cashflow_adjusted_high_water_mark"],
        location="cashflow_adjusted_high_water_mark",
        positive=True,
    )
    drawdown = _parse_decimal(
        accounting["drawdown_fraction"], location="drawdown_fraction", nonnegative=True
    )
    unexplained = _parse_decimal(
        accounting["unexplained_balance_difference"],
        location="unexplained_balance_difference",
    )
    if reported_futures_pnl != futures_pnl_total or reported_funding != funding_total:
        raise ValueError("futures PnL or funding aggregate mismatch")
    if nav != quote_cash + spot_total + futures_pnl_total + funding_total - fees - liabilities:
        raise ValueError("NAV formula mismatch")
    if cumulative_pnl != nav - baseline - cumulative_flow:
        raise ValueError("cumulative PnL formula mismatch")
    if daily_pnl != nav - daily_open - daily_flow:
        raise ValueError("daily PnL formula mismatch")
    if weekly_pnl != nav - weekly_open - weekly_flow:
        raise ValueError("weekly PnL formula mismatch")
    if hwm < nav or drawdown != max(Decimal("1") - nav / hwm, ZERO) or drawdown > 1:
        raise ValueError("drawdown formula mismatch")

    raw_orders = root["open_orders"]
    if not isinstance(raw_orders, list) or len(raw_orders) > 200:
        raise ValueError("open_orders must be a list with at most 200 orders")
    pending_spot = ZERO
    pending_futures_long = ZERO
    pending_futures_short = ZERO
    seen_order_ids: set[str] = set()
    for index, raw_order in enumerate(raw_orders):
        order = _require_mapping(raw_order, location=f"open_orders[{index}]")
        _require_exact_keys(
            order,
            {
                "order_id",
                "instrument_id",
                "market",
                "pair",
                "side",
                "position_side",
                "intent",
                "order_type",
                "quantity",
                "filled_quantity",
                "remaining_quantity",
                "reference_price",
                "trigger_price",
                "remaining_notional_quote",
                "reduce_only",
                "created_at_utc",
                "updated_at_utc",
            },
            location=f"open_orders[{index}]",
        )
        order_id = order["order_id"]
        pair = order["pair"]
        market = order["market"]
        position_side = order["position_side"]
        intent = order["intent"]
        if (
            not isinstance(order_id, str)
            or not 1 <= len(order_id) <= 128
            or order_id in seen_order_ids
        ):
            raise ValueError("open order ids must be unique non-empty strings")
        seen_order_ids.add(order_id)
        if not isinstance(pair, str) or order["instrument_id"] != _instrument_id_for_pair(pair):
            raise ValueError("open order instrument_id and pair are inconsistent")
        if market == "spot":
            if (
                SPOT_PAIR_PATTERN.fullmatch(pair) is None
                or position_side != "long"
                or (supported_spot is not None and pair not in supported_spot)
            ):
                raise ValueError("spot open order is not configured")
        elif market == "linear_perpetual":
            if (
                FUTURES_PAIR_PATTERN.fullmatch(pair) is None
                or position_side not in {"long", "short"}
                or (supported_futures is not None and pair not in supported_futures)
            ):
                raise ValueError("futures open order is not configured")
        else:
            raise ValueError("open order market is unsupported")
        if order["side"] not in {"buy", "sell"}:
            raise ValueError("open order side is unsupported")
        if intent not in {"entry", "reduce", "stop_loss", "take_profit"}:
            raise ValueError("open order intent is unsupported")
        if order["order_type"] not in {"limit", "market", "stop_limit", "stop_market"}:
            raise ValueError("open order type is unsupported")
        if type(order["reduce_only"]) is not bool or order["reduce_only"] != (intent != "entry"):
            raise ValueError("open order reduce_only is inconsistent with intent")
        expected_side = "buy" if (intent == "entry") == (position_side == "long") else "sell"
        if order["side"] != expected_side:
            raise ValueError("open order side is inconsistent with intent and position_side")
        quantity = _parse_decimal(order["quantity"], location=f"{order_id}.quantity", positive=True)
        filled = _parse_decimal(
            order["filled_quantity"], location=f"{order_id}.filled_quantity", nonnegative=True
        )
        remaining = _parse_decimal(
            order["remaining_quantity"], location=f"{order_id}.remaining_quantity", positive=True
        )
        reference = _parse_decimal(
            order["reference_price"], location=f"{order_id}.reference_price", positive=True
        )
        remaining_notional = _parse_decimal(
            order["remaining_notional_quote"],
            location=f"{order_id}.remaining_notional_quote",
            positive=True,
        )
        if (
            filled >= quantity
            or remaining != quantity - filled
            or remaining_notional != remaining * reference
        ):
            raise ValueError("open order remaining quantity or notional formula mismatch")
        trigger = order["trigger_price"]
        protection = intent in {"stop_loss", "take_profit"}
        if trigger is None:
            if protection or str(order["order_type"]).startswith("stop"):
                raise ValueError("conditional or protection order requires trigger_price")
        else:
            _parse_decimal(trigger, location=f"{order_id}.trigger_price", positive=True)
        created = _parse_timestamp(order["created_at_utc"], location=f"{order_id}.created_at_utc")
        updated = _parse_timestamp(order["updated_at_utc"], location=f"{order_id}.updated_at_utc")
        if not created <= updated <= generated_at:
            raise ValueError("open order timestamps are invalid")
        if intent == "entry":
            if market == "spot":
                pending_spot += remaining_notional
            elif position_side == "long":
                pending_futures_long += remaining_notional
            else:
                pending_futures_short += remaining_notional

    exposure = _require_mapping(root["exposure"], location="exposure")
    _require_exact_keys(
        exposure,
        {
            "open_exposure_quote",
            "spot_open_exposure_quote",
            "futures_long_notional_quote",
            "futures_short_notional_quote",
            "pending_entry_exposure_quote",
            "pending_spot_entry_exposure_quote",
            "pending_futures_long_entry_exposure_quote",
            "pending_futures_short_entry_exposure_quote",
            "available_balance_quote",
            "available_margin_quote",
            "used_margin_quote",
        },
        location="exposure",
    )
    if (
        _parse_decimal(
            exposure["open_exposure_quote"], location="open_exposure_quote", nonnegative=True
        )
        != spot_total + futures_long_total + futures_short_total
    ):
        raise ValueError("open exposure formula mismatch")
    if (
        _parse_decimal(
            exposure["spot_open_exposure_quote"],
            location="spot_open_exposure_quote",
            nonnegative=True,
        )
        != spot_total
    ):
        raise ValueError("spot exposure formula mismatch")
    if (
        _parse_decimal(
            exposure["futures_long_notional_quote"],
            location="futures_long_notional_quote",
            nonnegative=True,
        )
        != futures_long_total
        or _parse_decimal(
            exposure["futures_short_notional_quote"],
            location="futures_short_notional_quote",
            nonnegative=True,
        )
        != futures_short_total
    ):
        raise ValueError("futures exposure formula mismatch")
    pending_total = _parse_decimal(
        exposure["pending_entry_exposure_quote"],
        location="pending_entry_exposure_quote",
        nonnegative=True,
    )
    if (
        _parse_decimal(
            exposure["pending_spot_entry_exposure_quote"],
            location="pending_spot_entry_exposure_quote",
            nonnegative=True,
        )
        != pending_spot
        or _parse_decimal(
            exposure["pending_futures_long_entry_exposure_quote"],
            location="pending_futures_long_entry_exposure_quote",
            nonnegative=True,
        )
        != pending_futures_long
        or _parse_decimal(
            exposure["pending_futures_short_entry_exposure_quote"],
            location="pending_futures_short_entry_exposure_quote",
            nonnegative=True,
        )
        != pending_futures_short
        or pending_total != (pending_spot + pending_futures_long + pending_futures_short)
    ):
        raise ValueError("pending entry exposure formula mismatch")
    _parse_decimal(
        exposure["available_balance_quote"],
        location="available_balance_quote",
        nonnegative=True,
    )
    _parse_decimal(
        exposure["available_margin_quote"], location="available_margin_quote", nonnegative=True
    )
    if (
        _parse_decimal(
            exposure["used_margin_quote"], location="used_margin_quote", nonnegative=True
        )
        != used_margin_total
    ):
        raise ValueError("used margin formula mismatch")

    thresholds = _require_mapping(root["thresholds"], location="thresholds")
    _require_exact_keys(
        thresholds,
        {
            "trade_risk_fraction",
            "daily_loss_fraction",
            "weekly_loss_fraction",
            "drawdown_fraction",
            "maximum_absolute_loss_fraction",
            "maximum_absolute_loss",
            "effective_absolute_loss_limit",
        },
        location="thresholds",
    )
    expected_thresholds = {
        "trade_risk_fraction": "0.0025",
        "daily_loss_fraction": "0.01",
        "weekly_loss_fraction": "0.03",
        "drawdown_fraction": "0.05",
        "maximum_absolute_loss_fraction": "0.10",
        "maximum_absolute_loss": "45",
    }
    if any(thresholds[key] != value for key, value in expected_thresholds.items()):
        raise ValueError("risk thresholds do not match schema v2")
    effective_limit = _parse_decimal(
        thresholds["effective_absolute_loss_limit"],
        location="effective_absolute_loss_limit",
        positive=True,
    )
    if effective_limit != min(baseline * Decimal("0.10"), Decimal("45")):
        raise ValueError("effective absolute loss limit formula mismatch")

    decision = _require_mapping(root["decision"], location="decision")
    _require_exact_keys(
        decision,
        {
            "state",
            "entry_allowed",
            "close_only",
            "kill_switch",
            "cancel_pending_entries",
            "safe_exit_allowed",
            "manual_review_required",
            "reason_codes",
        },
        location="decision",
    )
    boolean_fields = {
        key: decision[key]
        for key in (
            "entry_allowed",
            "close_only",
            "kill_switch",
            "cancel_pending_entries",
            "safe_exit_allowed",
            "manual_review_required",
        )
    }
    if any(type(value) is not bool for value in boolean_fields.values()):
        raise ValueError("decision flags must be booleans")
    if decision["safe_exit_allowed"] is not True:
        raise ValueError("safe exits must always remain allowed")
    if not isinstance(decision["reason_codes"], list) or not decision["reason_codes"]:
        raise ValueError("decision reason_codes must be a non-empty list")
    reasons = decision["reason_codes"]
    if any(not isinstance(reason, str) for reason in reasons) or len(set(reasons)) != len(reasons):
        raise ValueError("decision reason_codes must be unique strings")
    close_reasons = {
        "daily_loss_limit_reached",
        "weekly_loss_limit_reached",
        "external_cash_flow_pending_review",
        "account_source_incomplete",
        "market_source_incomplete",
        "mark_price_missing",
        "mark_price_stale",
        "open_orders_source_incomplete",
        "open_orders_stale",
        "source_clock_skew",
    }
    kill_reasons = {
        "drawdown_limit_reached",
        "absolute_loss_limit_reached",
        "unexplained_balance_difference",
        "accounting_currency_mismatch",
        "unknown_liability",
        "unreconciled_order_or_position",
        "manual_kill_switch",
    }
    allowed_reasons = close_reasons | kill_reasons | {"risk_checks_passed"}
    if not set(reasons).issubset(allowed_reasons):
        raise ValueError("decision contains an unsupported reason code")
    state_value = decision["state"]
    if state_value == RiskState.ENTRY_ALLOWED.value:
        if boolean_fields != {
            "entry_allowed": True,
            "close_only": False,
            "kill_switch": False,
            "cancel_pending_entries": False,
            "safe_exit_allowed": True,
            "manual_review_required": False,
        } or reasons != ["risk_checks_passed"]:
            raise ValueError("ENTRY_ALLOWED decision flags are inconsistent")
        if (
            not freshness["account_complete"]
            or not freshness["market_complete"]
            or not freshness["orders_complete"]
        ):
            raise ValueError("ENTRY_ALLOWED requires complete sources")
    elif state_value == RiskState.CLOSE_ONLY.value:
        if (
            decision["entry_allowed"]
            or not decision["close_only"]
            or decision["kill_switch"]
            or not decision["cancel_pending_entries"]
            or not set(reasons).issubset(close_reasons)
        ):
            raise ValueError("CLOSE_ONLY decision flags or reasons are inconsistent")
        needs_review = bool(
            set(reasons) & {"weekly_loss_limit_reached", "external_cash_flow_pending_review"}
        )
        if decision["manual_review_required"] != needs_review:
            raise ValueError("CLOSE_ONLY manual review flag is inconsistent")
    elif state_value == RiskState.KILLED_MANUAL_REVIEW.value:
        if (
            decision["entry_allowed"]
            or not decision["close_only"]
            or not decision["kill_switch"]
            or not decision["cancel_pending_entries"]
            or not decision["manual_review_required"]
            or not set(reasons).intersection(kill_reasons)
        ):
            raise ValueError("KILLED_MANUAL_REVIEW decision is inconsistent")
    else:
        raise ValueError("decision state is unsupported")

    account_age = generated_at - account_observed_at
    market_age = generated_at - market_observed_at
    orders_age = generated_at - orders_observed_at
    required_reasons: set[str] = set()
    if not freshness["account_complete"] or account_age > MAXIMUM_SOURCE_AGE:
        required_reasons.add("account_source_incomplete")
    if not freshness["market_complete"]:
        required_reasons.add("market_source_incomplete")
    if market_age > MAXIMUM_SOURCE_AGE:
        required_reasons.add("mark_price_stale")
    if not freshness["orders_complete"]:
        required_reasons.add("open_orders_source_incomplete")
    if orders_age > MAXIMUM_SOURCE_AGE:
        required_reasons.add("open_orders_stale")
    if (
        account_age < -MAXIMUM_FUTURE_CLOCK_SKEW
        or market_age < -MAXIMUM_FUTURE_CLOCK_SKEW
        or orders_age < -MAXIMUM_FUTURE_CLOCK_SKEW
    ):
        required_reasons.add("source_clock_skew")
    if max(-daily_pnl, ZERO) / daily_open >= Decimal("0.01"):
        required_reasons.add("daily_loss_limit_reached")
    if max(-weekly_pnl, ZERO) / weekly_open >= Decimal("0.03"):
        required_reasons.add("weekly_loss_limit_reached")
    if drawdown >= Decimal("0.05"):
        required_reasons.add("drawdown_limit_reached")
    if max(-cumulative_pnl, ZERO) >= effective_limit:
        required_reasons.add("absolute_loss_limit_reached")
    if unexplained != ZERO:
        required_reasons.add("unexplained_balance_difference")
    if liabilities > ZERO:
        required_reasons.add("unknown_liability")
    if not required_reasons.issubset(set(reasons)):
        raise ValueError("decision omits a triggered risk reason")
    identity_document = dict(root)
    identity_document.pop("snapshot_id")
    expected_identity = hashlib.sha256(
        json.dumps(identity_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    if not str(root["snapshot_id"]).endswith(expected_identity):
        raise ValueError("snapshot_id content hash mismatch")


def atomic_publish_snapshot(payload: Mapping[str, Any], destination: str | Path) -> None:
    """同目录写临时文件，完整 fsync 后原子替换目标。"""

    validate_snapshot_payload(payload)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(
        f".{destination_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    )
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary_path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination_path)
        if os.name != "nt":
            # POSIX 需要同步目录项；Windows 的原子 replace 已在文件关闭后完成，
            # 目录不能按此方式打开。
            directory_fd = os.open(destination_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def _fail_closed(reason_code: str) -> SnapshotReadResult:
    return SnapshotReadResult(
        snapshot=None,
        entry_allowed=False,
        close_only=True,
        kill_switch=False,
        safe_exit_allowed=True,
        reason_codes=(reason_code,),
    )


def load_risk_snapshot(
    path: str | Path,
    *,
    now_utc: datetime,
    allowed_pairs: Collection[str] | None = None,
    allowed_futures_pairs: Collection[str] | None = None,
    expected_registry_sha256: str | None = None,
    expected_capability_sha256: str | None = None,
    maximum_futures_leverage: Mapping[str, Decimal] | None = None,
) -> SnapshotReadResult:
    """读取并复核发布快照；任何不确定性都只阻止入场，不阻塞安全退出。"""

    _require_utc(now_utc, field_name="now_utc")
    snapshot_path = Path(path)
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _fail_closed("snapshot_missing")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _fail_closed("snapshot_corrupt")
    if not isinstance(raw, dict):
        return _fail_closed("snapshot_corrupt")
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != 2:
        return _fail_closed("schema_version_unsupported")
    try:
        validate_snapshot_payload(
            raw,
            allowed_pairs=allowed_pairs,
            allowed_futures_pairs=allowed_futures_pairs,
            expected_registry_sha256=expected_registry_sha256,
            expected_capability_sha256=expected_capability_sha256,
            maximum_futures_leverage=maximum_futures_leverage,
        )
        generated_at = _parse_timestamp(raw["generated_at_utc"], location="generated_at_utc")
        expires_at = _parse_timestamp(raw["expires_at_utc"], location="expires_at_utc")
    except (KeyError, TypeError, ValueError):
        return _fail_closed("snapshot_corrupt")
    if generated_at - now_utc > MAXIMUM_FUTURE_CLOCK_SKEW:
        return _fail_closed("snapshot_clock_skew")
    if now_utc >= expires_at:
        return _fail_closed("snapshot_stale")

    decision = raw["decision"]
    assert isinstance(decision, dict)
    reasons = decision["reason_codes"]
    assert isinstance(reasons, list)
    return SnapshotReadResult(
        snapshot=raw,
        entry_allowed=decision["entry_allowed"],
        close_only=decision["close_only"],
        kill_switch=decision["kill_switch"],
        safe_exit_allowed=True,
        reason_codes=tuple(reasons),
    )
