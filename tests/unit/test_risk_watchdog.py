from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from alphamind.config import MarketKind, load_instrument_registry
from alphamind.config.risk_limits import load_risk_limits
from alphamind.market import load_market_capability_snapshot
from alphamind.risk.watchdog import (
    WATCHDOG_TARGET_INTERVAL,
    AccountRuntimeObservation,
    CashFlowKind,
    ExternalCashFlow,
    FuturesPositionObservation,
    OpenOrderObservation,
    OrderIntent,
    OrderSide,
    PeriodBoundary,
    PositionObservation,
    PositionSide,
    RiskAccountingState,
    WatchdogObservation,
    atomic_publish_snapshot,
    build_risk_snapshot,
    load_risk_snapshot,
    risk_config_sha256,
)

PROJECT_ROOT = Path(__file__).parents[2]
RISK_CONFIG = PROJECT_ROOT / "configs" / "common" / "risk-limits.toml"
INSTRUMENT_REGISTRY = load_instrument_registry(
    PROJECT_ROOT / "configs" / "alphamind" / "instruments.example.yaml"
)
SPOT_PAIRS = INSTRUMENT_REGISTRY.enabled_pairs(MarketKind.SPOT)
FUTURES_PAIRS = INSTRUMENT_REGISTRY.enabled_pairs(MarketKind.FUTURES)
MARKET_CAPABILITIES = load_market_capability_snapshot(
    PROJECT_ROOT / "configs" / "alphamind" / "market-capabilities.snapshot.json",
    registry=INSTRUMENT_REGISTRY,
)
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
    orders_complete: bool = True,
    orders_observed_at: datetime | None = None,
    runtime_reconciled: bool = True,
    daily_opening_nav: Decimal = Decimal("500"),
    weekly_opening_nav: Decimal = Decimal("500"),
    high_water_mark: Decimal = Decimal("500"),
    baseline: Decimal = Decimal("500"),
    cash_flows: tuple[ExternalCashFlow, ...] = (),
    manual_kill_switch: bool = False,
    position: PositionObservation | FuturesPositionObservation | None = None,
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
            open_orders=(),
            accrued_fees=accrued_fees,
            known_liabilities=known_liabilities,
            unexplained_balance_difference=unexplained_balance_difference,
            available_margin_quote=quote_cash,
            used_margin_quote=Decimal("0"),
            orders_observed_at_utc=orders_observed_at or GENERATED_AT - timedelta(seconds=4),
            orders_complete=orders_complete,
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
        INSTRUMENT_REGISTRY,
        MARKET_CAPABILITIES,
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


@pytest.mark.parametrize("pair", ["SOL/USDT", "HYPE/USDT"])
def test_registry_spot_pairs_are_accepted_without_code_changes(pair: str) -> None:
    position = PositionObservation(
        pair=pair,
        base_quantity=Decimal("1"),
        best_bid=Decimal("200"),
        last_trade=Decimal("201"),
    )

    snapshot = _build(_observation(position=position))

    assert _accounting(snapshot)["positions"][0]["pair"] == pair


@pytest.mark.parametrize(
    ("side", "entry_price", "mark_price", "liquidation_price"),
    [
        (PositionSide.LONG, Decimal("100"), Decimal("110"), Decimal("60")),
        (PositionSide.SHORT, Decimal("110"), Decimal("100"), Decimal("150")),
    ],
)
def test_futures_long_and_short_include_mark_liquidation_margin_and_funding(
    side: PositionSide,
    entry_price: Decimal,
    mark_price: Decimal,
    liquidation_price: Decimal,
) -> None:
    position = FuturesPositionObservation(
        pair="SOL/USDT:USDT",
        side=side,
        quantity=Decimal("1"),
        entry_price=entry_price,
        mark_price=mark_price,
        liquidation_price=liquidation_price,
        leverage=Decimal("2"),
        position_margin_quote=Decimal("50"),
        maintenance_margin_quote=Decimal("2"),
        unrealized_pnl_quote=Decimal("10"),
        funding_rate=Decimal("0.0001"),
        accrued_funding_quote=Decimal("-1"),
        next_funding_at_utc=GENERATED_AT + timedelta(hours=4),
    )
    observation = _observation(
        quote_cash=Decimal("500"),
        position=position,
        baseline=Decimal("509"),
        daily_opening_nav=Decimal("509"),
        weekly_opening_nav=Decimal("509"),
        high_water_mark=Decimal("509"),
    )
    account = replace(
        observation.account,
        available_balance_quote=Decimal("450"),
        available_margin_quote=Decimal("450"),
        used_margin_quote=Decimal("50"),
    )

    snapshot = _build(replace(observation, account=account))

    accounting = _accounting(snapshot)
    raw_position = accounting["positions"][0]
    assert accounting["nav"] == "509"
    assert accounting["futures_unrealized_pnl_quote"] == "10"
    assert accounting["accrued_funding_quote"] == "-1"
    assert raw_position["market"] == "linear_perpetual"
    assert raw_position["side"] == side.value
    assert Decimal(raw_position["liquidation_buffer_fraction"]) > 0


