from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote_plus
from uuid import uuid4


class ExecutionMode(StrEnum):
    PRACTICE = "practice"
    MANUAL_RECORD = "manual_record"
    PREFILLED_LINK = "prefilled_link"
    DIRECT_AUTO = "direct_auto"


class OrderState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PENDING_CONFIRMATION = "pending_confirmation"
    READY_TO_SUBMIT = "ready_to_submit"
    SUBMITTED = "submitted"
    AWAITING_EXTERNAL_CONFIRMATION = "awaiting_external_confirmation"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELED = "canceled"


class ReceiptType(StrEnum):
    VALIDATION = "validation"
    CONFIRMATION = "confirmation"
    SUBMISSION = "submission"
    PROVIDER_RESPONSE = "provider_response"
    PREFILLED_LINK = "prefilled_link"
    FAILURE = "failure"


class FailureCode(StrEnum):
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    DIRECT_NOT_AUTHORIZED = "direct_not_authorized"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_TIMEOUT = "provider_timeout"
    CONFIRMATION_REQUIRED = "confirmation_required"
    INVALID_TRANSITION = "invalid_transition"


@dataclass(frozen=True, slots=True)
class ExecutionOrder:
    id: str
    order_intent_id: str
    account_id: str
    market_id: str
    outcome_id: str
    stake: Decimal
    requested_odds: int
    accepted_odds: int
    sportsbook: str
    mode: ExecutionMode
    state: OrderState
    requires_confirmation: bool
    created_at: str
    updated_at: str
    provider_reference: str | None = None
    prefilled_url: str | None = None
    failure_code: FailureCode | None = None
    failure_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    id: str
    order_id: str
    receipt_type: ReceiptType
    status: str
    created_at: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutionTransition:
    id: str
    order_id: str
    from_state: OrderState
    to_state: OrderState
    reason: str
    occurred_at: str
    actor_type: str
    actor_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterExecutionResult:
    next_state: OrderState
    status: str
    provider_reference: str | None = None
    prefilled_url: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class ExecutionAdapter(Protocol):
    def execute(self, order: ExecutionOrder) -> AdapterExecutionResult: ...


class DirectExecutionNotAuthorized(RuntimeError):
    pass


class StateMachineError(ValueError):
    pass


class OrderStateMachine:
    ALLOWED_TRANSITIONS: dict[OrderState, set[OrderState]] = {
        OrderState.DRAFT: {OrderState.VALIDATED, OrderState.CANCELED},
        OrderState.VALIDATED: {
            OrderState.PENDING_CONFIRMATION,
            OrderState.READY_TO_SUBMIT,
            OrderState.CANCELED,
        },
        OrderState.PENDING_CONFIRMATION: {
            OrderState.READY_TO_SUBMIT,
            OrderState.CANCELED,
            OrderState.FAILED,
        },
        OrderState.READY_TO_SUBMIT: {OrderState.SUBMITTED, OrderState.CANCELED},
        OrderState.SUBMITTED: {
            OrderState.ACCEPTED,
            OrderState.REJECTED,
            OrderState.FAILED,
            OrderState.AWAITING_EXTERNAL_CONFIRMATION,
        },
        OrderState.AWAITING_EXTERNAL_CONFIRMATION: {
            OrderState.ACCEPTED,
            OrderState.REJECTED,
            OrderState.CANCELED,
            OrderState.FAILED,
        },
        OrderState.ACCEPTED: set(),
        OrderState.REJECTED: set(),
        OrderState.FAILED: set(),
        OrderState.CANCELED: set(),
    }

    @classmethod
    def can_transition(cls, from_state: OrderState, to_state: OrderState) -> bool:
        return to_state in cls.ALLOWED_TRANSITIONS.get(from_state, set())

    @classmethod
    def transition(
        cls,
        order: ExecutionOrder,
        to_state: OrderState,
        *,
        reason: str,
        now: str,
        failure_code: FailureCode | None = None,
        failure_message: str | None = None,
        provider_reference: str | None = None,
        prefilled_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ExecutionOrder, ExecutionTransition]:
        if not cls.can_transition(order.state, to_state):
            raise StateMachineError(
                f"invalid transition {order.state.value} -> {to_state.value}"
            )
        updated = replace(
            order,
            state=to_state,
            updated_at=now,
            provider_reference=provider_reference or order.provider_reference,
            prefilled_url=prefilled_url or order.prefilled_url,
            failure_code=failure_code,
            failure_message=failure_message,
            metadata={**order.metadata, **(metadata or {})},
        )
        transition = ExecutionTransition(
            id=f"transition-{uuid4().hex[:12]}",
            order_id=order.id,
            from_state=order.state,
            to_state=to_state,
            reason=reason,
            occurred_at=now,
            actor_type="system",
            actor_id="execution-orchestrator",
            metadata=metadata or {},
        )
        return updated, transition


