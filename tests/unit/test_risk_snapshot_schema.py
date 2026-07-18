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
        "schema_version": 2,
        "snapshot_id": "risk-20260716T000000Z-0123456789ab",
        "producer_version": "0.1.0",
        "generated_at_utc": "2026-07-16T00:00:00Z",
        "expires_at_utc": "2026-07-16T00:01:00Z",
        "account_id": "paper-primary",
        "accounting_currency": "USDT",
        "risk_config_sha256": "0" * 64,
        "instrument_registry_sha256": "1" * 64,
        "market_capability_snapshot_sha256": "2" * 64,
        "source_freshness": {
            "account_observed_at_utc": "2026-07-15T23:59:55Z",
            "market_observed_at_utc": "2026-07-15T23:59:58Z",
            "orders_observed_at_utc": "2026-07-15T23:59:57Z",
            "maximum_source_age_seconds": 30,
            "maximum_future_clock_skew_seconds": 5,
            "account_complete": True,
            "market_complete": True,
            "orders_complete": True,
        },
        "accounting": {
            "quote_cash": "500",
            "positions": [],
            "accrued_fees": "0",
            "known_liabilities": "0",
            "futures_unrealized_pnl_quote": "0",
            "accrued_funding_quote": "0",
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
        "open_orders": [],
        "exposure": {
            "open_exposure_quote": "0",
            "spot_open_exposure_quote": "0",
            "futures_long_notional_quote": "0",
            "futures_short_notional_quote": "0",
            "pending_entry_exposure_quote": "0",
            "pending_spot_entry_exposure_quote": "0",
            "pending_futures_long_entry_exposure_quote": "0",
            "pending_futures_short_entry_exposure_quote": "0",
            "available_balance_quote": "500",
            "available_margin_quote": "500",
            "used_margin_quote": "0",
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
        (
            "KILLED_MANUAL_REVIEW",
            False,
            True,
            True,
            "manual_kill_switch",
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


def test_position_pair_is_registry_extensible_not_asset_enumerated(
    validator: jsonschema.Draft202012Validator,
) -> None:
    snapshot = valid_snapshot()
    accounting = snapshot["accounting"]
    assert isinstance(accounting, dict)
    accounting["positions"] = [
        {
            "instrument_id": "SOL",
            "market": "spot",
            "pair": "SOL/USDT",
            "side": "long",
            "quantity": "1",
            "best_bid": "100",
            "last_trade": "101",
            "conservative_exit_mark": "100",
            "marked_value": "100",
        }
    ]
    accounting["quote_cash"] = "400"

    validator.validate(snapshot)


def test_futures_position_and_open_order_contracts(
    validator: jsonschema.Draft202012Validator,
) -> None:
    snapshot = valid_snapshot()
    accounting = snapshot["accounting"]
    exposure = snapshot["exposure"]
    assert isinstance(accounting, dict) and isinstance(exposure, dict)
    accounting["positions"] = [
        {
            "instrument_id": "SOL",
            "market": "linear_perpetual",
            "pair": "SOL/USDT:USDT",
            "side": "short",
            "quantity": "1",
            "entry_price": "110",
            "mark_price": "100",
            "liquidation_price": "150",
            "leverage": "2",
            "notional_quote": "100",
            "position_margin_quote": "50",
            "maintenance_margin_quote": "2",
            "unrealized_pnl_quote": "10",
            "funding_rate": "0.0001",
            "accrued_funding_quote": "-1",
            "next_funding_at_utc": "2026-07-16T08:00:00Z",
            "liquidation_buffer_fraction": "0.5",
        }
    ]
    accounting["quote_cash"] = "491"
    accounting["futures_unrealized_pnl_quote"] = "10"
    accounting["accrued_funding_quote"] = "-1"
    exposure.update(
        {
            "open_exposure_quote": "100",
            "futures_short_notional_quote": "100",
            "available_margin_quote": "450",
            "used_margin_quote": "50",
        }
    )
    snapshot["open_orders"] = [
        {
            "order_id": "order-1",
            "instrument_id": "SOL",
            "market": "linear_perpetual",
            "pair": "SOL/USDT:USDT",
            "side": "buy",
            "position_side": "short",
            "intent": "stop_loss",
            "order_type": "stop_market",
            "quantity": "1",
            "filled_quantity": "0",
            "remaining_quantity": "1",
            "reference_price": "105",
            "trigger_price": "105",
            "remaining_notional_quote": "105",
            "reduce_only": True,
            "created_at_utc": "2026-07-15T23:50:00Z",
            "updated_at_utc": "2026-07-15T23:59:00Z",
        }
    ]

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
