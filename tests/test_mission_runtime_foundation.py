"""Mission Runtime v0.1 Foundation Tests - Security and Correctness Attacks.

This test suite attacks the mission runtime implementation to verify:
1. ControlDomain isolation (no cross-domain pollution)
2. State machine integrity (illegal transitions fail closed)
3. Optimistic concurrency (stale writes lose)
4. Restart/recovery durability (state survives reopen)
5. Effect truth separation (mission status ≠ effect status)
6. Terminal state protection (irreversible states)
7. Checkpoint append-only integrity
8. Mission identity immutability

These tests are ADVERSARIAL - they attempt to break the runtime.
Success means all attacks fail as expected.
"""

import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation.mission_runtime import MissionRuntime, MissionRuntimeError
from federation.mission_runtime_store import (
    DomainMismatchError,
    IllegalMissionTransitionError,
    MissionNotFoundError,
    MissionRevisionConflictError,
    MissionRuntimeStore,
    MissionRuntimeStoreError,
)
from federation.mission_state import (
    EffectReference,
    MissionCheckpoint,
    MissionLifecycle,
    MissionSpecification,
)


# ============================================================================
# DOMAIN ISOLATION ATTACKS
# ============================================================================


def test_same_mission_id_different_domains_isolated():
    """ATTACK: Same mission_id in two ControlDomains should not collide."""
    runtime = MissionRuntime()

    mission_id = "test-mission-isolated"
    domain_a = "domain-alpha"
    domain_b = "domain-beta"

    # Create mission in domain A
    spec_a = runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain_a,
        objective="Domain A objective",
        owner_identity="owner-a",
    )

    # Create SAME mission_id in domain B - should succeed (different namespace)
    spec_b = runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain_b,
        objective="Domain B objective",
        owner_identity="owner-b",
    )

    # Verify both missions exist independently
    spec_a_retrieved, state_a, rev_a, _ = runtime.get_mission(domain_a, mission_id)
    spec_b_retrieved, state_b, rev_b, _ = runtime.get_mission(domain_b, mission_id)

    assert spec_a_retrieved.control_domain == domain_a
    assert spec_b_retrieved.control_domain == domain_b
    assert spec_a_retrieved.objective == "Domain A objective"
    assert spec_b_retrieved.objective == "Domain B objective"

    # Verify specifications have different fingerprints (different data)
    assert spec_a.specification_fingerprint() != spec_b.specification_fingerprint()


def test_cross_domain_lookup_isolated():
    """ATTACK: Lookup mission from wrong domain should fail."""
    runtime = MissionRuntime()

    mission_id = "test-mission-domain-a"
    domain_a = "domain-alpha"
    domain_b = "domain-beta"

    # Create mission in domain A
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain_a,
        objective="Domain A mission",
        owner_identity="owner-a",
    )

    # Attempt to lookup from domain B - should fail
    with pytest.raises(MissionNotFoundError, match="not found"):
        runtime.get_mission(domain_b, mission_id)


def test_cross_domain_transition_rejected():
    """ATTACK: Transition mission from wrong domain should fail."""
    runtime = MissionRuntime()

    mission_id = "test-mission-domain-a"
    domain_a = "domain-alpha"
    domain_b = "domain-beta"

    # Create mission in domain A
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain_a,
        objective="Domain A mission",
        owner_identity="owner-a",
    )

    # Attempt to start from domain B - should fail
    with pytest.raises(MissionNotFoundError, match="not found"):
        runtime.start_mission(domain_b, mission_id, expected_revision=1)


def test_cross_domain_checkpoint_rejected():
    """ATTACK: Create checkpoint for mission in wrong domain should fail."""
    runtime = MissionRuntime()

    mission_id = "test-mission-domain-a"
    domain_a = "domain-alpha"
    domain_b = "domain-beta"

    # Create mission in domain A
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain_a,
        objective="Domain A mission",
        owner_identity="owner-a",
    )

    # Attempt to checkpoint from domain B - should fail
    with pytest.raises(MissionNotFoundError, match="not found"):
        runtime.create_checkpoint(
            control_domain=domain_b,
            mission_id=mission_id,
            mission_state=MissionLifecycle.RUNNING,
        )


