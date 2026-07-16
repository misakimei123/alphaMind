from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest
import yaml

PROJECT_ROOT = Path(__file__).parents[2]
SCHEMA_PATH = PROJECT_ROOT / "data" / "schemas" / "risk-snapshot.schema.yaml"


@pytest.fixture(scope="module")
def validator() -> jsonschema.Draft202012Validator:
    schema = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def valid_snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "snapshot_id": "risk-20260716T000000Z-0123456789ab",
        "producer_version": "0.1.0",
        "generated_at_utc": "2026-07-16T00:00:00Z",
        "expires_at_utc": "2026-07-16T00:01:00Z",
        "account_id": "paper-primary",
        "accounting_currency": "USDT",
        "risk_config_sha256": "0" * 64,
        "source_freshness": {
            "account_observed_at_utc": "2026-07-15T23:59:55Z",
            "market_observed_at_utc": "2026-07-15T23:59:58Z",
            "maximum_source_age_seconds": 30,
            "maximum_future_clock_skew_seconds": 5,
            "account_complete": True,
            "market_complete": True,
        },
        "accounting": {
            "quote_cash": "500",
            "positions": [],
            "accrued_fees": "0",
            "known_liabilities": "0",
            "nav": "500",
            "approved_capital_baseline": "500",
            "cumulative_net_external_cash_flow": "0",
            "cashflow_adjusted_cumulative_pnl": "0",
            "daily_opening_nav": "500",
            "daily_net_external_cash_flow": "0",
            "daily_pnl": "0",
            "weekly_opening_nav": "500",
            "weekly_net_external_cash_flow": "0",
            "weekly_pnl": "0",
            "cashflow_adjusted_high_water_mark": "500",
            "drawdown_fraction": "0",
            "unexplained_balance_difference": "0",
        },
        "exposure": {
            "open_exposure_quote": "0",
            "pending_entry_exposure_quote": "0",
            "available_balance_quote": "500",
        },
        "thresholds": {
            "trade_risk_fraction": "0.0025",
            "daily_loss_fraction": "0.01",
            "weekly_loss_fraction": "0.03",
            "drawdown_fraction": "0.05",
            "maximum_absolute_loss_fraction": "0.10",
            "maximum_absolute_loss": "45",
            "effective_absolute_loss_limit": "45.00",
        },
        "decision": {
            "state": "ENTRY_ALLOWED",
            "entry_allowed": True,
            "close_only": False,
            "kill_switch": False,
            "cancel_pending_entries": False,
            "safe_exit_allowed": True,
            "manual_review_required": False,
            "reason_codes": ["risk_checks_passed"],
        },
    }


@pytest.mark.parametrize(
    ("state", "entry_allowed", "kill_switch", "manual_review", "reason"),
    [
        ("ENTRY_ALLOWED", True, False, False, "risk_checks_passed"),
        ("CLOSE_ONLY", False, False, False, "daily_loss_limit_reached"),
        (
            "KILLED_MANUAL_REVIEW",
            False,
            True,
            True,
            "absolute_loss_limit_reached",
        ),
    ],
)
def test_each_risk_state_has_a_valid_contract(
    validator: jsonschema.Draft202012Validator,
    state: str,
    entry_allowed: bool,
    kill_switch: bool,
    manual_review: bool,
    reason: str,
) -> None:
    snapshot = valid_snapshot()
    decision = snapshot["decision"]
    assert isinstance(decision, dict)
    decision.update(
        {
            "state": state,
            "entry_allowed": entry_allowed,
            "close_only": not entry_allowed,
            "kill_switch": kill_switch,
            "cancel_pending_entries": not entry_allowed,
            "manual_review_required": manual_review,
            "reason_codes": [reason],
        }
    )

    validator.validate(snapshot)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot.update({"unexpected": True}),
        lambda snapshot: snapshot["accounting"].update({"nav": 500.0}),
        lambda snapshot: snapshot["decision"].update({"safe_exit_allowed": False}),
        lambda snapshot: snapshot["decision"].update({"close_only": True}),
        lambda snapshot: snapshot["source_freshness"].update({"market_complete": False}),
    ],
)
def test_unsafe_or_ambiguous_snapshot_is_rejected(
    validator: jsonschema.Draft202012Validator,
    mutate: object,
) -> None:
    snapshot = deepcopy(valid_snapshot())
    assert callable(mutate)
    mutate(snapshot)

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(snapshot)
