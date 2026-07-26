from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

from sports.execution.models import (
    ExecutionAdapter,
    ExecutionReceipt,
    ExecutionReceiptType,
    OrderIntentEnvelope,
)


@dataclass
class _ReceiptStore:
    receipts: dict[str, ExecutionReceipt] = field(default_factory=dict)


class PracticeExecutionAdapter:
    def __init__(self) -> None:
        self._store = _ReceiptStore()

    def request_quote(self, order_intent: OrderIntentEnvelope) -> dict[str, object]:
        return {
            "mode": "practice",
            "execution_id": f"practice-{order_intent.intent_id}",
        }

    def validate_availability(
        self, order_intent: OrderIntentEnvelope
    ) -> dict[str, object]:
        return {"available": True, "mode": "practice"}

    def submit(
        self, order_intent: OrderIntentEnvelope, confirmation: dict[str, object]
    ) -> dict[str, object]:
        execution_id = f"practice-{order_intent.intent_id}"
        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{uuid4().hex[:10]}",
            execution_id=execution_id,
            receipt_type=ExecutionReceiptType.ACCEPTANCE,
            created_at=str(confirmation.get("created_at") or "now"),
            payload={"mode": "practice", "confirmation": confirmation},
        )
        self._store.receipts[execution_id] = receipt
        return {"execution_id": execution_id, "status": "accepted", "receipt": receipt}

    def get_status(self, execution_id: str) -> dict[str, object]:
        return {"execution_id": execution_id, "status": "accepted"}

    def cancel(self, execution_id: str) -> dict[str, object]:
        return {"execution_id": execution_id, "status": "canceled"}

    def fetch_receipt(self, execution_id: str) -> ExecutionReceipt | None:
        return self._store.receipts.get(execution_id)


class ManualRecordingAdapter:
    def __init__(self) -> None:
        self._store = _ReceiptStore()

    def request_quote(self, order_intent: OrderIntentEnvelope) -> dict[str, object]:
        return {
            "mode": "manual_record",
            "execution_id": f"manual-{order_intent.intent_id}",
        }

    def validate_availability(
        self, order_intent: OrderIntentEnvelope
    ) -> dict[str, object]:
        return {"available": True, "mode": "manual_record"}

    def submit(
        self, order_intent: OrderIntentEnvelope, confirmation: dict[str, object]
    ) -> dict[str, object]:
        execution_id = f"manual-{order_intent.intent_id}"
        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{uuid4().hex[:10]}",
            execution_id=execution_id,
            receipt_type=ExecutionReceiptType.CONFIRMATION,
            created_at=str(confirmation.get("created_at") or "now"),
            payload={"mode": "manual_record", "confirmation": confirmation},
        )
        self._store.receipts[execution_id] = receipt
        return {"execution_id": execution_id, "status": "accepted", "receipt": receipt}

    def get_status(self, execution_id: str) -> dict[str, object]:
        return {"execution_id": execution_id, "status": "accepted"}

    def cancel(self, execution_id: str) -> dict[str, object]:
        return {"execution_id": execution_id, "status": "canceled"}

    def fetch_receipt(self, execution_id: str) -> ExecutionReceipt | None:
        return self._store.receipts.get(execution_id)


class FutureDeepLinkAdapter:
    def __init__(
        self, *, sportsbook_url: str = "https://example.invalid/deeplink"
    ) -> None:
        self.sportsbook_url = sportsbook_url

    def request_quote(self, order_intent: OrderIntentEnvelope) -> dict[str, object]:
        return {
            "mode": "deep_link",
            "execution_id": f"deeplink-{order_intent.intent_id}",
        }

    def validate_availability(
        self, order_intent: OrderIntentEnvelope
    ) -> dict[str, object]:
        return {"available": True, "mode": "deep_link"}

    def submit(
        self, order_intent: OrderIntentEnvelope, confirmation: dict[str, object]
    ) -> dict[str, object]:
        execution_id = f"deeplink-{order_intent.intent_id}"
        return {
            "execution_id": execution_id,
            "status": "awaiting_external_confirmation",
            "deep_link_url": self.sportsbook_url,
            "confirmation": confirmation,
        }

    def get_status(self, execution_id: str) -> dict[str, object]:
        return {
            "execution_id": execution_id,
            "status": "awaiting_external_confirmation",
        }

    def cancel(self, execution_id: str) -> dict[str, object]:
        return {"execution_id": execution_id, "status": "canceled"}

    def fetch_receipt(self, execution_id: str) -> ExecutionReceipt | None:
        return None


class FutureAuthorizedTransactionalAdapter:
    def __init__(self, *, authorized: bool = False) -> None:
        self.authorized = authorized

    def request_quote(self, order_intent: OrderIntentEnvelope) -> dict[str, object]:
        return {
            "mode": "transactional",
            "execution_id": f"txn-{order_intent.intent_id}",
        }

    def validate_availability(
        self, order_intent: OrderIntentEnvelope
    ) -> dict[str, object]:
        return {"available": self.authorized, "mode": "transactional"}

    def submit(
        self, order_intent: OrderIntentEnvelope, confirmation: dict[str, object]
    ) -> dict[str, object]:
        if not self.authorized:
            raise RuntimeError(
                "direct sportsbook execution remains disabled until authorized"
            )
        execution_id = f"txn-{order_intent.intent_id}"
        return {
            "execution_id": execution_id,
            "status": "accepted",
            "confirmation": confirmation,
        }

    def get_status(self, execution_id: str) -> dict[str, object]:
        return {"execution_id": execution_id, "status": "accepted"}

    def cancel(self, execution_id: str) -> dict[str, object]:
        return {"execution_id": execution_id, "status": "canceled"}

    def fetch_receipt(self, execution_id: str) -> ExecutionReceipt | None:
        return None