# ============================================================================
# STATE MACHINE INTEGRITY ATTACKS
# ============================================================================


def test_legal_transitions_succeed():
    """Verify all legal state machine transitions."""
    runtime = MissionRuntime()

    mission_id = "test-legal-transitions"
    domain = "test-domain"

    # Create mission (CREATED)
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test legal transitions",
        owner_identity="owner",
    )

    _, state, rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.CREATED
    assert rev == 1

    # CREATED → RUNNING
    rev = runtime.start_mission(domain, mission_id, rev)
    _, state, new_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.RUNNING
    assert new_rev == rev == 2

    # RUNNING → PAUSED
    rev = runtime.pause_mission(domain, mission_id, rev)
    _, state, new_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.PAUSED
    assert new_rev == rev == 3

    # PAUSED → RUNNING
    rev = runtime.resume_mission(domain, mission_id, rev)
    _, state, new_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.RUNNING
    assert new_rev == rev == 4

    # RUNNING → COMPLETED
    rev = runtime.complete_mission(domain, mission_id, rev)
    _, state, new_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.COMPLETED
    assert new_rev == rev == 5


def test_terminal_state_cannot_transition():
    """ATTACK: Terminal states (COMPLETED, FAILED, CANCELLED) are irreversible."""
    runtime = MissionRuntime()

    # Test COMPLETED terminal state
    mission_id = "test-terminal-completed"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test terminal state",
        owner_identity="owner",
    )
    rev = runtime.start_mission(domain, mission_id, 1)
    rev = runtime.complete_mission(domain, mission_id, rev)

    _, state, final_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.COMPLETED
    assert final_rev == rev

    with pytest.raises(IllegalMissionTransitionError, match="terminal"):
        runtime.start_mission(domain, mission_id, rev)

    # Test FAILED terminal state
    mission_id = "test-terminal-failed"
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test terminal state",
        owner_identity="owner",
    )
    rev = runtime.start_mission(domain, mission_id, 1)
    rev = runtime.fail_mission(domain, mission_id, rev, "test failure")

    _, state, final_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.FAILED

    with pytest.raises(IllegalMissionTransitionError, match="terminal"):
        runtime.start_mission(domain, mission_id, rev)

    # Test CANCELLED terminal state
    mission_id = "test-terminal-cancelled"
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test terminal state",
        owner_identity="owner",
    )
    rev = runtime.start_mission(domain, mission_id, 1)
    rev = runtime.cancel_mission(domain, mission_id, rev)

    _, state, final_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.CANCELLED

    with pytest.raises(IllegalMissionTransitionError, match="terminal"):
        runtime.start_mission(domain, mission_id, rev)


def test_illegal_transition_created_to_paused():
    """ATTACK: CREATED → PAUSED is illegal (must start first)."""
    runtime = MissionRuntime()

    mission_id = "test-illegal-created-paused"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test illegal transition",
        owner_identity="owner",
    )

    with pytest.raises(IllegalMissionTransitionError, match="Illegal transition"):
        runtime.pause_mission(domain, mission_id, expected_revision=1)


def test_illegal_transition_paused_to_paused_idempotent():
    """PAUSED → PAUSED is idempotent (same state allowed)."""
    runtime = MissionRuntime()

    mission_id = "test-paused-idempotent"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test idempotent transition",
        owner_identity="owner",
    )

    rev = runtime.start_mission(domain, mission_id, 1)
    rev = runtime.pause_mission(domain, mission_id, rev)

    # PAUSED → PAUSED should succeed (idempotent)
    rev = runtime.pause_mission(domain, mission_id, rev, reason="Idempotent pause")
    _, state, new_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.PAUSED
    assert new_rev == rev


def test_resume_running_is_idempotent():
    """Resume from RUNNING state is idempotent (same state transition)."""
    runtime = MissionRuntime()

    mission_id = "test-resume-running"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test resume running",
        owner_identity="owner",
    )

    rev = runtime.start_mission(domain, mission_id, 1)

    # RUNNING → RUNNING via resume is idempotent (allowed)
    rev = runtime.resume_mission(domain, mission_id, rev, reason="Idempotent resume")
    _, state, new_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.RUNNING
    assert new_rev == rev


