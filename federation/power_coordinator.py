"""Authenticated durable coordination for governed worker power actions."""

import fcntl
import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.power_action import (
    POWER_PROPOSAL_DOMAIN,
    ApprovalType,
    ComponentKind,
    GovernedPowerComponent,
    OperationsPowerRecord,
    PowerAction,
    PowerAuditEvent,
    PowerApproval,
    PowerProposal,
    PowerSnapshot,
    PowerStatus,
    TERMINAL_STATUSES,
    canonical_json,
    parse_timestamp,
    timestamp,
)
from federation.power_adapter import (
    PowerConflictError,
    PowerCorruptionError,
    PowerRefusalError,
)
from federation.registry import NodeRegistry
from federation.worker_liveness import LivenessState


_SCHEMA_VERSION = 1
_EVENT_DOMAIN = b"raghub.power-event.v1"
_GENESIS_TAG = "0" * 64
_EVENT_FIELDS = {
    "schema_version",
    "event_sequence",
    "event_type",
    "proposal_id",
    "worker_id",
    "component_id",
    "action",
    "parameters",
    "capability_evidence",
    "policy_version",
    "checkpoint_evidence",
    "wake_path_evidence",
    "approval_actor",
    "approval_type",
    "approval_timestamp",
    "authorization_expiration",
    "adapter_identity",
    "execution_attempt",
    "result",
    "reason",
    "predecessor_tag",
    "controller_timestamp",
    "controller_authority",
    "integrity_authority",
    "authentication_tag",
}
_DISPLAY_ACTIONS = {PowerAction.DISPLAY_OFF, PowerAction.DISPLAY_ON}
_SYSTEM_ACTIONS = {
    PowerAction.SLEEP,
    PowerAction.HIBERNATE,
    PowerAction.SHUTDOWN,
}
_WAKE_ACTIONS = {PowerAction.WAKE_ON_LAN, PowerAction.SCHEDULED_WAKE}
_ADVERTISED_CAPABILITY = {
    PowerAction.DISPLAY_OFF: "display_control",
    PowerAction.DISPLAY_ON: "display_control",
    PowerAction.SLEEP: "sleep",
    PowerAction.HIBERNATE: "hibernate",
    PowerAction.SHUTDOWN: "shutdown",
    PowerAction.WAKE_ON_LAN: "wake_on_lan",
    PowerAction.SCHEDULED_WAKE: "scheduled_wake",
}


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PowerCorruptionError("power evidence has duplicate JSON keys")
        result[key] = value
    return result


