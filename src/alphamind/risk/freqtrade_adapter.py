"""P3-02 Freqtrade callback 使用的确定性风险适配层。"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from alphamind.risk.position_sizing import (
    PositionSizeContext,
    PositionSizeDecision,
    PositionSizeRequest,
    RiskContextSource,
    calculate_position_size,
)
from alphamind.risk.watchdog import SnapshotReadResult

ZERO = Decimal("0")
ONE = Decimal("1")
SUPPORTED_PAIRS = frozenset({"BTC/USDT", "ETH/USDT"})


@dataclass(frozen=True, slots=True)
class PairConstraint:
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal


@dataclass(frozen=True, slots=True)
class FreqtradeRiskConfig:
    snapshot_path: Path
    atr_period: int
    stop_multiple: Decimal
    maximum_holding_time_enabled: bool
    volatility_cap_fraction: Decimal
    symbol_exposure_fraction: Decimal
    directional_exposure_fraction: Decimal
    maximum_unit_loss_fraction: Decimal
    gap_buffer_rate: Decimal
    fee_rate_per_side: Decimal
    half_spread_rate: Decimal
    slippage_rate_per_side: Decimal
    pairs: Mapping[str, PairConstraint]


@dataclass(frozen=True, slots=True)
class RuntimeEntryApproval:
    """callback 之间传递的只读批准结果，不是第二套可执行订单。"""

    pair: str
    snapshot_id: str
    expires_at_utc: datetime
    reference_rate: Decimal
    signal_atr: Decimal
    approved_quantity: Decimal
    approved_stake: Decimal
    position_context: PositionSizeContext
    position_decision: PositionSizeDecision


def _require_mapping(value: object, *, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{location} must be a table")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], *, location: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{location} keys must be exactly {sorted(expected)}")


def _decimal_string(
    table: Mapping[str, Any],
    key: str,
    *,
    location: str,
    allow_zero: bool = False,
) -> Decimal:
    raw = table.get(key)
    if not isinstance(raw, str):
        raise TypeError(f"{location}.{key} must be a decimal string")
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{location}.{key} must be a decimal string") from error
    if not value.is_finite() or value < ZERO or (not allow_zero and value == ZERO):
        raise ValueError(f"{location}.{key} must be finite and positive")
    return value


def load_freqtrade_risk_config(path: str | Path) -> FreqtradeRiskConfig:
    """一次性加载并严格校验运行时适配配置；callback 不重复读取文件。"""

    config_path = Path(path)
    with config_path.open("rb") as stream:
        root = tomllib.load(stream)
    _require_exact_keys(
        root,
        {"schema_version", "snapshot_path", "strategy", "risk", "costs", "pairs"},
        location="config",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ValueError("config.schema_version must be 1")
    snapshot_path = root["snapshot_path"]
    if not isinstance(snapshot_path, str) or not snapshot_path.strip():
        raise ValueError("config.snapshot_path must be a non-empty path")

    strategy = _require_mapping(root["strategy"], location="strategy")
    _require_exact_keys(
        strategy,
        {"atr_period", "stop_multiple", "maximum_holding_time_enabled"},
        location="strategy",
    )
    if type(strategy["atr_period"]) is not int or strategy["atr_period"] != 20:
        raise ValueError("strategy.atr_period must remain frozen at 20")
    if strategy["maximum_holding_time_enabled"] is not False:
        raise ValueError("strategy maximum holding time must remain disabled")

    risk = _require_mapping(root["risk"], location="risk")
    _require_exact_keys(
        risk,
        {
            "volatility_cap_fraction",
            "symbol_exposure_fraction",
            "directional_exposure_fraction",
            "maximum_unit_loss_fraction",
            "gap_buffer_rate",
        },
        location="risk",
    )
    costs = _require_mapping(root["costs"], location="costs")
    _require_exact_keys(
        costs,
        {"fee_rate_per_side", "half_spread_rate", "slippage_rate_per_side"},
        location="costs",
    )

    raw_pairs = _require_mapping(root["pairs"], location="pairs")
    if set(raw_pairs) != SUPPORTED_PAIRS:
        raise ValueError("pairs must contain exactly BTC/USDT and ETH/USDT")
    pairs: dict[str, PairConstraint] = {}
    for pair in sorted(SUPPORTED_PAIRS):
        raw_pair = _require_mapping(raw_pairs[pair], location=f"pairs.{pair}")
        _require_exact_keys(
            raw_pair,
            {"price_tick", "quantity_step", "minimum_quantity", "minimum_notional"},
            location=f"pairs.{pair}",
        )
        pairs[pair] = PairConstraint(
            price_tick=_decimal_string(raw_pair, "price_tick", location=f"pairs.{pair}"),
            quantity_step=_decimal_string(raw_pair, "quantity_step", location=f"pairs.{pair}"),
            minimum_quantity=_decimal_string(
                raw_pair, "minimum_quantity", location=f"pairs.{pair}"
            ),
            minimum_notional=_decimal_string(
                raw_pair, "minimum_notional", location=f"pairs.{pair}"
            ),
        )

    config = FreqtradeRiskConfig(
        snapshot_path=Path(snapshot_path),
        atr_period=strategy["atr_period"],
        stop_multiple=_decimal_string(strategy, "stop_multiple", location="strategy"),
        maximum_holding_time_enabled=False,
        volatility_cap_fraction=_decimal_string(risk, "volatility_cap_fraction", location="risk"),
        symbol_exposure_fraction=_decimal_string(risk, "symbol_exposure_fraction", location="risk"),
        directional_exposure_fraction=_decimal_string(
            risk, "directional_exposure_fraction", location="risk"
        ),
        maximum_unit_loss_fraction=_decimal_string(
            risk, "maximum_unit_loss_fraction", location="risk"
        ),
        gap_buffer_rate=_decimal_string(risk, "gap_buffer_rate", location="risk"),
        fee_rate_per_side=_decimal_string(costs, "fee_rate_per_side", location="costs"),
        half_spread_rate=_decimal_string(costs, "half_spread_rate", location="costs"),
        slippage_rate_per_side=_decimal_string(costs, "slippage_rate_per_side", location="costs"),
        pairs=pairs,
    )
    for field_name in (
        "volatility_cap_fraction",
        "symbol_exposure_fraction",
        "directional_exposure_fraction",
        "maximum_unit_loss_fraction",
    ):
        if getattr(config, field_name) > ONE:
            raise ValueError(f"{field_name} must not exceed 1")
    return config


def _parse_decimal(value: object, *, location: str) -> Decimal:
    if not isinstance(value, str):
        raise TypeError(f"{location} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{location} must be a decimal string") from error
    if not parsed.is_finite():
        raise ValueError(f"{location} must be finite")
    return parsed


def _align_price(price: Decimal, tick: Decimal, *, upward: bool) -> Decimal:
    rounding = ROUND_CEILING if upward else ROUND_FLOOR
    return (price / tick).to_integral_value(rounding=rounding) * tick


def calculate_runtime_entry_approval(
    snapshot_result: SnapshotReadResult,
    config: FreqtradeRiskConfig,
    *,
    pair: str,
    current_rate: Decimal,
    signal_atr: Decimal,
    min_stake: Decimal | None,
    max_stake: Decimal,
) -> RuntimeEntryApproval | None:
    """从已验证快照构造 P2-03 同源请求，并返回 quote stake 批准结果。"""

    if not snapshot_result.entry_allowed or snapshot_result.snapshot is None:
        return None
    if pair not in config.pairs:
        return None
    for name, value in {
        "current_rate": current_rate,
        "signal_atr": signal_atr,
        "max_stake": max_stake,
    }.items():
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be Decimal")
        if not value.is_finite() or value <= ZERO:
            raise ValueError(f"{name} must be finite and positive")
    if min_stake is not None and (
        not isinstance(min_stake, Decimal) or not min_stake.is_finite() or min_stake < ZERO
    ):
        raise ValueError("min_stake must be a nonnegative Decimal or None")

    snapshot = snapshot_result.snapshot
    accounting = _require_mapping(snapshot["accounting"], location="snapshot.accounting")
    exposure = _require_mapping(snapshot["exposure"], location="snapshot.exposure")
    thresholds = _require_mapping(snapshot["thresholds"], location="snapshot.thresholds")
    positions = accounting["positions"]
    if not isinstance(positions, list):
        raise TypeError("snapshot.accounting.positions must be a list")

    constraint = config.pairs[pair]
    entry_price = _align_price(current_rate, constraint.price_tick, upward=True)
    stop_price = _align_price(
        entry_price - signal_atr * config.stop_multiple,
        constraint.price_tick,
        upward=False,
    )
    if stop_price <= ZERO or stop_price >= entry_price:
        return None

    nav = _parse_decimal(accounting["nav"], location="snapshot.accounting.nav")
    open_exposure = _parse_decimal(
        exposure["open_exposure_quote"], location="snapshot.exposure.open_exposure_quote"
    )
    pending_exposure = _parse_decimal(
        exposure["pending_entry_exposure_quote"],
        location="snapshot.exposure.pending_entry_exposure_quote",
    )
    available_balance = _parse_decimal(
        exposure["available_balance_quote"],
        location="snapshot.exposure.available_balance_quote",
    )
    current_symbol_exposure = ZERO
    for raw_position in positions:
        position = _require_mapping(raw_position, location="snapshot.accounting.positions[]")
        if position.get("pair") == pair:
            current_symbol_exposure += _parse_decimal(
                position["marked_value"], location=f"snapshot.{pair}.marked_value"
            )

    fee_buffer = (entry_price + stop_price) * config.fee_rate_per_side
    slippage_buffer = (entry_price + stop_price) * (
        config.half_spread_rate + config.slippage_rate_per_side
    )
    available_quote = min(available_balance, max_stake)
    minimum_notional = max(constraint.minimum_notional, min_stake or ZERO)
    request = PositionSizeRequest(
        nav=nav,
        risk_fraction=_parse_decimal(
            thresholds["trade_risk_fraction"],
            location="snapshot.thresholds.trade_risk_fraction",
        ),
        entry_price=entry_price,
        stop_price=stop_price,
        minimum_stop_distance=constraint.price_tick,
        fee_buffer_per_unit=fee_buffer,
        slippage_buffer_per_unit=slippage_buffer,
        gap_buffer_per_unit=entry_price * config.gap_buffer_rate,
        maximum_unit_loss=entry_price * config.maximum_unit_loss_fraction,
        volatility_cap_quantity=nav * config.volatility_cap_fraction / entry_price,
        symbol_exposure_limit_quote=nav * config.symbol_exposure_fraction,
        current_symbol_exposure_quote=current_symbol_exposure,
        # v1 快照只有账户级 pending；全部归入当前标的可保证不会低估风险。
        pending_symbol_entry_exposure_quote=pending_exposure,
        directional_exposure_limit_quote=nav * config.directional_exposure_fraction,
        current_directional_exposure_quote=open_exposure,
        pending_directional_entry_exposure_quote=pending_exposure,
        available_balance_cap_quantity=available_quote
        / (entry_price * (ONE + config.fee_rate_per_side)),
        price_tick=constraint.price_tick,
        quantity_step=constraint.quantity_step,
        minimum_quantity=constraint.minimum_quantity,
        minimum_notional=minimum_notional,
    )
    snapshot_id = snapshot["snapshot_id"]
    expires_at = snapshot["expires_at_utc"]
    if not isinstance(snapshot_id, str) or not isinstance(expires_at, str):
        raise TypeError("validated snapshot id and expiry must be strings")
    position_context = PositionSizeContext(1, RiskContextSource.RISK_SNAPSHOT, snapshot_id, request)
    decision = calculate_position_size(position_context)
    if not decision.approved:
        return None
    approved_stake = decision.approved_quantity * current_rate
    if approved_stake > max_stake:
        raise RuntimeError("approved stake exceeds Freqtrade max_stake")
    return RuntimeEntryApproval(
        pair=pair,
        snapshot_id=snapshot_id,
        expires_at_utc=datetime.fromisoformat(expires_at.replace("Z", "+00:00")),
        reference_rate=current_rate,
        signal_atr=signal_atr,
        approved_quantity=decision.approved_quantity,
        approved_stake=approved_stake,
        position_context=position_context,
        position_decision=decision,
    )


def calculate_initial_stop_price(
    config: FreqtradeRiskConfig,
    *,
    pair: str,
    average_entry_rate: Decimal,
    signal_atr: Decimal,
) -> Decimal | None:
    """按实际平均成交价和信号 candle ATR 固定初始绝对止损。"""

    if pair not in config.pairs:
        return None
    if any(
        not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO
        for value in (average_entry_rate, signal_atr)
    ):
        return None
    stop_price = _align_price(
        average_entry_rate - signal_atr * config.stop_multiple,
        config.pairs[pair].price_tick,
        upward=False,
    )
    return stop_price if ZERO < stop_price < average_entry_rate else None


def fixed_stoploss_ratio(*, initial_stop_price: Decimal, current_rate: Decimal) -> Decimal | None:
    """把固定绝对止损转换为 Freqtrade 相对当前价格的 callback 返回值。"""

    if any(
        not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO
        for value in (initial_stop_price, current_rate)
    ):
        return None
    if current_rate <= initial_stop_price:
        return ZERO
    return (current_rate - initial_stop_price) / current_rate