def test_open_orders_derive_pending_exposure_by_market_and_direction() -> None:
    def order(
        order_id: str,
        pair: str,
        market: MarketKind,
        position_side: PositionSide,
        quantity: str,
        filled: str,
        price: str,
    ) -> OpenOrderObservation:
        return OpenOrderObservation(
            order_id=order_id,
            pair=pair,
            market=market,
            side=(OrderSide.BUY if position_side is PositionSide.LONG else OrderSide.SELL),
            position_side=position_side,
            intent=OrderIntent.ENTRY,
            order_type="limit",
            quantity=Decimal(quantity),
            filled_quantity=Decimal(filled),
            reference_price=Decimal(price),
            reduce_only=False,
            created_at_utc=GENERATED_AT - timedelta(minutes=2),
            updated_at_utc=GENERATED_AT - timedelta(seconds=2),
        )

    orders = (
        order(
            "spot-entry",
            "BTC/USDT",
            MarketKind.SPOT,
            PositionSide.LONG,
            "0.002",
            "0.001",
            "20000",
        ),
        order(
            "futures-long-entry",
            "SOL/USDT:USDT",
            MarketKind.FUTURES,
            PositionSide.LONG,
            "1",
            "0",
            "100",
        ),
        order(
            "futures-short-entry",
            "ETH/USDT:USDT",
            MarketKind.FUTURES,
            PositionSide.SHORT,
            "2",
            "0",
            "50",
        ),
    )
    observation = _observation()
    account = replace(observation.account, open_orders=orders)

    snapshot = _build(replace(observation, account=account))

    exposure = snapshot["exposure"]
    assert exposure["pending_spot_entry_exposure_quote"] == "20.000"
    assert exposure["pending_futures_long_entry_exposure_quote"] == "100"
    assert exposure["pending_futures_short_entry_exposure_quote"] == "100"
    assert exposure["pending_entry_exposure_quote"] == "220.000"
    assert len(snapshot["open_orders"]) == 3


def test_protection_order_is_expressed_but_does_not_add_pending_entry_risk() -> None:
    protection = OpenOrderObservation(
        order_id="spot-stop",
        pair="BTC/USDT",
        market=MarketKind.SPOT,
        side=OrderSide.SELL,
        position_side=PositionSide.LONG,
        intent=OrderIntent.STOP_LOSS,
        order_type="stop_market",
        quantity=Decimal("0.01"),
        filled_quantity=Decimal("0"),
        reference_price=Decimal("18000"),
        trigger_price=Decimal("18000"),
        reduce_only=True,
        created_at_utc=GENERATED_AT - timedelta(minutes=2),
        updated_at_utc=GENERATED_AT - timedelta(seconds=2),
    )
    observation = _observation()
    account = replace(observation.account, open_orders=(protection,))

    snapshot = _build(replace(observation, account=account))

    assert snapshot["exposure"]["pending_entry_exposure_quote"] == "0"
    assert snapshot["open_orders"][0]["intent"] == "stop_loss"
    with pytest.raises(ValueError, match="side is inconsistent"):
        _build(
            replace(
                observation,
                account=replace(account, open_orders=(replace(protection, side=OrderSide.BUY),)),
            )
        )


def test_futures_leverage_and_liquidation_relationship_fail_closed() -> None:
    position = FuturesPositionObservation(
        pair="HYPE/USDT:USDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("10"),
        mark_price=Decimal("11"),
        liquidation_price=Decimal("8"),
        leverage=Decimal("2"),
        position_margin_quote=Decimal("5.5"),
        maintenance_margin_quote=Decimal("0.1"),
        unrealized_pnl_quote=Decimal("1"),
        funding_rate=Decimal("0.0001"),
        accrued_funding_quote=Decimal("0"),
        next_funding_at_utc=GENERATED_AT + timedelta(hours=4),
    )
    observation = _observation(position=position)
    account = replace(observation.account, used_margin_quote=Decimal("5.5"))
    with pytest.raises(ValueError, match="leverage exceeds"):
        _build(replace(observation, account=account))

    valid_leverage = replace(position, leverage=Decimal("1"))
    account = replace(
        account, positions=(replace(valid_leverage, liquidation_price=Decimal("12")),)
    )
    with pytest.raises(ValueError, match="liquidation price"):
        _build(replace(observation, account=account))


