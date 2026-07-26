from decimal import Decimal

import pytest

from sports.execution.models import OrderIntentEnvelope, OrderIntentState
from sports.execution.state_machine import (
    OrderIntentStateError,
    OrderIntentStateMachine,
)


def _intent(
    state: OrderIntentState = OrderIntentState.USER_CONFIRMED,
) -> OrderIntentEnvelope:
    return OrderIntentEnvelope(
        intent_id="intent-1",
        market_id="market-1",
        outcome_id="outcome-1",
        state=state,
        requested_odds=-110,
        current_quote_odds=-110,
    )


def test_invalid_state_transitions_are_rejected_and_duplicate_submission_is_idempotent():
    machine = OrderIntentStateMachine()
    with pytest.raises(OrderIntentStateError):
        machine.transition(
            _intent(OrderIntentState.THESIS_DRAFTED), OrderIntentState.RISK_APPROVED
        )

    submitted = machine.submit(_intent(), execution_id="exec-1")
    duplicate = machine.submit(submitted, execution_id="exec-1")

    assert submitted.state == OrderIntentState.EXECUTION_SUBMITTED
    assert duplicate == submitted


def test_price_movement_triggers_revalidation_and_acceptance_receipt_is_required():
    machine = OrderIntentStateMachine()
    confirmed = machine.confirm(
        _intent(OrderIntentState.EXECUTION_SUBMITTED),
        acceptance_receipt_id="receipt-1",
        current_quote_odds=-145,
        price_movement_tolerance=Decimal("0.10"),
    )

    assert confirmed.requires_revalidation
    with pytest.raises(OrderIntentStateError):
        machine.open_position(confirmed)

    accepted = machine.confirm(
        _intent(OrderIntentState.EXECUTION_SUBMITTED),
        acceptance_receipt_id="receipt-2",
        current_quote_odds=-112,
        price_movement_tolerance=Decimal("0.10"),
    )
    opened = machine.open_position(accepted)

    assert accepted.state == OrderIntentState.ACCEPTED
    assert opened.state == OrderIntentState.POSITION_OPEN