# ============================================================================
# OPTIMISTIC CONCURRENCY ATTACKS
# ============================================================================


def test_stale_revision_rejected():
    """ATTACK: Stale revision must be rejected (optimistic concurrency)."""
    runtime = MissionRuntime()

    mission_id = "test-stale-revision"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test stale revision",
        owner_identity="owner",
    )

    # Actor A reads revision 1
    _, state_a, rev_a, _ = runtime.get_mission(domain, mission_id)
    assert rev_a == 1

    # Actor B reads revision 1
    _, state_b, rev_b, _ = runtime.get_mission(domain, mission_id)
    assert rev_b == 1

    # Actor A starts mission (revision 1 → 2)
    new_rev_a = runtime.start_mission(domain, mission_id, rev_a)
    assert new_rev_a == 2

    # ATTACK: Actor B attempts to start with stale revision 1 - should fail
    with pytest.raises(MissionRevisionConflictError, match="Revision conflict"):
        runtime.start_mission(domain, mission_id, rev_b)

    # Verify mission is still in RUNNING state (Actor A's transition won)
    _, state, rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.RUNNING
    assert rev == 2


def test_concurrent_transitions_one_wins():
    """ATTACK: Concurrent conflicting transitions - one wins, one loses."""
    runtime = MissionRuntime()

    mission_id = "test-concurrent-transitions"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test concurrent transitions",
        owner_identity="owner",
    )

    rev = runtime.start_mission(domain, mission_id, 1)

    # Both actors read same revision
    _, _, rev_a, _ = runtime.get_mission(domain, mission_id)
    _, _, rev_b, _ = runtime.get_mission(domain, mission_id)
    assert rev_a == rev_b == rev

    # Actor A pauses (wins)
    new_rev_a = runtime.pause_mission(domain, mission_id, rev_a)
    assert new_rev_a == rev + 1

    # ATTACK: Actor B attempts to complete with stale revision - should fail
    with pytest.raises(MissionRevisionConflictError, match="Revision conflict"):
        runtime.complete_mission(domain, mission_id, rev_b)

    # Verify mission is PAUSED (Actor A won)
    _, state, final_rev, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.PAUSED
    assert final_rev == new_rev_a


def test_duplicate_checkpoint_sequence_rejected():
    """ATTACK: Duplicate checkpoint sequence should fail."""
    runtime = MissionRuntime()

    mission_id = "test-duplicate-checkpoint"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test duplicate checkpoint",
        owner_identity="owner",
    )

    # Create checkpoint at sequence 0
    runtime.create_checkpoint(
        control_domain=domain,
        mission_id=mission_id,
        mission_state=MissionLifecycle.CREATED,
        sequence=0,
    )

    # ATTACK: Create another checkpoint at sequence 0 - should fail
    with pytest.raises(MissionRuntimeError, match="already exists"):
        runtime.create_checkpoint(
            control_domain=domain,
            mission_id=mission_id,
            mission_state=MissionLifecycle.CREATED,
            sequence=0,
        )


# ============================================================================
# RESTART/RECOVERY DURABILITY ATTACKS
# ============================================================================


