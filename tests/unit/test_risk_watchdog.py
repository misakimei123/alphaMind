from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from alphamind.config.risk_limits import load_risk_limits
from alphamind.risk.watchdog import (
    WATCHDOG_TARGET_INTERVAL,
    AccountRuntimeObservation,
    CashFlowKind,
    ExternalCashFlow,
    PeriodBoundary,
    PositionObservation,
    RiskAccountingState,
    WatchdogObservation,
    atomic_publish_snapshot,
    build_risk_snapshot,
    load_risk_snapshot,
    risk_config_sha256,
)

PROJECT_ROOT = Path(__file__).parents[2]
RISK_CONFIG = PROJECT_ROOT / "configs" / "common" / "risk-limits.toml"
GENERATED_AT = datetime(2026, 7, 17, 12, tzinfo=UTC)


def _observation(
    *,
    quote_cash: Decimal = Decimal("300"),
    accrued_fees: Decimal = Decimal("0"),
    known_liabilities: Decimal = Decimal("0"),
    unexplained_balance_difference: Decimal = Decimal("0"),
    account_currency: str = "USDT",
    account_observed_at: datetime | None = None,
    market_observed_at: datetime | None = None,
    account_complete: bool = True,
    market_complete: bool = True,
    runtime_reconciled: bool = True,
    daily_opening_nav: Decimal = Decimal("500"),
    weekly_opening_nav: Decimal = Decimal("500"),
    high_water_mark: Decimal = Decimal("500"),
    baseline: Decimal = Decimal("500"),
    cash_flows: tuple[ExternalCashFlow, ...] = (),
    manual_kill_switch: bool = False,
    position: PositionObservation | None = None,
) -> WatchdogObservation:
    if position is None:
        position = PositionObservation(
            pair="BTC/USDT",
            base_quantity=Decimal("0.01"),
            best_bid=Decimal("20000"),
            last_trade=Decimal("20010"),
        )
    return WatchdogObservation(
        generated_at_utc=GENERATED_AT,
        market_observed_at_utc=market_observed_at or GENERATED_AT - timedelta(seconds=2),
        market_complete=market_complete,
        account=AccountRuntimeObservation(
            account_id="paper-primary",
            accounting_currency=account_currency,
            observed_at_utc=account_observed_at or GENERATED_AT - timedelta(seconds=5),
            quote_cash=quote_cash,
            available_balance_quote=quote_cash,
            positions=(position,),
            accrued_fees=accrued_fees,
            known_liabilities=known_liabilities,
            unexplained_balance_difference=unexplained_balance_difference,
            pending_entry_exposure_quote=Decimal("20"),
            account_complete=account_complete,
            runtime_reconciled=runtime_reconciled,
        ),
        accounting_state=RiskAccountingState(
            approved_capital_baseline=baseline,
            cumulative_external_cash_flow_before=Decimal("0"),
            daily_external_cash_flow_before=Decimal("0"),
            weekly_external_cash_flow_before=Decimal("0"),
            cashflow_adjusted_high_water_mark_before=high_water_mark,
            daily_boundary=PeriodBoundary(
                observed_at_utc=datetime(2026, 7, 17, tzinfo=UTC),
                opening_nav=daily_opening_nav,
            ),
            weekly_boundary=PeriodBoundary(
                observed_at_utc=datetime(2026, 7, 13, tzinfo=UTC),
                opening_nav=weekly_opening_nav,
            ),
            external_cash_flow_review_pending=bool(cash_flows),
        ),
        external_cash_flows=cash_flows,
        manual_kill_switch=manual_kill_switch,
    )


def _build(observation: WatchdogObservation) -> dict[str, object]:
    return build_risk_snapshot(
        observation,
        load_risk_limits(RISK_CONFIG),
        risk_config_sha256=risk_config_sha256(RISK_CONFIG),
        producer_version="0.1.0",
    )


def _decision(snapshot: dict[str, object]) -> dict[str, object]:
    decision = snapshot["decision"]
    assert isinstance(decision, dict)
    return decision


def _accounting(snapshot: dict[str, object]) -> dict[str, object]:
    accounting = snapshot["accounting"]
    assert isinstance(accounting, dict)
    return accounting


def test_builds_entry_allowed_snapshot_with_conservative_marks_and_exact_hash() -> None:
    snapshot = _build(_observation())

    accounting = _accounting(snapshot)
    assert accounting["nav"] == "500.00"
    assert accounting["cashflow_adjusted_cumulative_pnl"] == "0"
    assert snapshot["risk_config_sha256"] == risk_config_sha256(RISK_CONFIG)
    assert snapshot["expires_at_utc"] == "2026-07-17T12:01:00Z"
    assert timedelta(seconds=15) == WATCHDOG_TARGET_INTERVAL
    assert _decision(snapshot) == {
        "state": "ENTRY_ALLOWED",
        "entry_allowed": True,
        "close_only": False,
        "kill_switch": False,
        "cancel_pending_entries": False,
        "safe_exit_allowed": True,
        "manual_review_required": False,
        "reason_codes": ["risk_checks_passed"],
    }


