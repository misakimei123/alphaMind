from dataclasses import replace
from decimal import Decimal

import pytest

from alphamind.risk.position_sizing import (
    LimitingCap,
    PositionSizeRequest,
    RejectionReason,
    calculate_position_size,
)


def request(**overrides: Decimal) -> PositionSizeRequest:
    baseline = PositionSizeRequest(
        nav=Decimal("500"),
        risk_fraction=Decimal("0.0025"),
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        minimum_stop_distance=Decimal("0.01"),
        fee_buffer_per_unit=Decimal("0.10"),
        slippage_buffer_per_unit=Decimal("0.10"),
        gap_buffer_per_unit=Decimal("0.30"),
        volatility_cap_quantity=Decimal("10"),
        symbol_exposure_limit_quote=Decimal("1000"),
        current_symbol_exposure_quote=Decimal("0"),
        pending_symbol_entry_exposure_quote=Decimal("0"),
        directional_exposure_limit_quote=Decimal("1000"),
        current_directional_exposure_quote=Decimal("0"),
        pending_directional_entry_exposure_quote=Decimal("0"),
        available_balance_cap_quantity=Decimal("10"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        minimum_notional=Decimal("5"),
    )
    return replace(baseline, **overrides)


def test_risk_budget_and_step_rounding_determine_quantity() -> None:
    decision = calculate_position_size(request())

    assert decision.approved
    assert decision.risk_cash == Decimal("1.2500")
    assert decision.estimated_unit_loss == Decimal("5.50")
    assert decision.approved_quantity == Decimal("0.227")
    assert decision.limiting_cap is LimitingCap.RISK_BUDGET
    assert decision.approved_quantity * decision.estimated_unit_loss <= decision.risk_cash


def test_tighter_cap_never_increases_quantity() -> None:
    baseline = calculate_position_size(request())
    capped = calculate_position_size(request(volatility_cap_quantity=Decimal("0.1239")))

    assert capped.approved_quantity == Decimal("0.123")
    assert capped.approved_quantity < baseline.approved_quantity
    assert capped.limiting_cap is LimitingCap.VOLATILITY


@pytest.mark.parametrize(
    ("overrides", "expected_cap"),
    [
        (
            {
                "symbol_exposure_limit_quote": Decimal("10"),
            },
            LimitingCap.SYMBOL_EXPOSURE,
        ),
        (
            {
                "directional_exposure_limit_quote": Decimal("10"),
            },
            LimitingCap.DIRECTIONAL_EXPOSURE,
        ),
        (
            {
                "available_balance_cap_quantity": Decimal("0.1"),
            },
            LimitingCap.AVAILABLE_BALANCE,
        ),
    ],
)
def test_each_exposure_cap_can_only_reduce_quantity(
    overrides: dict[str, Decimal], expected_cap: LimitingCap
) -> None:
    baseline = calculate_position_size(request())
    capped = calculate_position_size(request(**overrides))

    assert capped.approved_quantity <= baseline.approved_quantity
    assert capped.limiting_cap is expected_cap


def test_pending_entry_uses_quote_normalized_exposure_capacity() -> None:
    decision = calculate_position_size(
        request(
            symbol_exposure_limit_quote=Decimal("100"),
            current_symbol_exposure_quote=Decimal("40"),
            pending_symbol_entry_exposure_quote=Decimal("50"),
            directional_exposure_limit_quote=Decimal("200"),
            current_directional_exposure_quote=Decimal("50"),
            pending_directional_entry_exposure_quote=Decimal("50"),
        )
    )

    assert decision.approved_quantity == Decimal("0.100")
    assert decision.limiting_cap is LimitingCap.SYMBOL_EXPOSURE


def test_no_capacity_and_minimum_notional_are_rejected() -> None:
    no_capacity = calculate_position_size(request(available_balance_cap_quantity=Decimal("0")))
    below_notional = calculate_position_size(
        request(
            volatility_cap_quantity=Decimal("0.04"),
            minimum_notional=Decimal("5"),
        )
    )

    assert no_capacity.approved_quantity == Decimal("0")
    assert no_capacity.rejection_reason is RejectionReason.NO_CAPACITY
    assert below_notional.approved_quantity == Decimal("0")
    assert below_notional.rejection_reason is RejectionReason.BELOW_MINIMUM_NOTIONAL


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"stop_price": Decimal("100")}, "stop_price"),
        (
            {
                "stop_price": Decimal("99.995"),
                "minimum_stop_distance": Decimal("0.01"),
            },
            "below the configured minimum",
        ),
        ({"nav": Decimal("NaN")}, "finite"),
        ({"fee_buffer_per_unit": Decimal("-0.01")}, "must not be negative"),
    ],
)
def test_invalid_inputs_are_rejected(overrides: dict[str, Decimal], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        calculate_position_size(request(**overrides))


def test_float_inputs_are_rejected_to_preserve_determinism() -> None:
    invalid = replace(request(), nav=500.0)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="nav must be Decimal"):
        calculate_position_size(invalid)