def test_position_outside_instrument_registry_is_rejected() -> None:
    position = PositionObservation(
        pair="XRP/USDT",
        base_quantity=Decimal("1"),
        best_bid=Decimal("2"),
        last_trade=Decimal("2.1"),
    )

    with pytest.raises(ValueError, match="Instrument Registry"):
        _build(_observation(position=position))


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
        (_observation(orders_complete=False), "open_orders_source_incomplete"),
        (
            _observation(orders_observed_at=GENERATED_AT - timedelta(seconds=31)),
            "open_orders_stale",
        ),
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
    result = load_risk_snapshot(
        destination,
        now_utc=GENERATED_AT + timedelta(seconds=15),
        allowed_pairs=SPOT_PAIRS,
        allowed_futures_pairs=FUTURES_PAIRS,
        expected_registry_sha256=INSTRUMENT_REGISTRY.source_sha256,
        expected_capability_sha256=MARKET_CAPABILITIES.source_sha256,
    )

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
        ('{"schema_version": 1}', GENERATED_AT, "schema_version_unsupported"),
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

    result = load_risk_snapshot(path, now_utc=now, allowed_pairs=SPOT_PAIRS)

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

    stale = load_risk_snapshot(
        path,
        now_utc=GENERATED_AT + timedelta(seconds=60),
        allowed_pairs=SPOT_PAIRS,
    )
    clock_skew = load_risk_snapshot(
        path,
        now_utc=GENERATED_AT - timedelta(seconds=6),
        allowed_pairs=SPOT_PAIRS,
    )

    assert stale.reason_codes == ("snapshot_stale",)
    assert clock_skew.reason_codes == ("snapshot_clock_skew",)
    assert stale.safe_exit_allowed and clock_skew.safe_exit_allowed


def test_semantically_tampered_snapshot_is_rejected_as_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "risk-snapshot.json"
    snapshot = _build(_observation())
    accounting = _accounting(snapshot)
    accounting["nav"] = "999"
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = load_risk_snapshot(path, now_utc=GENERATED_AT, allowed_pairs=SPOT_PAIRS)

    assert result.reason_codes == ("snapshot_corrupt",)
    assert result.safe_exit_allowed


def test_snapshot_hash_and_config_bindings_reject_tampering(tmp_path: Path) -> None:
    path = tmp_path / "risk-snapshot.json"
    snapshot = _build(_observation())
    snapshot["producer_version"] = "0.1.1"
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    content_tampered = load_risk_snapshot(path, now_utc=GENERATED_AT)
    assert content_tampered.reason_codes == ("snapshot_corrupt",)

    atomic_publish_snapshot(_build(_observation()), path)
    binding_mismatch = load_risk_snapshot(
        path,
        now_utc=GENERATED_AT,
        expected_registry_sha256="0" * 64,
    )
    assert binding_mismatch.reason_codes == ("snapshot_corrupt",)


def test_consumer_rechecks_futures_leverage_against_capability(tmp_path: Path) -> None:
    position = FuturesPositionObservation(
        pair="HYPE/USDT:USDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("10"),
        mark_price=Decimal("11"),
        liquidation_price=Decimal("8"),
        leverage=Decimal("1"),
        position_margin_quote=Decimal("11"),
        maintenance_margin_quote=Decimal("0.1"),
        unrealized_pnl_quote=Decimal("1"),
        funding_rate=Decimal("0.0001"),
        accrued_funding_quote=Decimal("0"),
        next_funding_at_utc=GENERATED_AT + timedelta(hours=4),
    )
    observation = _observation(
        quote_cash=Decimal("500"),
        position=position,
        baseline=Decimal("501"),
        daily_opening_nav=Decimal("501"),
        weekly_opening_nav=Decimal("501"),
        high_water_mark=Decimal("501"),
    )
    account = replace(observation.account, used_margin_quote=Decimal("11"))
    snapshot = _build(replace(observation, account=account))
    snapshot["accounting"]["positions"][0]["leverage"] = "2"
    identity_document = dict(snapshot)
    identity_document.pop("snapshot_id")
    identity = hashlib.sha256(
        json.dumps(identity_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    snapshot["snapshot_id"] = f"risk-20260717T120000Z-{identity}"
    path = tmp_path / "risk-snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = load_risk_snapshot(
        path,
        now_utc=GENERATED_AT,
        allowed_futures_pairs=FUTURES_PAIRS,
        maximum_futures_leverage={"HYPE/USDT:USDT": Decimal("1")},
    )

    assert result.reason_codes == ("snapshot_corrupt",)