@pytest.mark.parametrize(
    ("kind", "amount", "quote_cash", "expected_hwm"),
    [
        (CashFlowKind.DEPOSIT, Decimal("10"), Decimal("310"), "510"),
        (CashFlowKind.WITHDRAWAL, Decimal("-10"), Decimal("290"), "490"),
        (CashFlowKind.REBATE, Decimal("2"), Decimal("302"), "502"),
        (CashFlowKind.REWARD, Decimal("3"), Decimal("303"), "503"),
    ],
)
def test_external_cash_flows_are_excluded_from_strategy_pnl_and_require_review(
    kind: CashFlowKind,
    amount: Decimal,
    quote_cash: Decimal,
    expected_hwm: str,
) -> None:
    flow = ExternalCashFlow(
        event_id=f"flow-{kind.value}",
        occurred_at_utc=GENERATED_AT - timedelta(minutes=1),
        kind=kind,
        amount=amount,
    )

    snapshot = _build(_observation(quote_cash=quote_cash, cash_flows=(flow,)))

    accounting = _accounting(snapshot)
    assert accounting["cashflow_adjusted_cumulative_pnl"] == "0"
    assert accounting["daily_pnl"] == "0"
    assert accounting["weekly_pnl"] == "0"
    assert accounting["cashflow_adjusted_high_water_mark"] == expected_hwm
    decision = _decision(snapshot)
    assert decision["state"] == "CLOSE_ONLY"
    assert decision["manual_review_required"] is True
    assert decision["reason_codes"] == ["external_cash_flow_pending_review"]


def test_nav_includes_unrealized_mark_and_accrued_fees() -> None:
    position = PositionObservation(
        pair="BTC/USDT",
        base_quantity=Decimal("0.01"),
        best_bid=Decimal("19000"),
        last_trade=Decimal("19100"),
    )

    snapshot = _build(_observation(position=position, accrued_fees=Decimal("1")))

    accounting = _accounting(snapshot)
    assert accounting["nav"] == "489.00"
    assert accounting["daily_pnl"] == "-11.00"
    assert _decision(snapshot)["reason_codes"] == ["daily_loss_limit_reached"]


def test_pending_cash_flow_review_remains_close_only_after_event_cycle() -> None:
    observation = _observation(quote_cash=Decimal("310"), high_water_mark=Decimal("510"))
    pending_state = replace(
        observation.accounting_state,
        cumulative_external_cash_flow_before=Decimal("10"),
        daily_external_cash_flow_before=Decimal("10"),
        weekly_external_cash_flow_before=Decimal("10"),
        external_cash_flow_review_pending=True,
    )

    snapshot = _build(replace(observation, accounting_state=pending_state))

    assert _accounting(snapshot)["cashflow_adjusted_cumulative_pnl"] == "0"
    assert _decision(snapshot)["reason_codes"] == ["external_cash_flow_pending_review"]


@pytest.mark.parametrize(
    ("quote_cash", "daily_open", "weekly_open", "reason"),
    [
        (Decimal("295"), Decimal("500"), Decimal("495"), "daily_loss_limit_reached"),
        (Decimal("285"), Decimal("485"), Decimal("500"), "weekly_loss_limit_reached"),
    ],
)
def test_daily_and_weekly_loss_boundaries_trigger_at_exact_threshold(
    quote_cash: Decimal,
    daily_open: Decimal,
    weekly_open: Decimal,
    reason: str,
) -> None:
    snapshot = _build(
        _observation(
            quote_cash=quote_cash,
            daily_opening_nav=daily_open,
            weekly_opening_nav=weekly_open,
        )
    )

    decision = _decision(snapshot)
    assert decision["state"] == "CLOSE_ONLY"
    assert reason in decision["reason_codes"]
    assert decision["safe_exit_allowed"] is True


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (_observation(high_water_mark=Decimal("527")), "drawdown_limit_reached"),
        (_observation(quote_cash=Decimal("255")), "absolute_loss_limit_reached"),
        (_observation(manual_kill_switch=True), "manual_kill_switch"),
        (
            _observation(unexplained_balance_difference=Decimal("0.01")),
            "unexplained_balance_difference",
        ),
        (_observation(known_liabilities=Decimal("1")), "unknown_liability"),
        (_observation(runtime_reconciled=False), "unreconciled_order_or_position"),
        (_observation(account_currency="USD"), "accounting_currency_mismatch"),
    ],
)
def test_kill_conditions_require_manual_review_and_keep_safe_exit(
    observation: WatchdogObservation,
    reason: str,
) -> None:
    snapshot = _build(observation)

    decision = _decision(snapshot)
    assert decision["state"] == "KILLED_MANUAL_REVIEW"
    assert decision["entry_allowed"] is False
    assert decision["kill_switch"] is True
    assert decision["safe_exit_allowed"] is True
    assert reason in decision["reason_codes"]


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (
            _observation(account_observed_at=GENERATED_AT - timedelta(seconds=31)),
            "account_source_incomplete",
        ),
        (
            _observation(market_observed_at=GENERATED_AT - timedelta(seconds=31)),
            "mark_price_stale",
        ),
        (
            _observation(market_observed_at=GENERATED_AT + timedelta(seconds=6)),
            "source_clock_skew",
        ),
        (_observation(account_complete=False), "account_source_incomplete"),
        (_observation(market_complete=False), "market_source_incomplete"),
    ],
)
def test_incomplete_stale_or_future_sources_fail_closed(
    observation: WatchdogObservation,
    reason: str,
) -> None:
    decision = _decision(_build(observation))

    assert decision["state"] == "CLOSE_ONLY"
    assert decision["safe_exit_allowed"] is True
    assert reason in decision["reason_codes"]


