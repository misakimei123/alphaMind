from decimal import Decimal
from pathlib import Path

import pytest

from alphamind.config.risk_limits import load_risk_limits
from alphamind.risk.account_loss import (
    AbsoluteLossBoundary,
    AbsoluteLossReason,
    AccountPnlObservation,
    evaluate_absolute_loss,
)

PROJECT_ROOT = Path(__file__).parents[2]
RISK_CONFIG = PROJECT_ROOT / "configs" / "common" / "risk-limits.toml"


def test_repository_risk_config_loads_exact_values() -> None:
    config = load_risk_limits(RISK_CONFIG)

    assert config.accounting_currency == "USDT"
    assert config.maximum_absolute_loss_fraction == Decimal("0.10")
    assert config.maximum_absolute_loss == Decimal("45")
    assert config.risk_fraction == Decimal("0.0025")
    assert config.daily_loss_fraction == Decimal("0.01")
    assert config.weekly_loss_fraction == Decimal("0.03")
    assert config.drawdown_fraction == Decimal("0.05")


@pytest.mark.parametrize(
    ("pnl", "expected_loss", "expected_remaining"),
    [
        (Decimal("10"), Decimal("0"), Decimal("45")),
        (Decimal("0"), Decimal("0"), Decimal("45")),
        (Decimal("-44.99"), Decimal("44.99"), Decimal("0.01")),
    ],
)
def test_absolute_loss_below_limit_allows_entry(
    pnl: Decimal, expected_loss: Decimal, expected_remaining: Decimal
) -> None:
    config = load_risk_limits(RISK_CONFIG)

    decision = evaluate_absolute_loss(
        AccountPnlObservation("USDT", Decimal("450"), pnl),
        config,
    )

    assert decision.entry_allowed
    assert not decision.kill_switch
    assert decision.absolute_loss == expected_loss
    assert decision.effective_loss_limit == Decimal("45.00")
    assert decision.remaining_loss_capacity == expected_remaining
    assert decision.limiting_boundary is AbsoluteLossBoundary.BOTH
    assert decision.reason is AbsoluteLossReason.BELOW_LIMIT


@pytest.mark.parametrize("pnl", [Decimal("-45"), Decimal("-45.01"), Decimal("-100")])
def test_absolute_loss_at_or_above_limit_triggers_kill_switch(pnl: Decimal) -> None:
    config = load_risk_limits(RISK_CONFIG)

    decision = evaluate_absolute_loss(
        AccountPnlObservation("USDT", Decimal("500"), pnl),
        config,
    )

    assert not decision.entry_allowed
    assert decision.kill_switch
    assert decision.effective_loss_limit == Decimal("45")
    assert decision.remaining_loss_capacity == Decimal("0")
    assert decision.limiting_boundary is AbsoluteLossBoundary.FIXED
    assert decision.reason is AbsoluteLossReason.LIMIT_REACHED


def test_accounting_currency_mismatch_fails_closed() -> None:
    config = load_risk_limits(RISK_CONFIG)

    decision = evaluate_absolute_loss(
        AccountPnlObservation("USD", Decimal("500"), Decimal("-1")),
        config,
    )

    assert not decision.entry_allowed
    assert decision.kill_switch
    assert decision.limiting_boundary is AbsoluteLossBoundary.UNKNOWN
    assert decision.reason is AbsoluteLossReason.CURRENCY_MISMATCH


def test_fraction_boundary_triggers_before_fixed_cap() -> None:
    config = load_risk_limits(RISK_CONFIG)

    decision = evaluate_absolute_loss(
        AccountPnlObservation("USDT", Decimal("100"), Decimal("-10")),
        config,
    )

    assert not decision.entry_allowed
    assert decision.kill_switch
    assert decision.effective_loss_limit == Decimal("10.00")
    assert decision.limiting_boundary is AbsoluteLossBoundary.FRACTION
    assert decision.reason is AbsoluteLossReason.LIMIT_REACHED


def test_numeric_toml_risk_value_is_rejected(tmp_path: Path) -> None:
    invalid_config = tmp_path / "risk-limits.toml"
    invalid_config.write_text(
        """\
schema_version = 1
accounting_currency = "USDT"
[trade_limits]
risk_fraction = "0.0025"
[loss_limits]
maximum_absolute_loss = 45.0
maximum_absolute_loss_fraction = "0.10"
daily_loss_fraction = "0.01"
weekly_loss_fraction = "0.03"
drawdown_fraction = "0.05"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be a decimal string"):
        load_risk_limits(invalid_config)


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    invalid_config = tmp_path / "risk-limits.toml"
    invalid_config.write_text(
        """\
schema_version = 1
accounting_currency = "USDT"
unexpected = true
[trade_limits]
risk_fraction = "0.0025"
[loss_limits]
maximum_absolute_loss = "45"
maximum_absolute_loss_fraction = "0.10"
daily_loss_fraction = "0.01"
weekly_loss_fraction = "0.03"
drawdown_fraction = "0.05"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown keys: unexpected"):
        load_risk_limits(invalid_config)