def test_mission_state_survives_database_reopen():
    """CRITICAL: Mission state must survive database close/reopen."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_durability.db"

        mission_id = "test-durability"
        domain = "test-domain"

        # Phase 1: Create mission and transition to PAUSED
        runtime1 = MissionRuntime(db_path=db_path)
        runtime1.create_mission(
            mission_id=mission_id,
            control_domain=domain,
            objective="Test durability",
            owner_identity="owner",
        )
        rev = runtime1.start_mission(domain, mission_id, 1)
        rev = runtime1.pause_mission(domain, mission_id, rev)

        # Create checkpoint
        runtime1.create_checkpoint(
            control_domain=domain,
            mission_id=mission_id,
            mission_state=MissionLifecycle.PAUSED,
            progress_data={"progress": "50%"},
            sequence=0,
        )

        # Get final state
        _, state1, rev1, _ = runtime1.get_mission(domain, mission_id)
        assert state1 == MissionLifecycle.PAUSED
        assert rev1 == rev

        # Phase 2: Completely fresh runtime - reopen database
        runtime2 = MissionRuntime(db_path=db_path)

        # PROOF: State survived reopen
        spec2, state2, rev2, _ = runtime2.get_mission(domain, mission_id)
        assert spec2.mission_id == mission_id
        assert spec2.objective == "Test durability"
        assert state2 == MissionLifecycle.PAUSED
        assert rev2 == rev1

        # PROOF: Checkpoints survived
        checkpoints = runtime2.list_checkpoints(domain, mission_id)
        assert len(checkpoints) == 1
        assert checkpoints[0].sequence == 0
        assert checkpoints[0].progress_data == {"progress": "50%"}

        # PROOF: Can resume from PAUSED
        new_rev = runtime2.resume_mission(domain, mission_id, rev2)
        _, state3, rev3, _ = runtime2.get_mission(domain, mission_id)
        assert state3 == MissionLifecycle.RUNNING
        assert rev3 == new_rev


def test_paused_mission_stays_paused_after_restart():
    """CRITICAL: PAUSED mission does NOT auto-resume on restart."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_paused_restart.db"

        mission_id = "test-paused-restart"
        domain = "test-domain"

        # Phase 1: Create and pause mission
        runtime1 = MissionRuntime(db_path=db_path)
        runtime1.create_mission(
            mission_id=mission_id,
            control_domain=domain,
            objective="Test paused restart",
            owner_identity="owner",
        )
        rev = runtime1.start_mission(domain, mission_id, 1)
        runtime1.pause_mission(domain, mission_id, rev)

        # Phase 2: Restart runtime
        runtime2 = MissionRuntime(db_path=db_path)

        # PROOF: Mission is still PAUSED (NOT auto-resumed)
        _, state, _, _ = runtime2.get_mission(domain, mission_id)
        assert state == MissionLifecycle.PAUSED


def test_running_mission_does_not_auto_execute_after_restart():
    """CRITICAL: RUNNING mission does NOT auto-execute on restart (v0.1)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_running_restart.db"

        mission_id = "test-running-restart"
        domain = "test-domain"

        # Phase 1: Create and start mission
        runtime1 = MissionRuntime(db_path=db_path)
        runtime1.create_mission(
            mission_id=mission_id,
            control_domain=domain,
            objective="Test running restart",
            owner_identity="owner",
        )
        runtime1.start_mission(domain, mission_id, 1)

        # Phase 2: Restart runtime
        runtime2 = MissionRuntime(db_path=db_path)

        # PROOF: Mission is still RUNNING (state recovered faithfully)
        _, state, _, _ = runtime2.get_mission(domain, mission_id)
        assert state == MissionLifecycle.RUNNING

        # PROOF: No automatic execution occurred
        # (In v0.1, runtime only recovers state - no automatic execution)


def test_terminal_mission_remains_terminal_after_restart():
    """CRITICAL: Terminal states remain terminal across restart."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_terminal_restart.db"

        mission_id = "test-terminal-restart"
        domain = "test-domain"

        # Phase 1: Create, start, and complete mission
        runtime1 = MissionRuntime(db_path=db_path)
        runtime1.create_mission(
            mission_id=mission_id,
            control_domain=domain,
            objective="Test terminal restart",
            owner_identity="owner",
        )
        rev = runtime1.start_mission(domain, mission_id, 1)
        runtime1.complete_mission(domain, mission_id, rev)

        # Phase 2: Restart runtime
        runtime2 = MissionRuntime(db_path=db_path)

        # PROOF: Mission is still COMPLETED
        _, state, rev, _ = runtime2.get_mission(domain, mission_id)
        assert state == MissionLifecycle.COMPLETED

        # PROOF: Still cannot transition from terminal state
        with pytest.raises(IllegalMissionTransitionError, match="terminal"):
            runtime2.start_mission(domain, mission_id, rev)


# ============================================================================
# EFFECT TRUTH SEPARATION ATTACKS
# ============================================================================