class PracticeExecutionAdapter:
    def execute(self, order: ExecutionOrder) -> AdapterExecutionResult:
        return AdapterExecutionResult(
            next_state=OrderState.ACCEPTED,
            status="accepted",
            provider_reference=f"practice-{order.id}",
            payload={
                "message": "Practice position recorded; no external execution.",
                "mode": order.mode.value,
            },
        )


class ManualRecordingAdapter:
    def execute(self, order: ExecutionOrder) -> AdapterExecutionResult:
        return AdapterExecutionResult(
            next_state=OrderState.ACCEPTED,
            status="accepted",
            provider_reference=f"manual-{order.id}",
            payload={
                "message": "Manual real position recorded from user confirmation.",
                "mode": order.mode.value,
            },
        )


class PrefilledLinkAdapter:
    def __init__(self, *, supported_books: dict[str, str] | None = None) -> None:
        self.supported_books = supported_books or {
            "draftkings": "https://sportsbook.draftkings.com/event",
            "fanduel": "https://sportsbook.fanduel.com/navigation",
            "betmgm": "https://sports.betmgm.com/en/sports",
        }

    def execute(self, order: ExecutionOrder) -> AdapterExecutionResult:
        base = self.supported_books.get(order.sportsbook.lower())
        if base is None:
            base = "https://example.invalid/sportsbook"
        params = (
            f"market_id={quote_plus(order.market_id)}"
            f"&outcome_id={quote_plus(order.outcome_id)}"
            f"&odds={order.accepted_odds}"
            f"&stake={quote_plus(str(order.stake))}"
        )
        url = f"{base}?{params}"
        return AdapterExecutionResult(
            next_state=OrderState.AWAITING_EXTERNAL_CONFIRMATION,
            status="prefilled_link_ready",
            prefilled_url=url,
            payload={
                "message": "Open sportsbook link, confirm bet externally, then confirm receipt in SIP.",
                "supported": order.sportsbook.lower() in self.supported_books,
            },
        )


class DirectExecutionAdapter:
    def __init__(self, *, authorized: bool, provider_name: str | None = None) -> None:
        self.authorized = authorized
        self.provider_name = provider_name or "transactional-book"

    def execute(self, order: ExecutionOrder) -> AdapterExecutionResult:
        if not self.authorized:
            raise DirectExecutionNotAuthorized(
                "Direct execution is disabled until an authorized transactional sportsbook API is configured."
            )
        provider_ref = f"{self.provider_name}-{order.id}"
        return AdapterExecutionResult(
            next_state=OrderState.ACCEPTED,
            status="accepted",
            provider_reference=provider_ref,
            payload={
                "message": "Direct API execution accepted by provider.",
                "provider": self.provider_name,
            },
        )


