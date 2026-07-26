from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from sports.execution.models import OrderIntentEnvelope, OrderIntentState


class OrderIntentStateError(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[OrderIntentState, tuple[OrderIntentState, ...]] = {
    OrderIntentState.THESIS_DRAFTED: (OrderIntentState.FORECAST_CREATED,),
    OrderIntentState.FORECAST_CREATED: (OrderIntentState.PROBABILITIES_RECONCILED,),
    OrderIntentState.PROBABILITIES_RECONCILED: (OrderIntentState.STRATEGY_MATCHED,),
    OrderIntentState.STRATEGY_MATCHED: (OrderIntentState.RISK_EVALUATED,),
    OrderIntentState.RISK_EVALUATED: (OrderIntentState.RISK_APPROVED,),
    OrderIntentState.RISK_APPROVED: (OrderIntentState.ORDER_INTENT_CREATED,),
    OrderIntentState.ORDER_INTENT_CREATED: (OrderIntentState.USER_CONFIRMED,),
    OrderIntentState.USER_CONFIRMED: (OrderIntentState.EXECUTION_SUBMITTED,),
    OrderIntentState.EXECUTION_SUBMITTED: (
        OrderIntentState.ACCEPTED,
        OrderIntentState.REJECTED,
    ),
    OrderIntentState.ACCEPTED: (OrderIntentState.POSITION_OPEN,),
    OrderIntentState.REJECTED: (),
    OrderIntentState.POSITION_OPEN: (OrderIntentState.SETTLEMENT_PENDING,),
    OrderIntentState.SETTLEMENT_PENDING: (OrderIntentState.SETTLED,),
    OrderIntentState.SETTLED: (OrderIntentState.REVIEWED,),
    OrderIntentState.REVIEWED: (),
}


class OrderIntentStateMachine:
    def transition(
        self,
        intent: OrderIntentEnvelope,
        target_state: OrderIntentState,
    ) -> OrderIntentEnvelope:
        if intent.state == target_state:
            return intent
        allowed = ALLOWED_TRANSITIONS.get(intent.state, ())
        if target_state not in allowed:
            raise OrderIntentStateError(
                f"invalid transition {intent.state.value} -> {target_state.value}"
            )
        return replace(intent, state=target_state)

    def submit(
        self,
        intent: OrderIntentEnvelope,
        *,
        execution_id: str,
    ) -> OrderIntentEnvelope:
        if execution_id in intent.submission_ids:
            return intent
        if intent.state != OrderIntentState.USER_CONFIRMED:
            raise OrderIntentStateError(
                "duplicate submission or invalid submission state"
            )
        return replace(
            intent,
            state=OrderIntentState.EXECUTION_SUBMITTED,
            execution_id=execution_id,
            submission_ids=intent.submission_ids + (execution_id,),
        )

    def confirm(
        self,
        intent: OrderIntentEnvelope,
        *,
        acceptance_receipt_id: str,
        current_quote_odds: int,
        price_movement_tolerance: Decimal = Decimal("0.10"),
    ) -> OrderIntentEnvelope:
        requested = Decimal(str(intent.requested_odds or current_quote_odds))
        current = Decimal(str(current_quote_odds))
        if requested == 0:
            raise OrderIntentStateError("requested odds unavailable")
        movement = abs(current - requested) / abs(requested)
        if movement > price_movement_tolerance:
            return replace(
                intent,
                acceptance_receipt_id=acceptance_receipt_id,
                current_quote_odds=current_quote_odds,
                requires_revalidation=True,
            )
        if intent.state not in {
            OrderIntentState.EXECUTION_SUBMITTED,
            OrderIntentState.USER_CONFIRMED,
        }:
            raise OrderIntentStateError("confirmation requires a submitted intent")
        return replace(
            intent,
            state=OrderIntentState.ACCEPTED,
            acceptance_receipt_id=acceptance_receipt_id,
            current_quote_odds=current_quote_odds,
            requires_revalidation=False,
        )

    def open_position(self, intent: OrderIntentEnvelope) -> OrderIntentEnvelope:
        if not intent.acceptance_receipt_id:
            raise OrderIntentStateError(
                "acceptance receipt is required before opening a position"
            )
        if intent.state != OrderIntentState.ACCEPTED:
            raise OrderIntentStateError("position can only open after acceptance")
        return replace(intent, state=OrderIntentState.POSITION_OPEN)