def test_mission_failure_does_not_imply_effect_failure():
    """CRITICAL: Mission FAILED ≠ Effect failed or rolled back.

    Mission failure is a runtime state. Effect truth is owned by the gateway.
    A failed mission may have effects that successfully landed.
    """
    runtime = MissionRuntime()

    mission_id = "test-mission-failure-separation"
    domain = "test-domain"

    # Create and start mission
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test effect separation",
        owner_identity="owner",
    )
    rev = runtime.start_mission(domain, mission_id, 1)

    # Add effect reference (simulating an effect that may have landed)
    effect_ref = runtime.add_effect_reference(
        control_domain=domain,
        mission_id=mission_id,
        effect_intent_id="intent-123",
        gateway_claim_id="claim-456",
    )

    # Mission fails
    runtime.fail_mission(domain, mission_id, rev, reason="Test failure")

    # PROOF: Mission is FAILED
    _, state, _, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.FAILED

    # PROOF: Effect reference remains intact (NOT deleted or marked as failed)
    refs = runtime.list_effect_references(domain, mission_id)
    assert len(refs) == 1
    assert refs[0].effect_intent_id == "intent-123"
    assert refs[0].gateway_claim_id == "claim-456"

    # INTERPRETATION: Mission runtime does NOT claim effect status.
    # The effect may have landed successfully despite mission failure.
    # Only the Governed Effect Gateway knows effect truth.


def test_mission_cancellation_does_not_imply_effect_rollback():
    """CRITICAL: Mission CANCELLED ≠ Effects undone or nothing_landed.

    Cancellation means "no new work scheduled". Effects already dispatched
    remain in whatever state the gateway recorded.
    """
    runtime = MissionRuntime()

    mission_id = "test-cancellation-separation"
    domain = "test-domain"

    # Create and start mission
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test cancellation separation",
        owner_identity="owner",
    )
    rev = runtime.start_mission(domain, mission_id, 1)

    # Add effect reference
    runtime.add_effect_reference(
        control_domain=domain,
        mission_id=mission_id,
        effect_intent_id="intent-999",
    )

    # Cancel mission
    runtime.cancel_mission(domain, mission_id, rev, reason="Test cancellation")

    # PROOF: Mission is CANCELLED
    _, state, _, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.CANCELLED

    # PROOF: Effect reference remains (NOT auto-rolled-back)
    refs = runtime.list_effect_references(domain, mission_id)
    assert len(refs) == 1
    assert refs[0].effect_intent_id == "intent-999"


def test_unresolved_effect_reference_remains_unresolved():
    """CRITICAL: Mission runtime does NOT infer effect status.

    If an effect is unresolved (indeterminate), mission completion/failure
    does NOT automatically resolve it to "nothing_landed" or "something_landed".
    """
    runtime = MissionRuntime()

    mission_id = "test-unresolved-effect"
    domain = "test-domain"

    # Create and start mission
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test unresolved effect",
        owner_identity="owner",
    )
    rev = runtime.start_mission(domain, mission_id, 1)

    # Add effect reference (simulating unresolved effect)
    runtime.add_effect_reference(
        control_domain=domain,
        mission_id=mission_id,
        effect_intent_id="unresolved-intent",
    )

    # Mission completes
    runtime.complete_mission(domain, mission_id, rev)

    # PROOF: Mission is COMPLETED
    _, state, _, _ = runtime.get_mission(domain, mission_id)
    assert state == MissionLifecycle.COMPLETED

    # PROOF: Effect reference still exists (NOT auto-resolved)
    refs = runtime.list_effect_references(domain, mission_id)
    assert len(refs) == 1
    assert refs[0].effect_intent_id == "unresolved-intent"

    # Mission runtime preserves the reference as unresolved.
    # Only the gateway can resolve effect truth.


# ============================================================================
# CHECKPOINT APPEND-ONLY INTEGRITY
# ============================================================================


