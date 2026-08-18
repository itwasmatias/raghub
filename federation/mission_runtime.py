"""MissionaryX Mission Runtime v0.1 - Durable mission orchestration.

This module provides the public API for durable mission lifecycle management.

Critical architectural boundaries:
- Mission Runtime owns mission lifecycle state
- Governed Effect Gateway owns effect outcome state
- Mission failure ≠ Effect failure

The runtime ensures mission state survives process restarts without confusing
mission status with real-world effect truth.

Public API:
- create_mission(): Create new mission with CREATED state
- get_mission(): Retrieve mission specification and state
- start_mission(): Transition CREATED → RUNNING
- pause_mission(): Transition RUNNING → PAUSED
- resume_mission(): Transition PAUSED → RUNNING
- complete_mission(): Transition to COMPLETED (terminal)
- fail_mission(): Transition to FAILED (terminal)
- cancel_mission(): Transition to CANCELLED (terminal)
- create_checkpoint(): Record mission progress
- list_checkpoints(): Get mission checkpoints
- list_transitions(): Get mission history
- add_effect_reference(): Link effect without claiming truth
- list_effect_references(): Get effect references
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from federation.mission_runtime_store import (
    MissionRuntimeStore,
    MissionNotFoundError,
    MissionRevisionConflictError,
    IllegalMissionTransitionError,
    DomainMismatchError,
)
from federation.mission_state import (
    EffectReference,
    MissionCheckpoint,
    MissionLifecycle,
    MissionSpecification,
    MissionTransition,
)


class MissionRuntimeError(Exception):
    """Base error for mission runtime operations."""


class MissionRuntime:
    """Durable mission orchestration runtime.

    Provides high-level API for mission lifecycle management with durable state,
    optimistic concurrency control, and strict separation from effect truth.
    """

    def __init__(self, store: MissionRuntimeStore | None = None, db_path: Path | str | None = None) -> None:
        """Initialize mission runtime.

        Args:
            store: Existing store instance (for testing). If None, creates new store.
            db_path: Path to SQLite database. Only used if store is None.
        """
        if store is not None:
            self._store = store
        else:
            self._store = MissionRuntimeStore(db_path=db_path)

    def create_mission(
        self,
        mission_id: str,
        control_domain: str,
        objective: str,
        owner_identity: str,
        agent_identity: str | None = None,
        success_criteria: str | None = None,
        constraints: str | None = None,
        deadline: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MissionSpecification:
        """Create a new mission with initial CREATED state.

        Args:
            mission_id: Unique mission identifier (domain-scoped)
            control_domain: ControlDomain identifier
            objective: Mission objective description
            owner_identity: Identity of mission owner
            agent_identity: Optional assigned agent identity
            success_criteria: Optional success criteria
            constraints: Optional mission constraints
            deadline: Optional mission deadline
            metadata: Optional bounded metadata dictionary

        Returns:
            Created mission specification

        Raises:
            MissionRuntimeError: If mission already exists or validation fails
        """
        spec = MissionSpecification(
            mission_id=mission_id,
            control_domain=control_domain,
            objective=objective,
            owner_identity=owner_identity,
            agent_identity=agent_identity,
            success_criteria=success_criteria,
            constraints=constraints,
            deadline=deadline,
            metadata=metadata or {},
            created_at=datetime.now(timezone.utc),
        )

        try:
            self._store.create_mission(spec)
        except Exception as exc:
            raise MissionRuntimeError(f"Failed to create mission: {exc}") from exc

        return spec

    def get_mission(
        self, control_domain: str, mission_id: str
    ) -> tuple[MissionSpecification, MissionLifecycle, int, datetime]:
        """Get mission specification and current state.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            (specification, current_state, revision, updated_at)

        Raises:
            MissionNotFoundError: If mission does not exist
        """
        return self._store.get_mission(control_domain, mission_id)

    def start_mission(
        self, control_domain: str, mission_id: str, expected_revision: int, reason: str | None = None
    ) -> int:
        """Start a mission (transition CREATED → RUNNING).

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            expected_revision: Current revision for concurrency control
            reason: Optional start reason

        Returns:
            New revision number

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRevisionConflictError: If expected_revision is stale
            IllegalMissionTransitionError: If not in CREATED state
        """
        return self._store.transition_mission(
            control_domain=control_domain,
            mission_id=mission_id,
            expected_revision=expected_revision,
            to_state=MissionLifecycle.RUNNING,
            reason=reason or "Mission started",
        )

    def pause_mission(
        self, control_domain: str, mission_id: str, expected_revision: int, reason: str | None = None
    ) -> int:
        """Pause a running mission (transition RUNNING → PAUSED).

        Paused missions can be resumed later. The mission state is durable.
        Process restart does NOT automatically resume a paused mission.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            expected_revision: Current revision for concurrency control
            reason: Optional pause reason

        Returns:
            New revision number

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRevisionConflictError: If expected_revision is stale
            IllegalMissionTransitionError: If not in RUNNING state
        """
        return self._store.transition_mission(
            control_domain=control_domain,
            mission_id=mission_id,
            expected_revision=expected_revision,
            to_state=MissionLifecycle.PAUSED,
            reason=reason or "Mission paused",
        )

    def resume_mission(
        self, control_domain: str, mission_id: str, expected_revision: int, reason: str | None = None
    ) -> int:
        """Resume a paused mission (transition PAUSED → RUNNING).

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            expected_revision: Current revision for concurrency control
            reason: Optional resume reason

        Returns:
            New revision number

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRevisionConflictError: If expected_revision is stale
            IllegalMissionTransitionError: If not in PAUSED state
        """
        return self._store.transition_mission(
            control_domain=control_domain,
            mission_id=mission_id,
            expected_revision=expected_revision,
            to_state=MissionLifecycle.RUNNING,
            reason=reason or "Mission resumed",
        )

    def complete_mission(
        self, control_domain: str, mission_id: str, expected_revision: int, reason: str | None = None
    ) -> int:
        """Complete a mission (transition to COMPLETED terminal state).

        IMPORTANT: Mission completion does NOT imply all effects succeeded.
        Effect truth is owned by the Governed Effect Gateway.

        Callers should NOT complete a mission with unresolved effect obligations
        unless the mission contract explicitly permits completion-with-obligations.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            expected_revision: Current revision for concurrency control
            reason: Optional completion reason

        Returns:
            New revision number

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRevisionConflictError: If expected_revision is stale
            IllegalMissionTransitionError: If already terminal
        """
        return self._store.transition_mission(
            control_domain=control_domain,
            mission_id=mission_id,
            expected_revision=expected_revision,
            to_state=MissionLifecycle.COMPLETED,
            reason=reason or "Mission completed",
        )

    def fail_mission(
        self, control_domain: str, mission_id: str, expected_revision: int, reason: str
    ) -> int:
        """Fail a mission (transition to FAILED terminal state).

        CRITICAL: Mission failure does NOT imply effects failed or were rolled back.
        Effect truth is owned by the Governed Effect Gateway.

        A failed mission may have successfully completed effects that landed.
        Unresolved effect obligations remain unresolved - they are NOT automatically
        inferred to be "nothing_landed".

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            expected_revision: Current revision for concurrency control
            reason: Failure reason (REQUIRED for audit trail)

        Returns:
            New revision number

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRevisionConflictError: If expected_revision is stale
            IllegalMissionTransitionError: If already terminal
        """
        if not reason or not reason.strip():
            raise ValueError("Failure reason is required")

        return self._store.transition_mission(
            control_domain=control_domain,
            mission_id=mission_id,
            expected_revision=expected_revision,
            to_state=MissionLifecycle.FAILED,
            reason=reason,
        )

    def cancel_mission(
        self, control_domain: str, mission_id: str, expected_revision: int, reason: str | None = None
    ) -> int:
        """Cancel a mission (transition to CANCELLED terminal state).

        CRITICAL: Cancellation means "MissionaryX will not schedule new work".
        It does NOT mean "all effects were undone" or "nothing landed".

        Effect references with unresolved status remain unresolved.
        Cancellation is durable and terminal for v0.1.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            expected_revision: Current revision for concurrency control
            reason: Optional cancellation reason

        Returns:
            New revision number

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRevisionConflictError: If expected_revision is stale
            IllegalMissionTransitionError: If already terminal
        """
        return self._store.transition_mission(
            control_domain=control_domain,
            mission_id=mission_id,
            expected_revision=expected_revision,
            to_state=MissionLifecycle.CANCELLED,
            reason=reason or "Mission cancelled",
        )

    def create_checkpoint(
        self,
        control_domain: str,
        mission_id: str,
        mission_state: MissionLifecycle,
        progress_data: dict[str, Any] | None = None,
        reason: str | None = None,
        checkpoint_id: str | None = None,
        sequence: int | None = None,
        *,
        expected_revision: int | None = None,
    ) -> MissionCheckpoint:
        """Create a mission checkpoint (append-only progress evidence).

        Checkpoints record mission progress at a specific point in time.
        They are immutable - later checkpoints do not mutate earlier ones.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            mission_state: Mission state at checkpoint time
            progress_data: Optional bounded progress data
            reason: Optional checkpoint reason
            checkpoint_id: Optional checkpoint ID (auto-generated if None)
            sequence: Optional sequence number (auto-computed if None)
            expected_revision: Optional expected mission revision for concurrency control.
                If supplied, checkpoint creation fails with MissionRevisionConflictError
                if the mission revision has changed since the caller observed it.
                MUST be a positive integer (not bool, not 0, not negative).

        Returns:
            Created checkpoint

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRevisionConflictError: If expected_revision is stale
            MissionRuntimeError: If checkpoint sequence already exists
        """
        # Auto-generate checkpoint_id if not provided
        if checkpoint_id is None:
            checkpoint_id = f"checkpoint-{secrets.token_urlsafe(16)}"

        # Auto-compute sequence if not provided
        if sequence is None:
            existing = self._store.list_checkpoints(control_domain, mission_id)
            sequence = len(existing)

        checkpoint = MissionCheckpoint(
            checkpoint_id=checkpoint_id,
            mission_id=mission_id,
            control_domain=control_domain,
            sequence=sequence,
            mission_state=mission_state,
            progress_data=progress_data or {},
            reason=reason,
            created_at=datetime.now(timezone.utc),
        )

        try:
            self._store.create_checkpoint(checkpoint, expected_revision=expected_revision)
        except MissionNotFoundError:
            # Re-raise MissionNotFoundError without wrapping
            raise
        except MissionRevisionConflictError:
            # Re-raise MissionRevisionConflictError without wrapping
            raise
        except ValueError:
            # Re-raise ValueError without wrapping (validation errors)
            raise
        except Exception as exc:
            raise MissionRuntimeError(f"Failed to create checkpoint: {exc}") from exc

        return checkpoint

    def list_checkpoints(self, control_domain: str, mission_id: str) -> list[MissionCheckpoint]:
        """List all checkpoints for a mission in sequence order.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            List of checkpoints ordered by sequence
        """
        return self._store.list_checkpoints(control_domain, mission_id)

    def list_transitions(self, control_domain: str, mission_id: str) -> list[MissionTransition]:
        """List all lifecycle transitions for a mission in chronological order.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            List of transitions ordered by time
        """
        return self._store.list_transitions(control_domain, mission_id)

    def add_effect_reference(
        self,
        control_domain: str,
        mission_id: str,
        effect_intent_id: str,
        effect_dispatch_id: str | None = None,
        gateway_claim_id: str | None = None,
    ) -> EffectReference:
        """Add an effect reference to a mission.

        CRITICAL: This creates a REFERENCE only. It does NOT:
        - Claim effect outcome truth
        - Infer effect status from mission status
        - Automatically retry effects
        - Change gateway authority

        Effect truth remains owned by the Governed Effect Gateway.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            effect_intent_id: Effect intent identifier from gateway
            effect_dispatch_id: Optional effect dispatch identifier
            gateway_claim_id: Optional gateway claim identifier

        Returns:
            Created effect reference

        Raises:
            MissionNotFoundError: If mission does not exist
        """
        ref = EffectReference(
            effect_intent_id=effect_intent_id,
            effect_dispatch_id=effect_dispatch_id,
            gateway_claim_id=gateway_claim_id,
            referenced_at=datetime.now(timezone.utc),
        )

        try:
            self._store.add_effect_reference(ref, mission_id, control_domain)
        except Exception as exc:
            raise MissionRuntimeError(f"Failed to add effect reference: {exc}") from exc

        return ref

    def list_effect_references(self, control_domain: str, mission_id: str) -> list[EffectReference]:
        """List all effect references for a mission.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            List of effect references
        """
        return self._store.list_effect_references(control_domain, mission_id)


__all__ = [
    "MissionRuntime",
    "MissionRuntimeError",
]