class PowerCoordinator:
    """Controller workflow whose target component retains final refusal authority."""

    def __init__(
        self,
        path,
        *,
        node_registry,
        heartbeat_registry,
        controller_authority,
        integrity_authority,
        integrity_key,
        adapters,
        clock,
    ):
        if not isinstance(node_registry, NodeRegistry):
            raise TypeError("node_registry must be a NodeRegistry")
        if not hasattr(heartbeat_registry, "inspect"):
            raise TypeError("heartbeat_registry must provide inspect")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.path = Path(path)
        self.node_registry = node_registry
        self.heartbeat_registry = heartbeat_registry
        self.controller_authority = self._text(
            controller_authority,
            "controller_authority",
        )
        self.integrity_authority = self._text(
            integrity_authority,
            "integrity_authority",
        )
        self._integrity_key = require_integrity_key(integrity_key)
        self.adapters = dict(adapters)
        if any(
            key != getattr(adapter, "adapter_id", None)
            for key, adapter in self.adapters.items()
        ):
            raise ValueError("adapter map identity mismatch")
        self._clock = clock
        if self.path.exists():
            self._decode(self.path.read_bytes())

    @staticmethod
    def _text(value, field):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value.strip()

    def _now(self):
        value = self._clock()
        return parse_timestamp(timestamp(value), "controller_timestamp")

    def register_component(self, component):
        if not isinstance(component, GovernedPowerComponent):
            raise TypeError("component must be a GovernedPowerComponent")
        if self.node_registry.get(component.worker_id) is None:
            raise PowerRefusalError("component worker is not registered")
        if component.controller_authority != self.controller_authority:
            raise PowerRefusalError("component controller authority does not match")
        if component.integrity_authority != self.integrity_authority:
            raise PowerRefusalError("component integrity authority does not match")
        if component.adapter_id not in self.adapters:
            raise PowerRefusalError("component adapter authority is unavailable")

        def mutate(handle, records):
            state = self._rebuild(records)
            existing = state["components"].get(
                (component.worker_id, component.component_id)
            )
            if existing is not None:
                if existing == component:
                    return component
                raise PowerConflictError("component registration changed content")
            self._append(
                handle,
                records,
                event_type="component_registered",
                worker_id=component.worker_id,
                component_id=component.component_id,
                parameters={"component": component.record()},
                policy_version=component.policy.version,
                adapter_identity=component.adapter_id,
            )
            return component

        return self._write(mutate)

    def propose(self, proposal):
        if not isinstance(proposal, PowerProposal):
            raise TypeError("proposal must be a PowerProposal")
        self._validate_proposal_authority(proposal)

        def mutate(handle, records):
            state = self._rebuild(records)
            key = (proposal.worker_id, proposal.component_id)
            component = state["components"].get(key)
            if component is None:
                raise PowerRefusalError("proposal targets an unregistered component")
            existing_by_sequence = state["proposal_sequences"].get(
                (key, proposal.sequence)
            )
            if existing_by_sequence is not None:
                if existing_by_sequence == proposal:
                    return state["snapshots"][proposal.proposal_id]
                raise PowerConflictError(
                    "duplicate proposal sequence has changed content"
                )
            latest_sequence = state["latest_sequences"].get(key, 0)
            if proposal.sequence != latest_sequence + 1:
                raise PowerConflictError(
                    "proposal sequence is replayed, out of order, or non-contiguous"
                )
            self._validate_component_binding(proposal, component)
            now = self._now()
            lease = self.heartbeat_registry.inspect(proposal.worker_id)
            capabilities = self._capabilities(lease)
            self._append(
                handle,
                records,
                event_type="proposed",
                proposal=proposal,
                parameters={"proposal": proposal.record()},
                capability_evidence=sorted(capabilities),
            )
            reason, outcome, requirement = self._evaluate(
                proposal,
                component,
                lease,
                capabilities,
                now,
                state,
            )
            self._append(
                handle,
                records,
                event_type="policy_evaluated",
                proposal=proposal,
                capability_evidence=sorted(capabilities),
                result=outcome.value,
                reason=reason,
            )
            self._append(
                handle,
                records,
                event_type=outcome.value,
                proposal=proposal,
                capability_evidence=sorted(capabilities),
                approval_type=(
                    ApprovalType.AUTOMATIC_POLICY.value
                    if outcome is PowerStatus.AUTO_APPROVED
                    else None
                ),
                approval_actor=(
                    self.controller_authority
                    if outcome is PowerStatus.AUTO_APPROVED
                    else None
                ),
                approval_timestamp=(
                    timestamp(now)
                    if outcome is PowerStatus.AUTO_APPROVED
                    else None
                ),
                result=requirement,
                reason=reason,
            )
            return self._rebuild(records)["snapshots"][proposal.proposal_id]

        return self._write(mutate)

    def approve(self, approval):
        if not isinstance(approval, PowerApproval):
            raise TypeError("approval must be a PowerApproval")

        def mutate(handle, records):
            state = self._rebuild(records)
            proposal = state["proposals"].get(approval.proposal_id)
            if proposal is None:
                raise PowerRefusalError("approval proposal does not exist")
            snapshot = state["snapshots"][approval.proposal_id]
            if snapshot.status is PowerStatus.REFUSED:
                raise PowerRefusalError("refused proposal cannot be approved")
            if snapshot.status in {
                PowerStatus.APPROVED,
                PowerStatus.EXECUTION_AUTHORIZED,
                PowerStatus.EXECUTING,
                PowerStatus.SUCCEEDED,
                PowerStatus.FAILED,
            }:
                accepted = state["approvals"].get(approval.proposal_id)
                if accepted == approval:
                    return snapshot
                raise PowerConflictError("proposal already has different approval")
            if snapshot.status is not PowerStatus.AWAITING_APPROVAL:
                raise PowerRefusalError("proposal is not awaiting explicit approval")
            if approval.approval_type is not ApprovalType.EXPLICIT_USER:
                raise PowerRefusalError("explicit user approval is required")
            if (
                approval.worker_id != proposal.worker_id
                or approval.component_id != proposal.component_id
                or approval.action is not proposal.action
            ):
                raise PowerRefusalError("approval identity does not match proposal")
            if approval.approved_at < proposal.requested_at:
                raise PowerRefusalError("approval predates proposal")
            if proposal.action in _SYSTEM_ACTIONS and (
                approval.checkpoint_evidence != proposal.checkpoint_evidence
                or not approval.protected_work_safe
            ):
                raise PowerRefusalError("approval checkpoint evidence does not match")
            self._append(
                handle,
                records,
                event_type="approved",
                proposal=proposal,
                approval_actor=approval.actor,
                approval_type=approval.approval_type.value,
                approval_timestamp=timestamp(approval.approved_at),
                authorization_expiration=timestamp(approval.expires_at),
                parameters={
                    "approval": {
                        "proposal_id": approval.proposal_id,
                        "worker_id": approval.worker_id,
                        "component_id": approval.component_id,
                        "action": approval.action.value,
                        "approval_type": approval.approval_type.value,
                        "actor": approval.actor,
                        "approved_at": timestamp(approval.approved_at),
                        "expires_at": timestamp(approval.expires_at),
                        "checkpoint_evidence": approval.checkpoint_evidence,
                        "protected_work_safe": approval.protected_work_safe,
                    }
                },
            )
            return self._rebuild(records)["snapshots"][approval.proposal_id]

        return self._write(mutate)

    def authorize(self, proposal_id, *, expires_at):
        proposal_id = self._text(proposal_id, "proposal_id")
        expiration = parse_timestamp(timestamp(expires_at), "expires_at")

        def mutate(handle, records):
            state = self._rebuild(records)
            proposal = state["proposals"].get(proposal_id)
            if proposal is None:
                raise PowerRefusalError("authorization proposal does not exist")
            snapshot = state["snapshots"][proposal_id]
            if snapshot.status in {
                PowerStatus.EXECUTION_AUTHORIZED,
                PowerStatus.EXECUTING,
                PowerStatus.SUCCEEDED,
                PowerStatus.FAILED,
            }:
                if snapshot.authorization_expiration == expiration:
                    return snapshot
                raise PowerConflictError("authorization changed content")
            if snapshot.status not in {
                PowerStatus.AUTO_APPROVED,
                PowerStatus.APPROVED,
            }:
                raise PowerRefusalError("valid approval is required for authorization")
            now = self._now()
            if expiration <= now:
                raise PowerRefusalError("authorization expiration must be in the future")
            if expiration > now + timedelta(hours=1):
                raise PowerRefusalError("authorization exceeds one-hour bound")
            approval = state["approvals"].get(proposal_id)
            if snapshot.status is PowerStatus.APPROVED:
                if approval is None or approval.expires_at <= now:
                    raise PowerRefusalError("approval is expired")
                if expiration > approval.expires_at:
                    raise PowerRefusalError("authorization exceeds approval expiration")
            try:
                self._final_local_validation(proposal, state, now)
            except PowerRefusalError as exc:
                self._append(
                    handle,
                    records,
                    event_type="refused",
                    proposal=proposal,
                    reason=str(exc),
                    result="local_final_validation",
                )
                return self._rebuild(records)["snapshots"][proposal_id]
            component = state["components"][(proposal.worker_id, proposal.component_id)]
            self._append(
                handle,
                records,
                event_type="execution_authorized",
                proposal=proposal,
                authorization_expiration=timestamp(expiration),
                adapter_identity=component.adapter_id,
            )
            return self._rebuild(records)["snapshots"][proposal_id]

        return self._write(mutate)

    def execute(self, proposal_id):
        proposal_id = self._text(proposal_id, "proposal_id")

        def mutate(handle, records):
            state = self._rebuild(records)
            proposal = state["proposals"].get(proposal_id)
            if proposal is None:
                raise PowerRefusalError("execution proposal does not exist")
            snapshot = state["snapshots"][proposal_id]
            if snapshot.status in TERMINAL_STATUSES:
                return snapshot
            if snapshot.status is not PowerStatus.EXECUTION_AUTHORIZED:
                raise PowerRefusalError("execution authorization is required")
            now = self._now()
            if (
                snapshot.authorization_expiration is None
                or snapshot.authorization_expiration <= now
            ):
                self._append(
                    handle,
                    records,
                    event_type="expired",
                    proposal=proposal,
                    reason="execution authorization expired",
                )
                return self._rebuild(records)["snapshots"][proposal_id]
            component = state["components"][(proposal.worker_id, proposal.component_id)]
            attempt = state["attempts"].get(proposal_id, 0) + 1
            try:
                self._final_local_validation(proposal, state, now)
            except PowerRefusalError as exc:
                self._append(
                    handle,
                    records,
                    event_type="executing",
                    proposal=proposal,
                    adapter_identity=component.adapter_id,
                    execution_attempt=attempt,
                )
                self._append(
                    handle,
                    records,
                    event_type="failed",
                    proposal=proposal,
                    adapter_identity=component.adapter_id,
                    execution_attempt=attempt,
                    result="local_refusal",
                    reason=str(exc),
                )
                return self._rebuild(records)["snapshots"][proposal_id]
            adapter = self.adapters.get(component.adapter_id)
            if adapter is None:
                raise PowerRefusalError("authorized adapter is unavailable")
            self._append(
                handle,
                records,
                event_type="executing",
                proposal=proposal,
                adapter_identity=component.adapter_id,
                execution_attempt=attempt,
            )
            try:
                result = adapter.attempt(
                    proposal.action,
                    proposal.worker_id,
                    proposal.component_id,
                )
            except PowerRefusalError as exc:
                self._append(
                    handle,
                    records,
                    event_type="failed",
                    proposal=proposal,
                    adapter_identity=component.adapter_id,
                    execution_attempt=attempt,
                    result="local_refusal",
                    reason=str(exc),
                )
            else:
                self._append(
                    handle,
                    records,
                    event_type=(
                        "reconciliation_required"
                        if result == "reconciliation_required"
                        else "succeeded"
                    ),
                    proposal=proposal,
                    adapter_identity=component.adapter_id,
                    execution_attempt=attempt,
                    result=result,
                )
            return self._rebuild(records)["snapshots"][proposal_id]

        return self._write(mutate)

    def cancel(self, proposal_id, *, actor, reason):
        proposal_id = self._text(proposal_id, "proposal_id")
        actor = self._text(actor, "actor")
        reason = self._text(reason, "reason")

        def mutate(handle, records):
            state = self._rebuild(records)
            proposal = state["proposals"].get(proposal_id)
            if proposal is None:
                raise PowerRefusalError("cancellation proposal does not exist")
            snapshot = state["snapshots"][proposal_id]
            existing = state["cancellations"].get(proposal_id)
            cancellation = (actor, reason)
            if snapshot.status is PowerStatus.CANCELLED:
                if existing == cancellation:
                    return snapshot
                raise PowerConflictError("cancellation changed content")
            if snapshot.status in TERMINAL_STATUSES or snapshot.status is PowerStatus.EXECUTING:
                raise PowerRefusalError("proposal can no longer be cancelled")
            self._append(
                handle,
                records,
                event_type="cancelled",
                proposal=proposal,
                approval_actor=actor,
                parameters={"cancellation": {"actor": actor, "reason": reason}},
                reason=reason,
            )
            return self._rebuild(records)["snapshots"][proposal_id]

        return self._write(mutate)

    def reconcile(self, proposal_id, *, succeeded, reason):
        proposal_id = self._text(proposal_id, "proposal_id")
        if not isinstance(succeeded, bool):
            raise TypeError("succeeded must be a bool")
        reason = self._text(reason, "reason")
        event_type = "succeeded" if succeeded else "failed"

        def mutate(handle, records):
            state = self._rebuild(records)
            proposal = state["proposals"].get(proposal_id)
            if proposal is None:
                raise PowerRefusalError("reconciliation proposal does not exist")
            snapshot = state["snapshots"][proposal_id]
            existing = state["reconciliations"].get(proposal_id)
            reconciliation = (succeeded, reason)
            if snapshot.status in {PowerStatus.SUCCEEDED, PowerStatus.FAILED}:
                if existing == reconciliation:
                    return snapshot
                raise PowerConflictError("reconciliation contradicts terminal evidence")
            if snapshot.status is not PowerStatus.RECONCILIATION_REQUIRED:
                raise PowerRefusalError("proposal does not require reconciliation")
            component = state["components"][
                (proposal.worker_id, proposal.component_id)
            ]
            self._append(
                handle,
                records,
                event_type=event_type,
                proposal=proposal,
                adapter_identity=component.adapter_id,
                execution_attempt=state["attempts"][proposal_id],
                parameters={
                    "reconciliation": {
                        "succeeded": succeeded,
                        "reason": reason,
                    }
                },
                result="reconciled",
                reason=reason,
            )
            return self._rebuild(records)["snapshots"][proposal_id]

        return self._write(mutate)

    def inspect(self, proposal_id):
        state = self._rebuild(self._read())
        try:
            return state["snapshots"][proposal_id]
        except KeyError as exc:
            raise PowerRefusalError("proposal does not exist") from exc

    def audit_history(self):
        return tuple(self._audit_event(record) for record in self._read())

    def operations_view(self, worker_id, component_id):
        state = self._rebuild(self._read())
        key = (worker_id, component_id)
        component = state["components"].get(key)
        if component is None:
            raise PowerRefusalError("component does not exist")
        candidates = [
            snapshot
            for snapshot in state["snapshots"].values()
            if (snapshot.worker_id, snapshot.component_id) == key
        ]
        latest = max(candidates, key=lambda item: item.audit_sequence, default=None)
        pending = (
            None
            if latest is None or latest.status in TERMINAL_STATUSES
            else latest.proposal_id
        )
        return OperationsPowerRecord(
            worker_id=worker_id,
            component_id=component_id,
            supported_actions=tuple(
                action.value for action in component.supported_actions
            ),
            current_governed_state=(
                "registered" if latest is None else latest.status.value
            ),
            pending_proposal=pending,
            approval_requirement=(
                None if latest is None else latest.approval_requirement
            ),
            refusal_reason=None if latest is None else latest.refusal_reason,
            checkpoint_status=(
                "not_applicable" if latest is None else latest.checkpoint_status
            ),
            wake_path_status=(
                "not_applicable" if latest is None else latest.wake_path_status
            ),
            authorization_expiration=(
                None if latest is None else latest.authorization_expiration
            ),
            latest_result=None if latest is None else latest.latest_result,
            audit_sequence=(
                state["last_sequence"] if latest is None else latest.audit_sequence
            ),
        )

    @classmethod
    def inspect_store(
        cls,
        path,
        *,
        integrity_key,
        controller_authority,
        integrity_authority,
    ):
        key = require_integrity_key(integrity_key)
        path = Path(path)
        if not path.exists():
            return ()
        data = path.read_bytes()
        records = cls._decode_records(
            data,
            key,
            controller_authority,
            integrity_authority,
        )
        return tuple(cls._audit_event(record) for record in records)

    def _evaluate(self, proposal, component, lease, capabilities, now, state):
        if proposal.action not in component.supported_actions:
            return (
                "component capability is unsupported",
                PowerStatus.REFUSED,
                "none",
            )
        if _ADVERTISED_CAPABILITY[proposal.action] not in capabilities:
            return (
                "authenticated worker capability is unsupported",
                PowerStatus.REFUSED,
                "none",
            )
        active = [
            snapshot
            for snapshot in state["snapshots"].values()
            if snapshot.worker_id == proposal.worker_id
            and snapshot.component_id == proposal.component_id
            and snapshot.status not in TERMINAL_STATUSES
        ]
        if active:
            return (
                "another power transition is already active",
                PowerStatus.REFUSED,
                "none",
            )
        if proposal.action in _DISPLAY_ACTIONS:
            if any(
                (
                    proposal.checkpoint_evidence is not None,
                    not proposal.protected_work_safe,
                    proposal.wake_path_evidence is not None,
                    proposal.wake_coordinator_id is not None,
                    proposal.execute_after is not None,
                    proposal.expires_at is not None,
                )
            ):
                return (
                    "display action contains unrelated parameters",
                    PowerStatus.REFUSED,
                    "none",
                )
            if lease is None:
                return (
                    "worker is not authenticated",
                    PowerStatus.REFUSED,
                    "none",
                )
            state_value = getattr(lease, "state", None)
            if state_value is not LivenessState.ONLINE:
                label = getattr(state_value, "value", "unknown")
                return (
                    f"worker liveness is {label}",
                    PowerStatus.REFUSED,
                    "none",
                )
            if not component.policy.automatic_display_control:
                return (
                    "local policy denies automatic display control",
                    PowerStatus.REFUSED,
                    "none",
                )
            if component.policy.interactive_session_prohibited:
                return (
                    "local interactive session prohibits display control",
                    PowerStatus.REFUSED,
                    "none",
                )
            return None, PowerStatus.AUTO_APPROVED, "automatic_display_policy"
        if proposal.action in _SYSTEM_ACTIONS:
            if lease is None or getattr(lease, "state", None) is not LivenessState.ONLINE:
                return (
                    "system power action requires an authenticated online worker",
                    PowerStatus.REFUSED,
                    "none",
                )
            if not proposal.checkpoint_evidence:
                return (
                    "checkpoint evidence is required",
                    PowerStatus.REFUSED,
                    "none",
                )
            if not proposal.protected_work_safe:
                return (
                    "protected work could be lost",
                    PowerStatus.REFUSED,
                    "none",
                )
            if proposal.action is PowerAction.SHUTDOWN and (
                not proposal.wake_path_evidence
                or not proposal.wake_coordinator_id
                or proposal.wake_coordinator_id
                != component.policy.approved_wake_coordinator_id
            ):
                return (
                    "shutdown requires a verified wake path and coordinator",
                    PowerStatus.REFUSED,
                    "none",
                )
            return None, PowerStatus.AWAITING_APPROVAL, "explicit_user"
        if proposal.action in _WAKE_ACTIONS:
            if (
                not proposal.wake_coordinator_id
                or proposal.wake_coordinator_id
                != component.policy.approved_wake_coordinator_id
            ):
                return (
                    "wake coordinator is not authorized",
                    PowerStatus.REFUSED,
                    "none",
                )
            if proposal.expires_at is None or proposal.expires_at <= now:
                return (
                    "wake request expiration is absent or expired",
                    PowerStatus.REFUSED,
                    "none",
                )
            if proposal.expires_at > now + timedelta(hours=24):
                return (
                    "wake request expiration exceeds 24-hour bound",
                    PowerStatus.REFUSED,
                    "none",
                )
            if proposal.action is PowerAction.SCHEDULED_WAKE and (
                proposal.execute_after is None
                or proposal.execute_after < now
                or proposal.execute_after >= proposal.expires_at
            ):
                return (
                    "scheduled wake timing is invalid",
                    PowerStatus.REFUSED,
                    "none",
                )
            if lease is not None and getattr(lease, "state", None) in {
                LivenessState.ONLINE,
                LivenessState.WAKING,
            }:
                return (
                    "wake loop prevention refuses an online or waking worker",
                    PowerStatus.REFUSED,
                    "none",
                )
            return None, PowerStatus.AWAITING_APPROVAL, "explicit_user"
        return "action is unsupported", PowerStatus.REFUSED, "none"

    def _final_local_validation(self, proposal, state, now):
        component = state["components"].get(
            (proposal.worker_id, proposal.component_id)
        )
        if component is None:
            raise PowerRefusalError("local component identity does not match")
        self._validate_component_binding(proposal, component)
        if proposal.action not in component.supported_actions:
            raise PowerRefusalError("local component capability is unsupported")
        lease = self.heartbeat_registry.inspect(proposal.worker_id)
        capabilities = self._capabilities(lease)
        if _ADVERTISED_CAPABILITY[proposal.action] not in capabilities:
            raise PowerRefusalError("local authenticated capability is unsupported")
        if proposal.action in _DISPLAY_ACTIONS and (
            lease is None or getattr(lease, "state", None) is not LivenessState.ONLINE
        ):
            raise PowerRefusalError("local worker state makes display action unsafe")
        if proposal.action in _SYSTEM_ACTIONS:
            if lease is None or getattr(lease, "state", None) is not LivenessState.ONLINE:
                raise PowerRefusalError(
                    "local worker state makes system power action unsafe"
                )
            if not proposal.checkpoint_evidence or not proposal.protected_work_safe:
                raise PowerRefusalError("local checkpoint safety validation failed")
            if proposal.action is PowerAction.SHUTDOWN and (
                not proposal.wake_path_evidence
                or proposal.wake_coordinator_id
                != component.policy.approved_wake_coordinator_id
            ):
                raise PowerRefusalError("local shutdown wake path validation failed")
        if proposal.action in _WAKE_ACTIONS:
            if (
                proposal.expires_at is None
                or proposal.expires_at <= now
                or proposal.wake_coordinator_id
                != component.policy.approved_wake_coordinator_id
            ):
                raise PowerRefusalError("local wake validation failed")
            if lease is not None and getattr(lease, "state", None) in {
                LivenessState.ONLINE,
                LivenessState.WAKING,
            }:
                raise PowerRefusalError(
                    "local wake loop prevention refuses an online or waking worker"
                )

    def _validate_proposal_authority(self, proposal):
        if self.node_registry.get(proposal.worker_id) is None:
            raise PowerRefusalError("proposal worker is not registered")
        if proposal.controller_authority != self.controller_authority:
            raise PowerRefusalError("proposal controller authority does not match")
        if proposal.integrity_authority != self.integrity_authority:
            raise PowerRefusalError("proposal integrity authority does not match")
        if not authenticates(
            self._integrity_key,
            POWER_PROPOSAL_DOMAIN,
            canonical_json(proposal.unsigned_record()),
            proposal.authentication_tag,
        ):
            raise PowerRefusalError("proposal authentication is invalid")

    @staticmethod
    def _validate_component_binding(proposal, component):
        if (
            proposal.worker_id != component.worker_id
            or proposal.component_id != component.component_id
        ):
            raise PowerRefusalError("proposal component identity does not match")
        if proposal.policy_version != component.policy.version:
            raise PowerRefusalError("proposal policy version does not match")
        if proposal.controller_authority != component.controller_authority:
            raise PowerRefusalError("proposal controller authority does not match")
        if proposal.integrity_authority != component.integrity_authority:
            raise PowerRefusalError("proposal integrity authority does not match")

    @staticmethod
    def _capabilities(lease):
        if lease is None or not getattr(lease, "authentication_tag", None):
            return set()
        return {
            getattr(capability, "value", capability)
            for capability in getattr(lease, "power_capabilities", ())
        }

    def _write(self, operation):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                records = list(self._decode(handle.read()))
                result = operation(handle, records)
                return result
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists():
            return ()
        with self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._decode(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _decode(self, data):
        return self._decode_records(
            data,
            self._integrity_key,
            self.controller_authority,
            self.integrity_authority,
        )

    @classmethod
    def _decode_records(
        cls,
        data,
        integrity_key,
        controller_authority,
        integrity_authority,
    ):
        if not data:
            return ()
        if not data.endswith(b"\n"):
            raise PowerCorruptionError("power evidence has a truncated record")
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise PowerCorruptionError("power evidence is not UTF-8") from exc
        records = []
        predecessor = _GENESIS_TAG
        for expected_sequence, line in enumerate(lines, 1):
            try:
                record = json.loads(line, object_pairs_hook=_no_duplicate_keys)
            except (json.JSONDecodeError, PowerCorruptionError) as exc:
                raise PowerCorruptionError("power evidence is malformed") from exc
            if not isinstance(record, dict) or set(record) != _EVENT_FIELDS:
                raise PowerCorruptionError("power evidence schema is invalid")
            if (
                record["schema_version"] != _SCHEMA_VERSION
                or record["event_sequence"] != expected_sequence
            ):
                raise PowerCorruptionError("power evidence sequence is invalid")
            if record["predecessor_tag"] != predecessor:
                raise PowerCorruptionError("power predecessor authentication is invalid")
            if record["controller_authority"] != controller_authority:
                raise PowerCorruptionError("power controller authority is invalid")
            if record["integrity_authority"] != integrity_authority:
                raise PowerCorruptionError("power integrity authority is invalid")
            unsigned = dict(record)
            claimed = unsigned.pop("authentication_tag")
            if not authenticates(
                integrity_key,
                _EVENT_DOMAIN,
                canonical_json(unsigned),
                claimed,
            ):
                raise PowerCorruptionError(
                    "power evidence authentication tag is invalid"
                )
            try:
                parse_timestamp(
                    record["controller_timestamp"],
                    "controller_timestamp",
                )
            except (TypeError, ValueError) as exc:
                raise PowerCorruptionError(
                    "power evidence timestamp is invalid"
                ) from exc
            records.append(record)
            predecessor = claimed
        try:
            cls._validate_history(records)
        except (KeyError, TypeError, ValueError, PowerConflictError) as exc:
            raise PowerCorruptionError("power evidence history is contradictory") from exc
        return tuple(records)

    def _append(
        self,
        handle,
        records,
        *,
        event_type,
        worker_id=None,
        component_id=None,
        proposal=None,
        parameters=None,
        capability_evidence=None,
        policy_version=None,
        checkpoint_evidence=None,
        wake_path_evidence=None,
        approval_actor=None,
        approval_type=None,
        approval_timestamp=None,
        authorization_expiration=None,
        adapter_identity=None,
        execution_attempt=None,
        result=None,
        reason=None,
    ):
        if proposal is not None:
            worker_id = proposal.worker_id
            component_id = proposal.component_id
            policy_version = proposal.policy_version
            checkpoint_evidence = proposal.checkpoint_evidence
            wake_path_evidence = proposal.wake_path_evidence
        record = {
            "schema_version": _SCHEMA_VERSION,
            "event_sequence": len(records) + 1,
            "event_type": event_type,
            "proposal_id": None if proposal is None else proposal.proposal_id,
            "worker_id": worker_id,
            "component_id": component_id,
            "action": None if proposal is None else proposal.action.value,
            "parameters": parameters or {},
            "capability_evidence": capability_evidence or [],
            "policy_version": policy_version,
            "checkpoint_evidence": checkpoint_evidence,
            "wake_path_evidence": wake_path_evidence,
            "approval_actor": approval_actor,
            "approval_type": approval_type,
            "approval_timestamp": approval_timestamp,
            "authorization_expiration": authorization_expiration,
            "adapter_identity": adapter_identity,
            "execution_attempt": execution_attempt,
            "result": result,
            "reason": reason,
            "predecessor_tag": (
                _GENESIS_TAG
                if not records
                else records[-1]["authentication_tag"]
            ),
            "controller_timestamp": timestamp(self._now()),
            "controller_authority": self.controller_authority,
            "integrity_authority": self.integrity_authority,
        }
        record["authentication_tag"] = authentication_tag(
            self._integrity_key,
            _EVENT_DOMAIN,
            canonical_json(record),
        )
        handle.seek(0, 2)
        handle.write(canonical_json(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
        records.append(record)

    @classmethod
    def _validate_history(cls, records):
        cls._rebuild(records)

    @classmethod
    def _rebuild(cls, records):
        components = {}
        proposals = {}
        proposal_sequences = {}
        latest_sequences = {}
        snapshots = {}
        approvals = {}
        attempts = {}
        cancellations = {}
        reconciliations = {}
        for record in records:
            event = record["event_type"]
            key = (record["worker_id"], record["component_id"])
            if event == "component_registered":
                component = GovernedPowerComponent.from_record(
                    record["parameters"]["component"]
                )
                existing = components.get(key)
                if existing is not None and existing != component:
                    raise PowerConflictError("contradictory component history")
                components[key] = component
                continue
            proposal_id = record["proposal_id"]
            if event == "proposed":
                proposal = PowerProposal.from_record(
                    record["parameters"]["proposal"]
                )
                if proposal_id in proposals:
                    raise PowerConflictError("duplicate proposal event")
                if key not in components:
                    raise PowerConflictError("proposal precedes component")
                sequence_key = (key, proposal.sequence)
                if sequence_key in proposal_sequences:
                    raise PowerConflictError("duplicate proposal sequence")
                expected = latest_sequences.get(key, 0) + 1
                if proposal.sequence != expected:
                    raise PowerConflictError("proposal sequence is contradictory")
                proposals[proposal_id] = proposal
                proposal_sequences[sequence_key] = proposal
                latest_sequences[key] = proposal.sequence
                snapshots[proposal_id] = PowerSnapshot(
                    proposal_id=proposal_id,
                    worker_id=proposal.worker_id,
                    component_id=proposal.component_id,
                    action=proposal.action,
                    status=PowerStatus.PROPOSED,
                    approval_requirement="pending_policy_evaluation",
                    refusal_reason=None,
                    checkpoint_status=(
                        "present"
                        if proposal.checkpoint_evidence
                        else "not_present"
                    ),
                    wake_path_status=(
                        "verified"
                        if proposal.wake_path_evidence
                        else "not_verified"
                    ),
                    authorization_expiration=None,
                    latest_result=None,
                    audit_sequence=record["event_sequence"],
                )
                continue
            if proposal_id not in proposals:
                raise PowerConflictError("event precedes proposal")
            snapshot = snapshots[proposal_id]
            if snapshot.status in TERMINAL_STATUSES:
                raise PowerConflictError("terminal state has contradictory successor")
            if event == "policy_evaluated":
                snapshots[proposal_id] = replace(
                    snapshot,
                    audit_sequence=record["event_sequence"],
                )
            elif event in {
                PowerStatus.AUTO_APPROVED.value,
                PowerStatus.AWAITING_APPROVAL.value,
                PowerStatus.REFUSED.value,
            }:
                if (
                    event != PowerStatus.REFUSED.value
                    and snapshot.status is not PowerStatus.PROPOSED
                ):
                    raise PowerConflictError("policy outcome is contradictory")
                if (
                    event == PowerStatus.REFUSED.value
                    and snapshot.status
                    not in {
                        PowerStatus.PROPOSED,
                        PowerStatus.AUTO_APPROVED,
                        PowerStatus.APPROVED,
                    }
                ):
                    raise PowerConflictError("policy outcome is contradictory")
                status = PowerStatus(event)
                snapshots[proposal_id] = replace(
                    snapshot,
                    status=status,
                    approval_requirement=record["result"] or "none",
                    refusal_reason=record["reason"],
                    audit_sequence=record["event_sequence"],
                )
            elif event == "approved":
                if snapshot.status is not PowerStatus.AWAITING_APPROVAL:
                    raise PowerConflictError("approval transition is contradictory")
                value = record["parameters"]["approval"]
                approval = PowerApproval(
                    proposal_id=value["proposal_id"],
                    worker_id=value["worker_id"],
                    component_id=value["component_id"],
                    action=PowerAction(value["action"]),
                    approval_type=ApprovalType(value["approval_type"]),
                    actor=value["actor"],
                    approved_at=parse_timestamp(value["approved_at"], "approved_at"),
                    expires_at=parse_timestamp(value["expires_at"], "expires_at"),
                    checkpoint_evidence=value["checkpoint_evidence"],
                    protected_work_safe=value["protected_work_safe"],
                )
                approvals[proposal_id] = approval
                snapshots[proposal_id] = replace(
                    snapshot,
                    status=PowerStatus.APPROVED,
                    audit_sequence=record["event_sequence"],
                )
            elif event == "execution_authorized":
                if snapshot.status not in {
                    PowerStatus.AUTO_APPROVED,
                    PowerStatus.APPROVED,
                }:
                    raise PowerConflictError("authorization transition is contradictory")
                snapshots[proposal_id] = replace(
                    snapshot,
                    status=PowerStatus.EXECUTION_AUTHORIZED,
                    authorization_expiration=parse_timestamp(
                        record["authorization_expiration"],
                        "authorization_expiration",
                    ),
                    audit_sequence=record["event_sequence"],
                )
            elif event == "executing":
                if snapshot.status is not PowerStatus.EXECUTION_AUTHORIZED:
                    raise PowerConflictError("execution transition is contradictory")
                attempts[proposal_id] = record["execution_attempt"]
                snapshots[proposal_id] = replace(
                    snapshot,
                    status=PowerStatus.EXECUTING,
                    audit_sequence=record["event_sequence"],
                )
            elif event in {
                PowerStatus.SUCCEEDED.value,
                PowerStatus.FAILED.value,
                PowerStatus.EXPIRED.value,
                PowerStatus.CANCELLED.value,
                PowerStatus.RECONCILIATION_REQUIRED.value,
            }:
                status = PowerStatus(event)
                if status in {PowerStatus.SUCCEEDED, PowerStatus.FAILED} and (
                    snapshot.status
                    not in {
                        PowerStatus.EXECUTING,
                        PowerStatus.RECONCILIATION_REQUIRED,
                    }
                ):
                    raise PowerConflictError("terminal result is contradictory")
                if status is PowerStatus.RECONCILIATION_REQUIRED and (
                    snapshot.status is not PowerStatus.EXECUTING
                ):
                    raise PowerConflictError("reconciliation state is contradictory")
                if status is PowerStatus.CANCELLED:
                    value = record["parameters"]["cancellation"]
                    cancellations[proposal_id] = (value["actor"], value["reason"])
                if (
                    status in {PowerStatus.SUCCEEDED, PowerStatus.FAILED}
                    and snapshot.status is PowerStatus.RECONCILIATION_REQUIRED
                ):
                    value = record["parameters"]["reconciliation"]
                    reconciliations[proposal_id] = (
                        value["succeeded"],
                        value["reason"],
                    )
                snapshots[proposal_id] = replace(
                    snapshot,
                    status=status,
                    refusal_reason=record["reason"],
                    latest_result=record["result"],
                    audit_sequence=record["event_sequence"],
                )
            else:
                raise PowerConflictError("unknown power event type")
        return {
            "components": components,
            "proposals": proposals,
            "proposal_sequences": proposal_sequences,
            "latest_sequences": latest_sequences,
            "snapshots": snapshots,
            "approvals": approvals,
            "attempts": attempts,
            "cancellations": cancellations,
            "reconciliations": reconciliations,
            "last_sequence": len(records),
        }

    @staticmethod
    def _audit_event(record):
        return PowerAuditEvent(
            sequence=record["event_sequence"],
            event_type=record["event_type"],
            proposal_id=record["proposal_id"],
            worker_id=record["worker_id"],
            component_id=record["component_id"],
            action=record["action"],
            controller_timestamp=parse_timestamp(
                record["controller_timestamp"],
                "controller_timestamp",
            ),
            reason=record["reason"],
            authentication_tag=record["authentication_tag"],
        )