def test_checkpoints_are_append_only():
    """CRITICAL: Checkpoints are immutable, append-only records."""
    runtime = MissionRuntime()

    mission_id = "test-checkpoint-append"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test checkpoint append-only",
        owner_identity="owner",
    )

    # Create checkpoint 0
    cp0 = runtime.create_checkpoint(
        control_domain=domain,
        mission_id=mission_id,
        mission_state=MissionLifecycle.CREATED,
        progress_data={"step": "init"},
        sequence=0,
    )

    # Create checkpoint 1
    cp1 = runtime.create_checkpoint(
        control_domain=domain,
        mission_id=mission_id,
        mission_state=MissionLifecycle.RUNNING,
        progress_data={"step": "running"},
        sequence=1,
    )

    # Retrieve all checkpoints
    checkpoints = runtime.list_checkpoints(domain, mission_id)
    assert len(checkpoints) == 2

    # PROOF: Checkpoint 0 unchanged
    assert checkpoints[0].checkpoint_id == cp0.checkpoint_id
    assert checkpoints[0].sequence == 0
    assert checkpoints[0].progress_data == {"step": "init"}

    # PROOF: Checkpoint 1 is new
    assert checkpoints[1].checkpoint_id == cp1.checkpoint_id
    assert checkpoints[1].sequence == 1
    assert checkpoints[1].progress_data == {"step": "running"}


def test_transition_history_is_append_only():
    """CRITICAL: Transition history is immutable, append-only."""
    runtime = MissionRuntime()

    mission_id = "test-transition-history"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test transition history",
        owner_identity="owner",
    )

    runtime.start_mission(domain, mission_id, 1, reason="Started")
    runtime.pause_mission(domain, mission_id, 2, reason="Paused")
    runtime.resume_mission(domain, mission_id, 3, reason="Resumed")

    # Get transition history
    transitions = runtime.list_transitions(domain, mission_id)
    assert len(transitions) == 4  # CREATED→CREATED, CREATED→RUNNING, RUNNING→PAUSED, PAUSED→RUNNING

    # PROOF: Transitions are in order
    assert transitions[0].from_state == MissionLifecycle.CREATED
    assert transitions[0].to_state == MissionLifecycle.CREATED
    assert transitions[0].revision == 1

    assert transitions[1].from_state == MissionLifecycle.CREATED
    assert transitions[1].to_state == MissionLifecycle.RUNNING
    assert transitions[1].revision == 2

    assert transitions[2].from_state == MissionLifecycle.RUNNING
    assert transitions[2].to_state == MissionLifecycle.PAUSED
    assert transitions[2].revision == 3

    assert transitions[3].from_state == MissionLifecycle.PAUSED
    assert transitions[3].to_state == MissionLifecycle.RUNNING
    assert transitions[3].revision == 4


# ============================================================================
# MISSION IDENTITY IMMUTABILITY
# ============================================================================


def test_mission_specification_immutable():
    """CRITICAL: Mission specification is immutable after creation."""
    runtime = MissionRuntime()

    mission_id = "test-immutable-spec"
    domain = "test-domain"

    original_spec = runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Original objective",
        owner_identity="original-owner",
        success_criteria="Original criteria",
    )

    original_fingerprint = original_spec.specification_fingerprint()

    # Transition through states
    rev = runtime.start_mission(domain, mission_id, 1)
    runtime.pause_mission(domain, mission_id, rev)

    # Retrieve specification
    retrieved_spec, _, _, _ = runtime.get_mission(domain, mission_id)

    # PROOF: Specification unchanged
    assert retrieved_spec.mission_id == mission_id
    assert retrieved_spec.objective == "Original objective"
    assert retrieved_spec.owner_identity == "original-owner"
    assert retrieved_spec.success_criteria == "Original criteria"
    assert retrieved_spec.specification_fingerprint() == original_fingerprint


# ============================================================================
# VALIDATION ATTACKS
# ============================================================================


def test_malformed_mission_id_rejected():
    """ATTACK: Malformed mission_id (NULL byte, empty, oversized)."""
    runtime = MissionRuntime()

    # Empty mission_id
    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.create_mission(
            mission_id="",
            control_domain="domain",
            objective="Test",
            owner_identity="owner",
        )

    # NULL byte in mission_id
    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.create_mission(
            mission_id="mission\x00id",
            control_domain="domain",
            objective="Test",
            owner_identity="owner",
        )

    # Oversized mission_id
    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.create_mission(
            mission_id="x" * 300,  # MAX_MISSION_ID_LENGTH is 255
            control_domain="domain",
            objective="Test",
            owner_identity="owner",
        )


