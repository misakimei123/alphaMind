"""风险阈值配置的严格解析与校验。"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RiskLimitsConfig:
    schema_version: int
    accounting_currency: str
    risk_fraction: Decimal
    maximum_absolute_loss_fraction: Decimal
    maximum_absolute_loss: Decimal
    daily_loss_fraction: Decimal
    weekly_loss_fraction: Decimal
    drawdown_fraction: Decimal


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], *, location: str) -> None:
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ValueError(f"{location} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{location} contains unknown keys: {', '.join(sorted(unknown))}")


def _require_table(value: object, *, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be a TOML table")
    return value


def _require_decimal_string(table: Mapping[str, Any], key: str, *, location: str) -> Decimal:
    raw_value = table[key]
    # TOML float 会先经过二进制浮点解析；风险阈值必须使用字符串以保持十进制精确性。
    if not isinstance(raw_value, str):
        raise ValueError(f"{location}.{key} must be a decimal string")
    try:
        value = Decimal(raw_value)
    except InvalidOperation as error:
        raise ValueError(f"{location}.{key} must be a valid decimal string") from error
    if not value.is_finite():
        raise ValueError(f"{location}.{key} must be finite")
    return value


def load_risk_limits(path: str | Path) -> RiskLimitsConfig:
    """加载风险配置；缺失、拼写错误和未知字段一律拒绝。"""

    config_path = Path(path)
    with config_path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    _require_exact_keys(
        raw,
        {"schema_version", "accounting_currency", "trade_limits", "loss_limits"},
        location="root",
    )
    if raw["schema_version"] != 1:
        raise ValueError("schema_version must be 1")

    accounting_currency = raw["accounting_currency"]
    if (
        not isinstance(accounting_currency, str)
        or not accounting_currency.isascii()
        or not accounting_currency.isalnum()
        or accounting_currency != accounting_currency.upper()
    ):
        raise ValueError("accounting_currency must be an uppercase ASCII asset code")

    trade_limits = _require_table(raw["trade_limits"], location="trade_limits")
    loss_limits = _require_table(raw["loss_limits"], location="loss_limits")
    _require_exact_keys(trade_limits, {"risk_fraction"}, location="trade_limits")
    _require_exact_keys(
        loss_limits,
        {
            "maximum_absolute_loss_fraction",
            "maximum_absolute_loss",
            "daily_loss_fraction",
            "weekly_loss_fraction",
            "drawdown_fraction",
        },
        location="loss_limits",
    )

    config = RiskLimitsConfig(
        schema_version=1,
        accounting_currency=accounting_currency,
        risk_fraction=_require_decimal_string(
            trade_limits, "risk_fraction", location="trade_limits"
        ),
        maximum_absolute_loss_fraction=_require_decimal_string(
            loss_limits, "maximum_absolute_loss_fraction", location="loss_limits"
        ),
        maximum_absolute_loss=_require_decimal_string(
            loss_limits, "maximum_absolute_loss", location="loss_limits"
        ),
        daily_loss_fraction=_require_decimal_string(
            loss_limits, "daily_loss_fraction", location="loss_limits"
        ),
        weekly_loss_fraction=_require_decimal_string(
            loss_limits, "weekly_loss_fraction", location="loss_limits"
        ),
        drawdown_fraction=_require_decimal_string(
            loss_limits, "drawdown_fraction", location="loss_limits"
        ),
    )

    if config.maximum_absolute_loss <= 0:
        raise ValueError("loss_limits.maximum_absolute_loss must be positive")
    for name in (
        "risk_fraction",
        "maximum_absolute_loss_fraction",
        "daily_loss_fraction",
        "weekly_loss_fraction",
        "drawdown_fraction",
    ):
        value = getattr(config, name)
        if not Decimal("0") < value <= Decimal("1"):
            raise ValueError(f"{name} must be in (0, 1]")

    return config
