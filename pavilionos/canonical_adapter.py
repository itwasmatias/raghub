"""Canonical PavilionOS Provider Adapter v0.1

CRITICAL SECURITY ENFORCEMENT BOUNDARY

This adapter enforces the mandatory canonical gateway permit consumption
before ANY provider effect may execute.

Flow:
1. Read authorization envelope from stdin (JSON)
2. Parse and validate envelope
3. Call gateway.verify_and_consume_permit(permit_token, control_domain)
4. Verify returned gateway_claim_id matches envelope
5. ONLY NOW may provider execution begin
6. Record receipt and result

NO provider operation (subprocess, OS effect, process launch, etc.) may occur
before successful canonical permit consumption and durable HANDOFF_STARTED.

Direct adapter invocation without valid canonical authorization MUST be denied.
Local lineage files MUST NOT authorize execution.
Fabricated envelopes MUST NOT bypass the canonical permit check.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from federation.canonical_digest import operation_digest
from federation.effect_gateway import (
    DenialReason,
    GatewayDenied,
    GatewayStateError,
    GovernedEffectGateway,
)
from pavilionos.authorization_envelope import PavilionAuthorizationEnvelope


class AdapterDenied(Exception):
    """Provider adapter denied execution before provider handoff."""
    pass


class AdapterError(Exception):
    """Provider adapter internal error after authorization."""
    pass


@dataclass(slots=True, frozen=True)
class ProviderResult:
    """Provider execution result.

    Separates task outcome from effect state.
    """
    task_succeeded: bool
    task_error: str | None
    effect_status: str  # "nothing_landed", "something_landed", "indeterminate"
    detail: str
    observations: dict[str, Any]


@dataclass(slots=True, frozen=True)
class AdapterReceipt:
    """Adapter execution receipt returned to coordinator."""
    receipt_id: str
    gateway_claim_id: str
    effect_intent_id: str
    effect_dispatch_id: str
    task_succeeded: bool
    task_error: str | None
    effect_status: str
    detail: str
    observations: dict[str, Any]


class CanonicalPavilionAdapter:
    """Canonical provider adapter enforcing gateway permit consumption.

    This adapter is the CRITICAL ENFORCEMENT POINT where canonical
    MissionaryX authority meets provider execution.

    No provider effect may occur without successful permit consumption
    from the canonical governed gateway.
    """

    def __init__(
        self,
        gateway: GovernedEffectGateway,
        durable_store: Any,
        provider_registry: dict[str, Callable[[str], ProviderResult]],
        *,
        adapter_id: str = "pavilionos-canonical-adapter-v0.1",
    ) -> None:
        """Initialize canonical adapter.

        Args:
            gateway: GovernedEffectGateway instance
            durable_store: DurableEffectStore for claim verification
            provider_registry: Mapping action -> provider callable
            adapter_id: Adapter identity for validation
        """
        self.gateway = gateway
        self.durable_store = durable_store
        self.provider_registry = provider_registry
        self.adapter_id = adapter_id

    def dispatch(
        self,
        envelope: PavilionAuthorizationEnvelope,
    ) -> AdapterReceipt:
        """Dispatch provider execution after canonical permit consumption.

        This is the MANDATORY AUTHORIZATION BOUNDARY.

        Args:
            envelope: Authorization envelope with permit_token

        Returns:
            AdapterReceipt with provider execution result

        Raises:
            AdapterDenied: If authorization fails before provider handoff
            AdapterError: If internal error after authorization
        """
        # Step 1: Validate envelope adapter binding
        if envelope.adapter_id != self.adapter_id:
            raise AdapterDenied(
                f"Envelope adapter_id {envelope.adapter_id!r} does not match "
                f"this adapter {self.adapter_id!r}"
            )

        # Step 2: Validate envelope internal consistency
        # Verify that envelope.operation_digest actually corresponds to
        # envelope.action and envelope.provider_id (prevents action substitution)
        expected_operation_digest = operation_digest({
            "action": envelope.action,
            "provider_id": envelope.provider_id,
        })

        if envelope.operation_digest != expected_operation_digest:
            raise AdapterDenied(
                f"Envelope operation_digest {envelope.operation_digest!r} does not match "
                f"computed digest from envelope action/provider {expected_operation_digest!r}. "
                f"Envelope is internally inconsistent (possible action substitution attack)."
            )

        # Step 3: Validate provider is registered
        provider_fn = self.provider_registry.get(envelope.action)
        if provider_fn is None:
            raise AdapterDenied(
                f"Action {envelope.action!r} has no registered provider"
            )

        # Step 4: CRITICAL - Pre-consumption durable claim verification
        # Fetch authoritative durable claim BEFORE consuming authority.
        # This prevents forged envelopes from burning legitimate permits.
        # Enforces: THE EFFECT THAT ESCAPES TO THE PROVIDER MUST BE EXACTLY
        # THE EFFECT AUTHORIZED BY THE DURABLE CLAIM AND CONSUMED PERMIT.
        try:
            pre_consumption_claim = self.durable_store.get_gateway_claim(
                gateway_claim_id=envelope.gateway_claim_id,
                control_domain=envelope.control_domain,
            )
        except Exception as exc:
            raise AdapterDenied(
                f"Failed to retrieve gateway claim for pre-consumption verification: {exc}"
            ) from exc

        if pre_consumption_claim is None:
            raise AdapterDenied(
                f"Gateway claim {envelope.gateway_claim_id!r} not found in durable store"
            )

        # Verify envelope operation binding against authoritative durable claim
        # These checks occur BEFORE permit consumption to prevent authority burning

        # Verify operation_digest binding (covers action + provider binding)
        if pre_consumption_claim["operation_digest"] != envelope.operation_digest:
            raise AdapterDenied(
                f"Envelope operation_digest {envelope.operation_digest!r} does not match "
                f"authorized claim operation_digest {pre_consumption_claim['operation_digest']!r}"
            )

        # Verify provider_id binding (explicit check for defense-in-depth)
        if pre_consumption_claim["provider_id"] != envelope.provider_id:
            raise AdapterDenied(
                f"Envelope provider_id {envelope.provider_id!r} does not match "
                f"authorized claim provider_id {pre_consumption_claim['provider_id']!r}"
            )

        # Verify adapter_id binding (prevent adapter substitution)
        if pre_consumption_claim["adapter_id"] != envelope.adapter_id:
            raise AdapterDenied(
                f"Envelope adapter_id {envelope.adapter_id!r} does not match "
                f"authorized claim adapter_id {pre_consumption_claim['adapter_id']!r}"
            )

        # Step 5: CRITICAL - Consume canonical permit and establish HANDOFF_STARTED
        # Pre-consumption verification passed - envelope matches durable claim.
        # Now atomically consume permit and transition to HANDOFF_STARTED.
        try:
            authorized_claim_id = self.gateway.verify_and_consume_permit(
                permit_token=envelope.permit_token,
                control_domain=envelope.control_domain,
            )
        except GatewayDenied as exc:
            # Map gateway denial reasons to adapter denial
            raise AdapterDenied(
                f"Gateway denied permit consumption: {exc.reason.value}"
            ) from exc
        except Exception as exc:
            # Gateway errors before HANDOFF_STARTED are pre-authorization failures
            raise AdapterDenied(
                f"Gateway permit consumption failed: {exc}"
            ) from exc

        # Step 6: Verify returned claim ID matches envelope
        if authorized_claim_id != envelope.gateway_claim_id:
            # This should never happen unless there's a serious bug or attack
            raise AdapterError(
                f"Gateway authorized claim {authorized_claim_id!r} "
                f"does not match envelope claim {envelope.gateway_claim_id!r}"
            )

        # Step 7: Generate receipt ID
        import uuid
        receipt_id = f"pavilion-receipt-{uuid.uuid4().hex}"

        # At this point:
        # - Envelope internal consistency verified
        # - Envelope operation binding verified against durable claim (BEFORE consumption)
        # - Canonical permit has been successfully consumed
        # - Gateway claim is durably in HANDOFF_STARTED state
        # - Provider execution is now authorized
        # - Forged envelopes are rejected WITHOUT burning legitimate authority

        # Step 8: Execute provider
        try:
            result = provider_fn(envelope.action)
        except Exception as exc:
            # Provider raised exception - effect may be indeterminate
            result = ProviderResult(
                task_succeeded=False,
                task_error=f"Provider raised exception: {exc}",
                effect_status="indeterminate",
                detail=f"Provider execution failed with exception after authorization",
                observations={"exception": repr(exc)},
            )

        # Step 9: Build adapter receipt
        receipt = AdapterReceipt(
            receipt_id=receipt_id,
            gateway_claim_id=envelope.gateway_claim_id,
            effect_intent_id=envelope.effect_intent_id,
            effect_dispatch_id=envelope.effect_dispatch_id,
            task_succeeded=result.task_succeeded,
            task_error=result.task_error,
            effect_status=result.effect_status,
            detail=result.detail,
            observations=result.observations,
        )

        # Note: Gateway receipt/result recording is handled by coordinator
        return receipt

    def dispatch_from_stdin(self) -> int:
        """Read envelope from stdin, dispatch, write receipt to stdout.

        This is the entry point for subprocess invocation.

        Returns:
            Exit code (0 for success, 3 for denial, 1 for error)
        """
        try:
            # Read envelope from stdin
            envelope_json = sys.stdin.read()
            if not envelope_json.strip():
                print(
                    "DENIED: No authorization envelope provided on stdin",
                    file=sys.stderr,
                )
                return 3

            # Parse envelope
            try:
                envelope = PavilionAuthorizationEnvelope.from_json(envelope_json)
            except ValueError as exc:
                print(
                    f"DENIED: Invalid authorization envelope: {exc}",
                    file=sys.stderr,
                )
                return 3

            # Dispatch with canonical authorization
            receipt = self.dispatch(envelope)

            # Write receipt to stdout as JSON
            import json
            receipt_json = json.dumps(
                {
                    "receipt_id": receipt.receipt_id,
                    "gateway_claim_id": receipt.gateway_claim_id,
                    "effect_intent_id": receipt.effect_intent_id,
                    "effect_dispatch_id": receipt.effect_dispatch_id,
                    "task_succeeded": receipt.task_succeeded,
                    "task_error": receipt.task_error,
                    "effect_status": receipt.effect_status,
                    "detail": receipt.detail,
                    "observations": receipt.observations,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            print(receipt_json)

            return 0 if receipt.task_succeeded else 1

        except AdapterDenied as exc:
            print(f"DENIED: {exc}", file=sys.stderr)
            return 3
        except AdapterError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr)
            return 1


__all__ = [
    "CanonicalPavilionAdapter",
    "AdapterDenied",
    "AdapterError",
    "ProviderResult",
    "AdapterReceipt",
]