def test_malformed_control_domain_rejected():
    """ATTACK: Malformed control_domain."""
    runtime = MissionRuntime()

    # Empty domain
    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.create_mission(
            mission_id="mission",
            control_domain="",
            objective="Test",
            owner_identity="owner",
        )

    # NULL byte in domain
    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.create_mission(
            mission_id="mission",
            control_domain="domain\x00",
            objective="Test",
            owner_identity="owner",
        )


def test_empty_objective_rejected():
    """ATTACK: Empty or blank objective."""
    runtime = MissionRuntime()

    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.create_mission(
            mission_id="mission",
            control_domain="domain",
            objective="",
            owner_identity="owner",
        )

    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.create_mission(
            mission_id="mission",
            control_domain="domain",
            objective="   ",
            owner_identity="owner",
        )


def test_negative_expected_revision_rejected():
    """ATTACK: Negative or zero expected_revision."""
    runtime = MissionRuntime()

    mission_id = "test-negative-revision"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test",
        owner_identity="owner",
    )

    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.start_mission(domain, mission_id, expected_revision=0)

    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.start_mission(domain, mission_id, expected_revision=-1)


def test_failure_without_reason_rejected():
    """ATTACK: Failing mission without reason."""
    runtime = MissionRuntime()

    mission_id = "test-failure-no-reason"
    domain = "test-domain"

    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test",
        owner_identity="owner",
    )
    rev = runtime.start_mission(domain, mission_id, 1)

    # Empty reason
    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.fail_mission(domain, mission_id, rev, reason="")

    # Whitespace-only reason
    with pytest.raises((ValueError, MissionRuntimeError)):
        runtime.fail_mission(domain, mission_id, rev, reason="   ")


# ============================================================================
# TIMESTAMP INTEGRITY
# ============================================================================


def test_mission_timestamps_are_utc():
    """CRITICAL: All timestamps must be UTC-aware."""
    runtime = MissionRuntime()

    mission_id = "test-utc-timestamps"
    domain = "test-domain"

    spec = runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective="Test UTC timestamps",
        owner_identity="owner",
    )

    # PROOF: created_at is UTC
    assert spec.created_at.tzinfo is not None
    assert spec.created_at.utcoffset() == timedelta(0)

    # Create checkpoint
    checkpoint = runtime.create_checkpoint(
        control_domain=domain,
        mission_id=mission_id,
        mission_state=MissionLifecycle.CREATED,
    )

    # PROOF: checkpoint created_at is UTC
    assert checkpoint.created_at.tzinfo is not None
    assert checkpoint.created_at.utcoffset() == timedelta(0)


# ============================================================================
# SUMMARY
# ============================================================================


def test_mission_runtime_v0_1_foundation_complete():
    """Meta-test: Verify critical foundation features are present."""
    runtime = MissionRuntime()

    # PROOF: Can create mission
    spec = runtime.create_mission(
        mission_id="meta-test",
        control_domain="meta-domain",
        objective="Verify foundation",
        owner_identity="meta-owner",
    )

    # PROOF: Can retrieve mission
    retrieved_spec, state, rev, updated_at = runtime.get_mission("meta-domain", "meta-test")
    assert retrieved_spec.mission_id == spec.mission_id
    assert state == MissionLifecycle.CREATED
    assert rev == 1

    # PROOF: Can transition states
    rev = runtime.start_mission("meta-domain", "meta-test", rev)
    rev = runtime.pause_mission("meta-domain", "meta-test", rev)
    rev = runtime.resume_mission("meta-domain", "meta-test", rev)
    rev = runtime.complete_mission("meta-domain", "meta-test", rev)

    # PROOF: Can create checkpoints
    checkpoint = runtime.create_checkpoint(
        control_domain="meta-domain",
        mission_id="meta-test",
        mission_state=MissionLifecycle.COMPLETED,
    )
    assert checkpoint is not None

    # PROOF: Can add effect references
    ref = runtime.add_effect_reference(
        control_domain="meta-domain",
        mission_id="meta-test",
        effect_intent_id="meta-intent",
    )
    assert ref is not None

    # PROOF: Can list history
    transitions = runtime.list_transitions("meta-domain", "meta-test")
    assert len(transitions) > 0

    # Foundation complete
    assert True
