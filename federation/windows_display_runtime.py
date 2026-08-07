"""Windows Display Runtime trusted local composition and intake processing."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from federation.power_action import PowerAction, require_text
from federation.power_adapter import (
    PowerExecutionAuthorization,
    PowerExecutionAuthorizationAuthority,
    PowerRefusalError,
)
from federation.windows_display_adapter import (
    WindowsDisplayAdapter,
    WorkerStateProbe,
)
from federation.windows_display_intake import IntakeEnvelope, IntakeEnvelopeAuthority
from federation.windows_display_runtime_store import IntakeStore
from federation.windows_session import WindowsSessionProbe


class RuntimeMode(str, Enum):
    """Operating mode for the runtime."""

    DISABLED = "disabled"
    DRY_RUN = "dry_run"
    REAL = "real"


class IntakeState(str, Enum):
    """Intake processing states."""

    RECEIVED = "received"
    AUTHENTICATED = "authenticated"
    REFUSED = "refused"
    DRY_RUN_VALIDATED = "dry_run_validated"
    EXECUTION_AUTHORIZED = "execution_authorized"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    RECONCILIATION_REQUIRED = "reconciliation_required"


TERMINAL_STATES = {
    IntakeState.REFUSED,
    IntakeState.SUCCEEDED,
    IntakeState.FAILED,
    IntakeState.EXPIRED,
    IntakeState.DRY_RUN_VALIDATED,  # Dry-run is terminal (envelope consumed)
    IntakeState.RECONCILIATION_REQUIRED,  # Ambiguous crash state is terminal
}


@dataclass(slots=True, frozen=True)
class RuntimeDeployment:
    """Immutable runtime deployment configuration."""

    runtime_identity: str
    worker_identity: str
    component_identity: str
    adapter_identity: str
    policy_version: str
    controller_authority: str
    integrity_authority: str
    intake_authority: str
    execution_authorization_authority: str
    mode: RuntimeMode
    evidence_store_path: str
    native_timeout_ms: int

    def __post_init__(self):
        for field in (
            "runtime_identity",
            "worker_identity",
            "component_identity",
            "adapter_identity",
            "policy_version",
            "controller_authority",
            "integrity_authority",
            "intake_authority",
            "execution_authorization_authority",
            "evidence_store_path",
        ):
            object.__setattr__(
                self,
                field,
                require_text(getattr(self, field), field),
            )

        if not isinstance(self.mode, RuntimeMode):
            if isinstance(self.mode, str):
                object.__setattr__(self, "mode", RuntimeMode(self.mode))
            else:
                raise TypeError("mode must be a RuntimeMode")

        if isinstance(self.native_timeout_ms, bool) or not isinstance(
            self.native_timeout_ms, int
        ):
            raise TypeError("native_timeout_ms must be an integer")
        if not 1 <= self.native_timeout_ms <= 5000:
            raise ValueError("native_timeout_ms must be between 1 and 5000")


@dataclass(slots=True, frozen=True)
class IntakeRecord:
    """Operations Room read model for intake records."""

    envelope_id: str
    runtime_identity: str
    worker_id: str
    component_id: str
    adapter_id: str
    action: str
    proposal_id: str
    intake_sequence: int
    state: IntakeState
    refusal_code: str | None
    received_time: datetime
    expiration: datetime | None
    mode: RuntimeMode
    latest_result: str | None

    def to_dict(self):
        return {
            "envelope_id": self.envelope_id,
            "runtime_identity": self.runtime_identity,
            "worker_id": self.worker_id,
            "component_id": self.component_id,
            "adapter_id": self.adapter_id,
            "action": self.action,
            "proposal_id": self.proposal_id,
            "intake_sequence": self.intake_sequence,
            "state": self.state.value,
            "refusal_code": self.refusal_code,
            "received_time": self.received_time.isoformat() if self.received_time else None,
            "expiration": self.expiration.isoformat() if self.expiration else None,
            "mode": self.mode.value,
            "latest_result": self.latest_result,
        }


class WindowsDisplayRuntime:
    """Trusted local Windows runtime for authorized display control."""

    def __init__(
        self,
        *,
        deployment,
        intake_authority,
        execution_authorization_authority,
        adapter,
        worker_state_probe,
        session_probe,
        intake_store,
        clock,
    ):
        if not isinstance(deployment, RuntimeDeployment):
            raise TypeError("deployment must be a RuntimeDeployment")
        if type(intake_authority) is not IntakeEnvelopeAuthority:
            raise TypeError(
                "intake_authority must be an IntakeEnvelopeAuthority"
            )
        if type(execution_authorization_authority) is not PowerExecutionAuthorizationAuthority:
            raise TypeError(
                "execution_authorization_authority must be a "
                "PowerExecutionAuthorizationAuthority"
            )
        if deployment.mode is RuntimeMode.REAL:
            if not isinstance(adapter, WindowsDisplayAdapter):
                raise TypeError("adapter must be a WindowsDisplayAdapter in real mode")
            if not isinstance(worker_state_probe, WorkerStateProbe):
                raise TypeError("worker_state_probe must be a WorkerStateProbe in real mode")
            if not isinstance(session_probe, WindowsSessionProbe):
                raise TypeError("session_probe must be a WindowsSessionProbe in real mode")
        else:
            if adapter is not None and not isinstance(adapter, WindowsDisplayAdapter):
                raise TypeError("adapter must be a WindowsDisplayAdapter or None")
            if worker_state_probe is not None and not isinstance(
                worker_state_probe, WorkerStateProbe
            ):
                raise TypeError("worker_state_probe must be a WorkerStateProbe or None")
            if session_probe is not None and not isinstance(
                session_probe, WindowsSessionProbe
            ):
                raise TypeError("session_probe must be a WindowsSessionProbe or None")
        if not isinstance(intake_store, IntakeStore):
            raise TypeError("intake_store must be an IntakeStore")
        if not callable(clock):
            raise TypeError("clock must be callable")

        self.deployment = deployment
        self._intake_authority = intake_authority
        self._execution_authorization_authority = execution_authorization_authority
        self._adapter = adapter
        self._worker_state_probe = worker_state_probe
        self._session_probe = session_probe
        self._store = intake_store
        self._clock = clock

    def process_intake(self, envelope_bytes):
        """Process an intake envelope in bytes form with full governance."""
        import hashlib

        if not isinstance(envelope_bytes, bytes):
            raise TypeError("envelope_bytes must be bytes")

        # Disabled mode: refuse without parsing untrusted bytes
        if self.deployment.mode is RuntimeMode.DISABLED:
            return self._refuse("disabled", "Runtime is in disabled mode")

        # Compute envelope content hash for duplicate detection
        envelope_hash = hashlib.sha256(envelope_bytes).hexdigest()

        # Parse and authenticate envelope (intake authentication layer)
        from federation.windows_display_intake import parse_intake_envelope

        try:
            envelope = parse_intake_envelope(
                envelope_bytes,
                self._intake_authority._key,
            )
        except Exception as exc:
            return self._refuse("intake_authentication_failed", str(exc))

        # Validate runtime binding
        if envelope.runtime_identity != self.deployment.runtime_identity:
            return self._refuse(
                "runtime_mismatch",
                "Envelope runtime identity does not match deployment",
            )

        # Reconcile an orphaned execution claim atomically before duplicate
        # handling so an ambiguous native outcome can never be retried.
        existing_state = self._store.reconcile_execution(
            envelope.envelope_id,
            envelope.intake_sequence,
        )
        if (
            existing_state
            and existing_state.get("new_state")
            == IntakeState.RECONCILIATION_REQUIRED.value
        ):
            return self._build_record_from_event(existing_state)

        # Check for duplicate or replay
        is_duplicate, conflict = self._store.check_duplicate_or_replay(
            envelope.envelope_id,
            envelope.intake_sequence,
            envelope_hash,
            envelope.controller_authority,
        )

        if conflict:
            return self._record_and_refuse(envelope, conflict, f"Replay/duplicate attack: {conflict}", envelope_hash)

        if is_duplicate:
            # Exact duplicate - return idempotent result
            existing = self._store.find_intake_state(envelope.envelope_id, envelope.intake_sequence)
            if existing:
                return self._build_record_from_event(existing)
            # Should not happen, but fail safe
            return self._refuse("duplicate_state_missing", "Duplicate but no state found")

        # Validate expiration
        now = self._clock()
        if envelope.expires_at <= now:
            return self._record_and_refuse(
                envelope,
                "expired",
                "Intake envelope is expired",
                envelope_hash,
                use_expired_state=True,
            )

        # Verify embedded authorization (dual authentication)
        if envelope.authorization_record:
            try:
                embedded_auth = PowerExecutionAuthorization(**envelope.authorization_record)
                if not self._execution_authorization_authority.verify(embedded_auth):
                    return self._record_and_refuse(
                        envelope,
                        "embedded_authorization_invalid",
                        "Embedded authorization authentication failed",
                        envelope_hash,
                    )

                # Verify embedded fields match envelope
                if not self._verify_authorization_binding(envelope, embedded_auth):
                    return self._record_and_refuse(
                        envelope,
                        "authorization_binding_mismatch",
                        "Embedded authorization does not match envelope",
                        envelope_hash,
                    )
            except Exception as exc:
                return self._record_and_refuse(
                    envelope,
                    "embedded_authorization_corrupt",
                    f"Failed to verify embedded authorization: {exc}",
                    envelope_hash,
                )

        # Route based on operating mode
        if self.deployment.mode is RuntimeMode.DRY_RUN:
            return self._process_dry_run(envelope, envelope_hash)
        elif self.deployment.mode is RuntimeMode.REAL:
            return self._process_real(envelope, envelope_hash)
        else:
            return self._refuse("invalid_mode", "Invalid runtime mode")

    def _process_dry_run(self, envelope, envelope_hash):
        """Process envelope in dry-run mode (validation only).

        Dry-run performs all authentication and validation but never invokes
        the adapter or creates executable native actions. The envelope is
        consumed and cannot later be reused in real mode.
        """
        now = self._clock()

        # Record RECEIVED state
        self._store.record_event(
            event_type="received",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state=None,
            new_state=IntakeState.RECEIVED.value,
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=envelope_hash,
        )

        # Record AUTHENTICATED state
        self._store.record_event(
            event_type="authenticated",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state=IntakeState.RECEIVED.value,
            new_state=IntakeState.AUTHENTICATED.value,
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=envelope_hash,
        )

        # Record DRY_RUN_VALIDATED terminal state
        self._store.record_event(
            event_type="dry_run_validated",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state=IntakeState.AUTHENTICATED.value,
            new_state=IntakeState.DRY_RUN_VALIDATED.value,
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code="dry_run_success",
            envelope_hash=envelope_hash,
        )

        return IntakeRecord(
            envelope_id=envelope.envelope_id,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            intake_sequence=envelope.intake_sequence,
            state=IntakeState.DRY_RUN_VALIDATED,
            refusal_code=None,
            received_time=now,
            expiration=envelope.expires_at,
            mode=RuntimeMode.DRY_RUN,
            latest_result="dry_run_success",
        )

    def _process_real(self, envelope, envelope_hash):
        """Process envelope in real mode with atomic claim-before-execution."""
        now = self._clock()

        # Record RECEIVED state
        self._store.record_event(
            event_type="received",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state=None,
            new_state=IntakeState.RECEIVED.value,
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=envelope_hash,
        )

        # Record AUTHENTICATED state
        self._store.record_event(
            event_type="authenticated",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state=IntakeState.RECEIVED.value,
            new_state=IntakeState.AUTHENTICATED.value,
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=envelope_hash,
        )

        # Record EXECUTION_AUTHORIZED state
        self._store.record_event(
            event_type="execution_authorized",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state=IntakeState.AUTHENTICATED.value,
            new_state=IntakeState.EXECUTION_AUTHORIZED.value,
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=envelope_hash,
        )

        # ATOMIC CLAIM: Durably record EXECUTING state before adapter invocation
        claimed = self._store.claim_execution(
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            envelope_hash=envelope_hash,
        )

        if not claimed:
            # Already claimed or terminal - concurrent execution prevented
            return IntakeRecord(
                envelope_id=envelope.envelope_id,
                runtime_identity=envelope.runtime_identity,
                worker_id=envelope.worker_id,
                component_id=envelope.component_id,
                adapter_id=envelope.adapter_id,
                action=envelope.action.value,
                proposal_id=envelope.proposal_id,
                intake_sequence=envelope.intake_sequence,
                state=IntakeState.EXECUTING,
                refusal_code="concurrent_execution_prevented",
                received_time=now,
                expiration=envelope.expires_at,
                mode=RuntimeMode.REAL,
                latest_result="Execution already claimed",
            )

        # Create execution authorization (second authentication layer)
        execution_auth = self._execution_authorization_authority.issue(
            proposal_id=envelope.proposal_id,
            adapter_id=envelope.adapter_id,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            action=envelope.action,
            policy_version=envelope.policy_version,
            controller_authority=envelope.controller_authority,
            integrity_authority=envelope.integrity_authority,
            authorization_sequence=envelope.authorization_sequence,
            issued_at=envelope.issued_at,
            expires_at=envelope.expires_at,
        )

        # Invoke adapter (only after durable claim)
        result = self._adapter.attempt_authorized(execution_auth)

        # Record terminal result
        if result.succeeded:
            state = IntakeState.SUCCEEDED
            result_code = "succeeded"
        else:
            state = IntakeState.FAILED
            result_code = result.code.value

        self._store.record_event(
            event_type=state.value,
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state=IntakeState.EXECUTING.value,
            new_state=state.value,
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=result_code,
            envelope_hash=envelope_hash,
        )

        return IntakeRecord(
            envelope_id=envelope.envelope_id,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            intake_sequence=envelope.intake_sequence,
            state=state,
            refusal_code=None if result.succeeded else result_code,
            received_time=now,
            expiration=envelope.expires_at,
            mode=RuntimeMode.REAL,
            latest_result=result_code,
        )

    def _verify_authorization_binding(self, envelope, embedded_auth):
        """Verify that embedded authorization matches envelope fields exactly."""
        return (
            embedded_auth.proposal_id == envelope.proposal_id
            and embedded_auth.adapter_id == envelope.adapter_id
            and embedded_auth.worker_id == envelope.worker_id
            and embedded_auth.component_id == envelope.component_id
            and embedded_auth.action == envelope.action
            and embedded_auth.policy_version == envelope.policy_version
            and embedded_auth.controller_authority == envelope.controller_authority
            and embedded_auth.integrity_authority == envelope.integrity_authority
            and embedded_auth.authorization_sequence == envelope.authorization_sequence
            and embedded_auth.issued_at == envelope.issued_at
            and embedded_auth.expires_at == envelope.expires_at
        )

    def _build_record_from_event(self, event):
        """Build an IntakeRecord from a stored event."""
        from federation.power_action import parse_timestamp

        state_value = event.get("new_state", "unknown")
        try:
            state = IntakeState(state_value)
        except ValueError:
            state = IntakeState.REFUSED

        return IntakeRecord(
            envelope_id=event.get("envelope_id", "unknown"),
            runtime_identity=event.get("runtime_identity", self.deployment.runtime_identity),
            worker_id=event.get("worker_id", "unknown"),
            component_id=event.get("component_id", "unknown"),
            adapter_id=event.get("adapter_id", "unknown"),
            action=event.get("action", "unknown"),
            proposal_id=event.get("proposal_id", "unknown"),
            intake_sequence=event.get("intake_sequence", 0),
            state=state,
            refusal_code=event.get("result_code") if state in TERMINAL_STATES else None,
            received_time=parse_timestamp(event["runtime_timestamp"], "runtime_timestamp"),
            expiration=parse_timestamp(event["expiration"], "expiration") if event.get("expiration") else None,
            mode=self.deployment.mode,
            latest_result=event.get("result_code", "unknown"),
        )

    def _refuse(self, code, reason):
        """Create a refusal record without envelope details."""
        return IntakeRecord(
            envelope_id="unknown",
            runtime_identity=self.deployment.runtime_identity,
            worker_id="unknown",
            component_id="unknown",
            adapter_id="unknown",
            action="unknown",
            proposal_id="unknown",
            intake_sequence=0,
            state=IntakeState.REFUSED,
            refusal_code=code,
            received_time=self._clock(),
            expiration=None,
            mode=self.deployment.mode,
            latest_result=reason,
        )

    def _record_and_refuse(self, envelope, code, reason, envelope_hash, use_expired_state=False):
        """Record refusal event and return refusal record."""
        state = IntakeState.EXPIRED if use_expired_state else IntakeState.REFUSED

        self._store.record_event(
            event_type=state.value,
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state=None,
            new_state=state.value,
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=code,
            envelope_hash=envelope_hash,
        )

        return IntakeRecord(
            envelope_id=envelope.envelope_id,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            intake_sequence=envelope.intake_sequence,
            state=state,
            refusal_code=code,
            received_time=self._clock(),
            expiration=envelope.expires_at,
            mode=self.deployment.mode,
            latest_result=reason,
        )

    def inspect_intake(self, envelope_id=None):
        """Read-only inspection of intake records."""
        events = self._store.inspect()
        if not events:
            return ()

        if envelope_id:
            matching = [e for e in events if e.get("envelope_id") == envelope_id]
            return tuple(matching)

        return events
