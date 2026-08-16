"""Canonical PavilionOS Coordinator v0.1

Orchestrates the complete canonical MissionaryX authorization-to-dispatch flow
for PavilionOS local-shell actions.

Flow:
1. Accept Pavilion action request
2. Resolve active canonical delegation grant
3. Create canonical authority reservation (RESERVED)
4. Create canonical EffectIntent (write-ahead commitment)
5. Create canonical EffectDispatch
6. Create GatewayEffectRequest
7. Claim gateway dispatch (single-winner)
8. Issue gateway permit (single-use)
9. Create authorization envelope
10. Invoke adapter subprocess with envelope via stdin
11. Record gateway receipt
12. Record gateway result (TERMINAL or INDETERMINATE)

Authority is owned by canonical MissionaryX state, not local Pavilion files.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from federation.canonical_digest import operation_digest
from federation.delegation_grant import AuthoritativeDelegationGrant, DelegationGrantStatus
from federation.delegation_grant_registry import DelegationGrantRegistry
from federation.durable_effect_store import DurableEffectStore
from federation.effect_gateway import (
    EffectConsequence,
    GatewayDenied,
    GatewayEffectRequest,
    GatewayEffectResult,
    GovernedEffectGateway,
)
from federation.effect_safety import (
    AuthorityDisposition,
    AuthorityReservation,
    EffectDispatch,
    EffectIntent,
    EffectState,
    ProviderReconcilability,
)
from pavilionos.authorization_envelope import PavilionAuthorizationEnvelope


# PavilionOS v0.1 Constants
PAVILION_CONTROL_DOMAIN = "pavilionos-localhost-v0.1"
PAVILION_PROVIDER_ID = "pavilionos-local-shell"
PAVILION_ADAPTER_ID = "pavilionos-canonical-adapter-v0.1"
PAVILION_CREDENTIAL_SCOPE = ("pavilionos:local-shell",)


class CoordinatorError(Exception):
    """Base coordinator error."""
    pass


class CoordinatorDenied(CoordinatorError):
    """Coordinator denied action before provider handoff."""
    pass


@dataclass(slots=True, frozen=True)
class PavilionActionRequest:
    """Pavilion action request."""
    action: str  # e.g., "restart-firefox", "reload-desktop"
    mission_id: str
    task_id: str
    attempt_id: str
    principal_identity: str
    agent_identity: str
    delegation_grant_id: str
    requested_capability: str


@dataclass(slots=True, frozen=True)
class CoordinatorResult:
    """Coordinator execution result."""
    task_succeeded: bool
    task_error: str | None
    effect_status: EffectState
    effect_intent_id: str
    effect_dispatch_id: str
    authority_reservation_id: str
    authority_disposition: AuthorityDisposition | None
    gateway_claim_id: str
    receipt_id: str | None
    detail: str


class CanonicalPavilionCoordinator:
    """Canonical coordinator for Pavilion actions using MissionaryX gateway.

    Orchestrates the complete canonical authorization-to-dispatch flow:
    delegation → reservation → intent → dispatch → gateway → permit → adapter
    """

    def __init__(
        self,
        durable_store: DurableEffectStore,
        delegation_registry: DelegationGrantRegistry,
        gateway: GovernedEffectGateway,
        *,
        adapter_path: Path | None = None,
        clock: Any = None,
    ) -> None:
        """Initialize canonical coordinator.

        Args:
            durable_store: DurableEffectStore for intents/dispatches/reservations
            delegation_registry: DelegationGrantRegistry for grant resolution
            gateway: GovernedEffectGateway for permit issuance
            adapter_path: Path to adapter executable (for subprocess invocation)
            clock: Optional clock for testing
        """
        self.store = durable_store
        self.delegation_registry = delegation_registry
        self.gateway = gateway
        self.adapter_path = adapter_path
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def _now(self) -> datetime:
        """Get current time from clock."""
        return self._clock()

    def coordinate(
        self,
        request: PavilionActionRequest,
        *,
        provider_registry: dict[str, Any] | None = None,
    ) -> CoordinatorResult:
        """Coordinate Pavilion action through canonical gateway.

        Args:
            request: Pavilion action request
            provider_registry: Optional provider mapping (for testing)

        Returns:
            CoordinatorResult with final effect status

        Raises:
            CoordinatorDenied: If authorization fails before provider handoff
            CoordinatorError: If error occurs after authorization
        """
        now = self._now()

        # Step 1: Resolve active canonical delegation grant
        try:
            grant = self.delegation_registry.get(
                request.delegation_grant_id,
                domain_id=PAVILION_CONTROL_DOMAIN,
                mission_id=request.mission_id,
            )
        except Exception as exc:
            raise CoordinatorDenied(f"Delegation grant resolution failed: {exc}") from exc

        if grant.status != DelegationGrantStatus.ACTIVE:
            raise CoordinatorDenied(
                f"Delegation grant {request.delegation_grant_id} is not active: {grant.status.value}"
            )

        if request.requested_capability not in grant.capabilities.capabilities:
            raise CoordinatorDenied(
                f"Requested capability {request.requested_capability!r} "
                f"not in grant capabilities"
            )

        # Step 2: Generate canonical identifiers
        effect_intent_id = f"pavilion-intent-{secrets.token_urlsafe(16)}"
        effect_dispatch_id = f"pavilion-dispatch-{secrets.token_urlsafe(16)}"
        authority_reservation_id = f"pavilion-reservation-{secrets.token_urlsafe(16)}"
        idempotency_key = f"pavilion:{request.mission_id}:{request.task_id}:{request.attempt_id}"

        # Step 3: Create canonical EffectIntent (write-ahead commitment)
        # MUST create intent BEFORE reservation due to FK constraint
        operation_params = {
            "action": request.action,
            "provider_id": PAVILION_PROVIDER_ID,
        }
        op_digest = operation_digest(operation_params)

        intent = EffectIntent(
            effect_intent_id=effect_intent_id,
            decision_id=f"pavilion-decision-{request.attempt_id}",
            mission_id=request.mission_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            operation_digest=op_digest,
            idempotency_key=idempotency_key,
            provider_scope=PAVILION_PROVIDER_ID,
            authority_reservation_id=authority_reservation_id,
            compensation_strategy=None,
            evidence_reference=f"pavilion-evidence-{effect_intent_id}",
            state="committed_not_dispatched",
            created_at=now,
            control_domain=PAVILION_CONTROL_DOMAIN,
        )

        try:
            self.store.commit_intent(intent)
        except Exception as exc:
            raise CoordinatorDenied(f"Effect intent commit failed: {exc}") from exc

        # Step 4: Create canonical authority reservation
        # References intent via FK constraint
        reservation = AuthorityReservation(
            reservation_id=authority_reservation_id,
            effect_intent_id=effect_intent_id,
            capability_type=request.requested_capability,
            amount=1.0,
            disposition=AuthorityDisposition.RESERVED,
            reserved_at=now,
            disposition_at=None,
            disposition_evidence=None,
            control_domain=PAVILION_CONTROL_DOMAIN,
        )

        try:
            self.store.store_reservation(reservation)
        except Exception as exc:
            raise CoordinatorDenied(f"Authority reservation failed: {exc}") from exc

        # Step 5: Create canonical EffectDispatch
        dispatch = EffectDispatch(
            dispatch_id=effect_dispatch_id,
            effect_intent_id=effect_intent_id,
            attempt_id=request.attempt_id,
            idempotency_key=idempotency_key,
            provider_adapter=PAVILION_ADAPTER_ID,
            capability_profile_version="v0.1",
            transport_digest=op_digest,
            posture="attempting",
            provider_operation_id=None,
            evidence_reference=f"pavilion-dispatch-{effect_dispatch_id}",
            dispatched_at=now,
            control_domain=PAVILION_CONTROL_DOMAIN,
        )

        try:
            self.store.commit_dispatch(dispatch)
        except Exception as exc:
            raise CoordinatorDenied(f"Effect dispatch commit failed: {exc}") from exc

        # Step 6: Create GatewayEffectRequest
        request_expiry = now + timedelta(minutes=5)
        gateway_request = GatewayEffectRequest(
            control_domain=PAVILION_CONTROL_DOMAIN,
            principal_identity=request.principal_identity,
            agent_identity=request.agent_identity,
            mission_id=request.mission_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            delegation_grant_id=request.delegation_grant_id,
            delegation_grant_fingerprint=grant.grant_fingerprint,
            requested_capability=request.requested_capability,
            effect_intent_id=effect_intent_id,
            effect_dispatch_id=effect_dispatch_id,
            authority_reservation_id=authority_reservation_id,
            operation_digest=op_digest,
            idempotency_key=idempotency_key,
            provider_id=PAVILION_PROVIDER_ID,
            adapter_id=PAVILION_ADAPTER_ID,
            effect_consequence=EffectConsequence.PRIVILEGED_EXECUTION,
            provider_reconcilability=ProviderReconcilability.NONE,
            request_timestamp=now,
            request_expiry=request_expiry,
            credential_scope=PAVILION_CREDENTIAL_SCOPE,
        )

        # Step 7: Claim gateway dispatch
        try:
            gateway_claim_id, request_fingerprint = self.gateway.claim_dispatch(
                gateway_request,
                owner_identity=request.principal_identity,
            )
        except GatewayDenied as exc:
            raise CoordinatorDenied(f"Gateway claim denied: {exc}") from exc

        # Step 8: Issue gateway permit
        try:
            permit_id, permit_token = self.gateway.issue_dispatch_permit(
                gateway_request,
                gateway_claim_id,
            )
        except GatewayDenied as exc:
            raise CoordinatorDenied(f"Gateway permit issuance denied: {exc}") from exc

        # Step 9: Create authorization envelope
        envelope = PavilionAuthorizationEnvelope(
            control_domain=PAVILION_CONTROL_DOMAIN,
            provider_id=PAVILION_PROVIDER_ID,
            adapter_id=PAVILION_ADAPTER_ID,
            action=request.action,
            effect_intent_id=effect_intent_id,
            effect_dispatch_id=effect_dispatch_id,
            authority_reservation_id=authority_reservation_id,
            gateway_claim_id=gateway_claim_id,
            delegation_grant_id=request.delegation_grant_id,
            delegation_grant_fingerprint=grant.grant_fingerprint,
            requested_capability=request.requested_capability,
            operation_digest=op_digest,
            idempotency_key=idempotency_key,
            credential_scope=PAVILION_CREDENTIAL_SCOPE,
            permit_token=permit_token,
        )

        # Step 10: Invoke adapter (subprocess or direct call for testing)
        if self.adapter_path and provider_registry is None:
            # Subprocess invocation for production
            try:
                result = subprocess.run(
                    [str(self.adapter_path)],
                    input=envelope.to_json(),
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )

                if result.returncode == 3:
                    # Adapter denied
                    raise CoordinatorError(f"Adapter denied: {result.stderr}")

                if result.returncode != 0:
                    # Adapter error after authorization - effect indeterminate
                    return self._handle_indeterminate(
                        effect_intent_id=effect_intent_id,
                        effect_dispatch_id=effect_dispatch_id,
                        authority_reservation_id=authority_reservation_id,
                        gateway_claim_id=gateway_claim_id,
                        detail=f"Adapter failed after authorization: {result.stderr}",
                    )

                # Parse adapter receipt from stdout
                try:
                    receipt_data = json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    return self._handle_indeterminate(
                        effect_intent_id=effect_intent_id,
                        effect_dispatch_id=effect_dispatch_id,
                        authority_reservation_id=authority_reservation_id,
                        gateway_claim_id=gateway_claim_id,
                        detail=f"Adapter returned invalid JSON: {exc}",
                    )

            except subprocess.TimeoutExpired:
                return self._handle_indeterminate(
                    effect_intent_id=effect_intent_id,
                    effect_dispatch_id=effect_dispatch_id,
                    authority_reservation_id=authority_reservation_id,
                    gateway_claim_id=gateway_claim_id,
                    detail="Adapter subprocess timed out after authorization",
                )
            except Exception as exc:
                return self._handle_indeterminate(
                    effect_intent_id=effect_intent_id,
                    effect_dispatch_id=effect_dispatch_id,
                    authority_reservation_id=authority_reservation_id,
                    gateway_claim_id=gateway_claim_id,
                    detail=f"Adapter invocation failed: {exc}",
                )

        else:
            # Direct call for testing with provider_registry
            if provider_registry is None:
                raise CoordinatorError("No adapter_path and no provider_registry for testing")

            from pavilionos.canonical_adapter import CanonicalPavilionAdapter, ProviderResult

            # Create adapter instance for testing
            adapter = CanonicalPavilionAdapter(
                gateway=self.gateway,
                durable_store=self.store,
                provider_registry=provider_registry,
                adapter_id=PAVILION_ADAPTER_ID,
            )

            try:
                adapter_receipt = adapter.dispatch(envelope)
                receipt_data = {
                    "receipt_id": adapter_receipt.receipt_id,
                    "gateway_claim_id": adapter_receipt.gateway_claim_id,
                    "effect_intent_id": adapter_receipt.effect_intent_id,
                    "effect_dispatch_id": adapter_receipt.effect_dispatch_id,
                    "task_succeeded": adapter_receipt.task_succeeded,
                    "task_error": adapter_receipt.task_error,
                    "effect_status": adapter_receipt.effect_status,
                    "detail": adapter_receipt.detail,
                    "observations": adapter_receipt.observations,
                }
            except Exception as exc:
                return self._handle_indeterminate(
                    effect_intent_id=effect_intent_id,
                    effect_dispatch_id=effect_dispatch_id,
                    authority_reservation_id=authority_reservation_id,
                    gateway_claim_id=gateway_claim_id,
                    detail=f"Adapter dispatch failed: {exc}",
                )

        # Step 11: Parse effect status first to determine if receipt can be recorded
        effect_status = EffectState(receipt_data["effect_status"])
        task_succeeded = receipt_data["task_succeeded"]
        task_error = receipt_data.get("task_error")

        # Step 12: Record gateway receipt (ONLY if not indeterminate)
        # Indeterminate effects cannot have verified receipts
        receipt_recorded = False
        if effect_status != EffectState.INDETERMINATE:
            try:
                self.gateway.record_receipt(
                    gateway_claim_id=gateway_claim_id,
                    control_domain=PAVILION_CONTROL_DOMAIN,
                )
                receipt_recorded = True
            except Exception as exc:
                # Receipt recording failure doesn't change effect truth
                pass

        # Step 13: Determine authority disposition
        if effect_status == EffectState.NOTHING_LANDED:
            authority_disposition = AuthorityDisposition.RELEASED
        elif effect_status == EffectState.SOMETHING_LANDED:
            authority_disposition = AuthorityDisposition.CONSUMED
        else:
            authority_disposition = AuthorityDisposition.RESERVED

        gateway_result = GatewayEffectResult(
            task_succeeded=task_succeeded,
            task_error=task_error,
            effect_status=effect_status,
            dispatch_attempted=True,
            handoff_started=True,
            receipt_recorded=receipt_recorded,
            authority_disposition=authority_disposition,
            reconciliation_required=(effect_status == EffectState.INDETERMINATE),
            reconciliation_obligation_id=(
                f"pavilion-obligation-{effect_intent_id}" if effect_status == EffectState.INDETERMINATE else None
            ),
            gateway_claim_id=gateway_claim_id,
            effect_intent_id=effect_intent_id,
            effect_dispatch_id=effect_dispatch_id,
        )

        try:
            self.gateway.record_effect_result(
                gateway_claim_id=gateway_claim_id,
                control_domain=PAVILION_CONTROL_DOMAIN,
                result=gateway_result,
            )
        except Exception as exc:
            # Result recording failure doesn't change effect truth
            pass

        return CoordinatorResult(
            task_succeeded=task_succeeded,
            task_error=task_error,
            effect_status=effect_status,
            effect_intent_id=effect_intent_id,
            effect_dispatch_id=effect_dispatch_id,
            authority_reservation_id=authority_reservation_id,
            authority_disposition=authority_disposition,
            gateway_claim_id=gateway_claim_id,
            receipt_id=receipt_data.get("receipt_id"),
            detail=receipt_data.get("detail", ""),
        )

    def _handle_indeterminate(
        self,
        effect_intent_id: str,
        effect_dispatch_id: str,
        authority_reservation_id: str,
        gateway_claim_id: str,
        detail: str,
    ) -> CoordinatorResult:
        """Handle indeterminate effect after HANDOFF_STARTED."""
        # Effect may have landed - cannot classify as NOTHING_LANDED
        return CoordinatorResult(
            task_succeeded=False,
            task_error=detail,
            effect_status=EffectState.INDETERMINATE,
            effect_intent_id=effect_intent_id,
            effect_dispatch_id=effect_dispatch_id,
            authority_reservation_id=authority_reservation_id,
            authority_disposition=AuthorityDisposition.RESERVED,
            gateway_claim_id=gateway_claim_id,
            receipt_id=None,
            detail=detail,
        )


__all__ = [
    "CanonicalPavilionCoordinator",
    "PavilionActionRequest",
    "CoordinatorResult",
    "CoordinatorError",
    "CoordinatorDenied",
    "PAVILION_CONTROL_DOMAIN",
    "PAVILION_PROVIDER_ID",
    "PAVILION_ADAPTER_ID",
    "PAVILION_CREDENTIAL_SCOPE",
]