class ExecutionOrchestrator:
    def __init__(self, *, direct_authorized: bool = False) -> None:
        self.adapters: dict[ExecutionMode, ExecutionAdapter] = {
            ExecutionMode.PRACTICE: PracticeExecutionAdapter(),
            ExecutionMode.MANUAL_RECORD: ManualRecordingAdapter(),
            ExecutionMode.PREFILLED_LINK: PrefilledLinkAdapter(),
            ExecutionMode.DIRECT_AUTO: DirectExecutionAdapter(
                authorized=direct_authorized,
                provider_name="transactional-book",
            ),
        }

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def requires_confirmation(mode: ExecutionMode) -> bool:
        return mode in {ExecutionMode.MANUAL_RECORD, ExecutionMode.PREFILLED_LINK}

    def new_order(
        self,
        *,
        order_intent_id: str,
        account_id: str,
        market_id: str,
        outcome_id: str,
        stake: Decimal,
        requested_odds: int,
        accepted_odds: int,
        sportsbook: str,
        mode: ExecutionMode,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ExecutionOrder, list[ExecutionTransition], list[ExecutionReceipt]]:
        now = self.now_iso()
        order = ExecutionOrder(
            id=f"exec-{uuid4().hex[:12]}",
            order_intent_id=order_intent_id,
            account_id=account_id,
            market_id=market_id,
            outcome_id=outcome_id,
            stake=stake,
            requested_odds=requested_odds,
            accepted_odds=accepted_odds,
            sportsbook=sportsbook,
            mode=mode,
            state=OrderState.DRAFT,
            requires_confirmation=self.requires_confirmation(mode),
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )

        transitions: list[ExecutionTransition] = []
        receipts: list[ExecutionReceipt] = []

        order, t1 = OrderStateMachine.transition(
            order,
            OrderState.VALIDATED,
            reason="order validated",
            now=now,
        )
        transitions.append(t1)
        receipts.append(
            ExecutionReceipt(
                id=f"receipt-{uuid4().hex[:12]}",
                order_id=order.id,
                receipt_type=ReceiptType.VALIDATION,
                status="ok",
                created_at=now,
                payload={"mode": mode.value},
            )
        )

        next_state = (
            OrderState.PENDING_CONFIRMATION
            if order.requires_confirmation
            else OrderState.READY_TO_SUBMIT
        )
        order, t2 = OrderStateMachine.transition(
            order,
            next_state,
            reason=(
                "awaiting explicit confirmation"
                if order.requires_confirmation
                else "ready to submit"
            ),
            now=now,
        )
        transitions.append(t2)
        return order, transitions, receipts

    def confirm_order(
        self,
        order: ExecutionOrder,
        *,
        actor_id: str,
        note: str | None = None,
    ) -> tuple[ExecutionOrder, ExecutionTransition, ExecutionReceipt]:
        now = self.now_iso()
        if order.state != OrderState.PENDING_CONFIRMATION:
            raise StateMachineError("order is not waiting for confirmation")
        updated, transition = OrderStateMachine.transition(
            order,
            OrderState.READY_TO_SUBMIT,
            reason="user confirmed execution intent",
            now=now,
            metadata={"actor_id": actor_id, "note": note or ""},
        )
        receipt = ExecutionReceipt(
            id=f"receipt-{uuid4().hex[:12]}",
            order_id=updated.id,
            receipt_type=ReceiptType.CONFIRMATION,
            status="confirmed",
            created_at=now,
            payload={"actor_id": actor_id, "note": note or ""},
        )
        return updated, transition, receipt

    def submit_order(
        self,
        order: ExecutionOrder,
    ) -> tuple[ExecutionOrder, list[ExecutionTransition], list[ExecutionReceipt]]:
        now = self.now_iso()
        if order.state != OrderState.READY_TO_SUBMIT:
            raise StateMachineError("order is not ready to submit")

        transitions: list[ExecutionTransition] = []
        receipts: list[ExecutionReceipt] = []

        order, submit_transition = OrderStateMachine.transition(
            order,
            OrderState.SUBMITTED,
            reason="submitted to adapter",
            now=now,
        )
        transitions.append(submit_transition)
        receipts.append(
            ExecutionReceipt(
                id=f"receipt-{uuid4().hex[:12]}",
                order_id=order.id,
                receipt_type=ReceiptType.SUBMISSION,
                status="submitted",
                created_at=now,
                payload={"mode": order.mode.value, "sportsbook": order.sportsbook},
            )
        )

        adapter = self.adapters[order.mode]
        try:
            result = adapter.execute(order)
        except DirectExecutionNotAuthorized as error:
            failed, failed_transition = OrderStateMachine.transition(
                order,
                OrderState.FAILED,
                reason="direct execution not authorized",
                now=self.now_iso(),
                failure_code=FailureCode.DIRECT_NOT_AUTHORIZED,
                failure_message=str(error),
            )
            transitions.append(failed_transition)
            receipts.append(
                ExecutionReceipt(
                    id=f"receipt-{uuid4().hex[:12]}",
                    order_id=order.id,
                    receipt_type=ReceiptType.FAILURE,
                    status="failed",
                    created_at=self.now_iso(),
                    payload={
                        "failure_code": FailureCode.DIRECT_NOT_AUTHORIZED.value,
                        "message": str(error),
                    },
                )
            )
            return failed, transitions, receipts
        except Exception as error:  # pragma: no cover
            failed, failed_transition = OrderStateMachine.transition(
                order,
                OrderState.FAILED,
                reason="adapter execution failed",
                now=self.now_iso(),
                failure_code=FailureCode.ADAPTER_UNAVAILABLE,
                failure_message=str(error),
            )
            transitions.append(failed_transition)
            receipts.append(
                ExecutionReceipt(
                    id=f"receipt-{uuid4().hex[:12]}",
                    order_id=order.id,
                    receipt_type=ReceiptType.FAILURE,
                    status="failed",
                    created_at=self.now_iso(),
                    payload={
                        "failure_code": FailureCode.ADAPTER_UNAVAILABLE.value,
                        "message": str(error),
                    },
                )
            )
            return failed, transitions, receipts

        next_order, done_transition = OrderStateMachine.transition(
            order,
            result.next_state,
            reason="adapter result",
            now=self.now_iso(),
            provider_reference=result.provider_reference,
            prefilled_url=result.prefilled_url,
            metadata=result.payload,
        )
        transitions.append(done_transition)

        receipt_type = (
            ReceiptType.PREFILLED_LINK
            if result.next_state == OrderState.AWAITING_EXTERNAL_CONFIRMATION
            else ReceiptType.PROVIDER_RESPONSE
        )
        receipts.append(
            ExecutionReceipt(
                id=f"receipt-{uuid4().hex[:12]}",
                order_id=next_order.id,
                receipt_type=receipt_type,
                status=result.status,
                created_at=self.now_iso(),
                payload={
                    **result.payload,
                    "provider_reference": result.provider_reference,
                    "prefilled_url": result.prefilled_url,
                },
            )
        )
        return next_order, transitions, receipts

    def finalize_external_confirmation(
        self,
        order: ExecutionOrder,
        *,
        accepted: bool,
        external_reference: str | None,
        note: str | None,
    ) -> tuple[ExecutionOrder, ExecutionTransition, ExecutionReceipt]:
        if order.state != OrderState.AWAITING_EXTERNAL_CONFIRMATION:
            raise StateMachineError("order is not awaiting external confirmation")

        target_state = OrderState.ACCEPTED if accepted else OrderState.REJECTED
        now = self.now_iso()
        updated, transition = OrderStateMachine.transition(
            order,
            target_state,
            reason="external sportsbook confirmation recorded",
            now=now,
            provider_reference=external_reference or order.provider_reference,
            failure_code=None if accepted else FailureCode.PROVIDER_REJECTED,
            failure_message=None if accepted else (note or "Rejected externally"),
            metadata={"external_note": note or ""},
        )
        receipt = ExecutionReceipt(
            id=f"receipt-{uuid4().hex[:12]}",
            order_id=order.id,
            receipt_type=ReceiptType.PROVIDER_RESPONSE,
            status="accepted" if accepted else "rejected",
            created_at=now,
            payload={
                "external_reference": external_reference,
                "note": note,
                "accepted": accepted,
            },
        )
        return updated, transition, receipt