def test_boundaries_must_be_exact_utc_day_and_iso_week() -> None:
    observation = _observation()
    invalid_state = replace(
        observation.accounting_state,
        daily_boundary=PeriodBoundary(
            observed_at_utc=datetime(2026, 7, 17, 0, 0, 1, tzinfo=UTC),
            opening_nav=Decimal("500"),
        ),
    )

    with pytest.raises(ValueError, match="daily boundary"):
        _build(replace(observation, accounting_state=invalid_state))


def test_missing_mark_refuses_to_publish_fabricated_nav() -> None:
    invalid_position = PositionObservation(
        pair="BTC/USDT",
        base_quantity=Decimal("0.01"),
        best_bid=Decimal("0"),
        last_trade=Decimal("20000"),
    )

    with pytest.raises(ValueError, match="marks must be positive"):
        _build(_observation(position=invalid_position))


def test_atomic_publish_and_load_round_trip(tmp_path: Path) -> None:
    destination = tmp_path / "runtime" / "risk-snapshot.json"
    snapshot = _build(_observation())

    atomic_publish_snapshot(snapshot, destination)
    result = load_risk_snapshot(destination, now_utc=GENERATED_AT + timedelta(seconds=15))

    assert result.snapshot == snapshot
    assert result.entry_allowed
    assert not result.close_only
    assert result.safe_exit_allowed
    assert list(destination.parent.glob("*.tmp")) == []


def test_failed_replace_preserves_previous_complete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "risk-snapshot.json"
    previous = _build(_observation())
    atomic_publish_snapshot(previous, destination)
    replacement = _build(
        replace(_observation(), generated_at_utc=GENERATED_AT + timedelta(seconds=15))
    )

    def fail_replace(
        source: str | bytes | os.PathLike[str], target: str | bytes | os.PathLike[str]
    ) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        atomic_publish_snapshot(replacement, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == previous
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    ("contents", "now", "expected_reason"),
    [
        (None, GENERATED_AT, "snapshot_missing"),
        ("{partial", GENERATED_AT, "snapshot_corrupt"),
        ('{"schema_version": 2}', GENERATED_AT, "schema_version_unsupported"),
    ],
)
def test_snapshot_read_failures_are_local_fail_closed(
    tmp_path: Path,
    contents: str | None,
    now: datetime,
    expected_reason: str,
) -> None:
    path = tmp_path / "risk-snapshot.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    result = load_risk_snapshot(path, now_utc=now)

    assert result.snapshot is None
    assert not result.entry_allowed
    assert result.close_only
    assert not result.kill_switch
    assert result.safe_exit_allowed
    assert result.reason_codes == (expected_reason,)


def test_stale_snapshot_and_consumer_clock_skew_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "risk-snapshot.json"
    snapshot = _build(_observation())
    atomic_publish_snapshot(snapshot, path)

    stale = load_risk_snapshot(path, now_utc=GENERATED_AT + timedelta(seconds=60))
    clock_skew = load_risk_snapshot(path, now_utc=GENERATED_AT - timedelta(seconds=6))

    assert stale.reason_codes == ("snapshot_stale",)
    assert clock_skew.reason_codes == ("snapshot_clock_skew",)
    assert stale.safe_exit_allowed and clock_skew.safe_exit_allowed


def test_semantically_tampered_snapshot_is_rejected_as_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "risk-snapshot.json"
    snapshot = _build(_observation())
    accounting = _accounting(snapshot)
    accounting["nav"] = "999"
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = load_risk_snapshot(path, now_utc=GENERATED_AT)

    assert result.reason_codes == ("snapshot_corrupt",)
    assert result.safe_exit_allowed
