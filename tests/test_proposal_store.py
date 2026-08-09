"""
Durable Proposal Store Tests

Comprehensive tests for ProposalStore covering:
- Authenticated append-only storage
- Exact idempotency (same proposal_id + same fingerprint)
- Conflict detection (same proposal_id + different fingerprint)
- Restart safety
- Corruption detection
- Concurrent submission safety
- Authority substitution rejection
"""

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from federation.integrity import authentication_tag
from federation.task_request import AuthorizationLevel
from tools.ai_controller.operations_api.proposal_store import (
    ProposalConflict,
    ProposalIntegrityError,
    ProposalLifecycleState,
    ProposalNotFound,
    ProposalRecord,
    ProposalStore,
)


@pytest.fixture
def integrity_key():
    """Create integrity key for testing."""
    return b"test-integrity-key-32-bytes-xxxx"


@pytest.fixture
def store_root(tmp_path):
    """Create temporary store root."""
    return tmp_path / "proposal-store"


@pytest.fixture
def proposal_store(store_root, integrity_key):
    """Create proposal store for testing."""
    return ProposalStore(store_root, integrity_key=integrity_key)


@pytest.fixture
def test_clock():
    """Create deterministic clock for testing."""
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    return lambda: now


@pytest.fixture
def sample_proposal():
    """Create sample proposal data."""
    return {
        "proposal_id": "proposal-001",
        "worker_identity": "ai-worker-1",
        "mission_id": "mission-001",
        "task_id": "task-001",
        "action_type": "inspect_git_status",
        "workspace_id": "workspace-001",
        "immutable_parameters": {},
        "expected_result": "Git repository status",
        "authorization_level": AuthorizationLevel.INTERNAL,
        "approval_required": False,
        "required_capabilities": frozenset(["git"]),
        "proposal_fingerprint": "0" * 64,
        "execution_fingerprint": "1" * 64,
        "approval_execution_fingerprint": None,
    }


class TestProposalStoreBasics:
    """Test basic proposal store operations."""

    def test_submit_new_proposal(self, proposal_store, sample_proposal, test_clock):
        """Submit new proposal should create durable record."""
        record = proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        assert isinstance(record, ProposalRecord)
        assert record.proposal_id == "proposal-001"
        assert record.worker_identity == "ai-worker-1"
        assert record.lifecycle_state == ProposalLifecycleState.VALIDATED
        assert record.created_at is not None
        assert record.updated_at is not None

    def test_get_proposal_by_id(self, proposal_store, sample_proposal, test_clock):
        """Get proposal by ID should return correct record."""
        proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        record = proposal_store.get_proposal("proposal-001")

        assert record is not None
        assert record.proposal_id == "proposal-001"
        assert record.worker_identity == "ai-worker-1"

    def test_get_nonexistent_proposal(self, proposal_store):
        """Get nonexistent proposal should return None."""
        record = proposal_store.get_proposal("nonexistent")
        assert record is None

    def test_update_lifecycle(self, proposal_store, sample_proposal, test_clock):
        """Update lifecycle should modify state."""
        proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        updated = proposal_store.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.ROUTED,
            assignment_id="assignment-001",
            clock=test_clock,
        )

        assert updated.lifecycle_state == ProposalLifecycleState.ROUTED
        assert updated.assignment_id == "assignment-001"


class TestExactIdempotency:
    """Test exact idempotency guarantees."""

    def test_exact_duplicate_returns_existing(self, proposal_store, sample_proposal, test_clock):
        """Exact duplicate (same fingerprint) should return existing record without mutation."""
        # First submission
        record1 = proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        # Exact duplicate
        record2 = proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        # Should be same record (no new write)
        assert record1.proposal_id == record2.proposal_id
        assert record1.proposal_fingerprint == record2.proposal_fingerprint
        assert record1.created_at == record2.created_at

    def test_exact_duplicate_after_restart(self, store_root, integrity_key, sample_proposal, test_clock):
        """Exact duplicate after restart should return existing record."""
        # First session
        store1 = ProposalStore(store_root, integrity_key=integrity_key)
        record1 = store1.submit_proposal(**sample_proposal, clock=test_clock)

        # Simulate restart with new store instance
        store2 = ProposalStore(store_root, integrity_key=integrity_key)
        record2 = store2.submit_proposal(**sample_proposal, clock=test_clock)

        # Should be same record
        assert record1.proposal_id == record2.proposal_id
        assert record1.proposal_fingerprint == record2.proposal_fingerprint
        assert record1.created_at == record2.created_at


class TestConflictDetection:
    """Test conflict detection for proposal substitution attacks."""

    def test_conflicting_duplicate_raises_error(self, proposal_store, sample_proposal, test_clock):
        """Conflicting duplicate (different fingerprint) should raise ProposalConflict."""
        # First submission
        proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        # Conflicting duplicate (same ID, different fingerprint)
        conflicting = {**sample_proposal, "proposal_fingerprint": "f" * 64}

        with pytest.raises(ProposalConflict, match="proposal-001"):
            proposal_store.submit_proposal(**conflicting, clock=test_clock)

    def test_conflicting_duplicate_after_restart(self, store_root, integrity_key, sample_proposal, test_clock):
        """Conflicting duplicate after restart should raise ProposalConflict."""
        # First session
        store1 = ProposalStore(store_root, integrity_key=integrity_key)
        store1.submit_proposal(**sample_proposal, clock=test_clock)

        # Simulate restart
        store2 = ProposalStore(store_root, integrity_key=integrity_key)

        # Attempt conflicting duplicate
        conflicting = {**sample_proposal, "proposal_fingerprint": "f" * 64}

        with pytest.raises(ProposalConflict, match="proposal-001"):
            store2.submit_proposal(**conflicting, clock=test_clock)


class TestConcurrencySafety:
    """Test cross-thread and cross-process concurrency safety."""

    def test_concurrent_exact_duplicates(self, store_root, integrity_key, sample_proposal, test_clock):
        """Concurrent exact duplicates should result in single governed lineage."""
        results = []
        errors = []

        def submit():
            try:
                store = ProposalStore(store_root, integrity_key=integrity_key)
                record = store.submit_proposal(**sample_proposal, clock=test_clock)
                results.append(record)
            except Exception as exc:
                errors.append(exc)

        # Submit concurrently from multiple threads
        threads = [threading.Thread(target=submit) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Should have no errors
        assert not errors

        # All results should be identical (same proposal)
        assert len(results) == 10
        fingerprints = {r.proposal_fingerprint for r in results}
        assert len(fingerprints) == 1  # All same fingerprint
        proposal_ids = {r.proposal_id for r in results}
        assert len(proposal_ids) == 1  # All same ID

    def test_concurrent_conflicting_duplicates(self, store_root, integrity_key, sample_proposal, test_clock):
        """Concurrent conflicting duplicates should fail closed."""
        results = []
        conflicts = []

        def submit(fingerprint_suffix):
            try:
                store = ProposalStore(store_root, integrity_key=integrity_key)
                modified = {
                    **sample_proposal,
                    "proposal_fingerprint": fingerprint_suffix * 64,
                }
                record = store.submit_proposal(**modified, clock=test_clock)
                results.append(record)
            except ProposalConflict as exc:
                conflicts.append(exc)

        # Submit with different fingerprints concurrently
        threads = [
            threading.Thread(target=submit, args=(str(i),))
            for i in range(10)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Exactly one should succeed, others should conflict
        assert len(results) == 1
        assert len(conflicts) == 9


class TestAuthenticationAndIntegrity:
    """Test authenticated storage and integrity protection."""

    def test_authentication_prevents_tampering(self, proposal_store, sample_proposal, test_clock):
        """Tampered evidence should be rejected."""
        # Submit valid proposal
        proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        # Tamper with stored file
        store_file = proposal_store.path
        content = store_file.read_text()
        # Change a character in the middle (corrupting the record)
        tampered = content[:100] + "X" + content[101:]
        store_file.write_text(tampered)

        # Subsequent reads should fail authentication
        with pytest.raises(ProposalIntegrityError):
            proposal_store.snapshot()

    def test_truncated_evidence_rejected(self, proposal_store, sample_proposal, test_clock):
        """Truncated evidence should be rejected."""
        # Submit valid proposal
        proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        # Truncate file
        store_file = proposal_store.path
        content = store_file.read_bytes()
        store_file.write_bytes(content[:-10])  # Remove last 10 bytes

        # Should fail integrity check
        with pytest.raises(ProposalIntegrityError):
            proposal_store.snapshot()

    def test_incomplete_tail_rejected(self, proposal_store, sample_proposal, test_clock):
        """Evidence without final newline should be rejected."""
        # Submit valid proposal
        proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        # Remove final newline
        store_file = proposal_store.path
        content = store_file.read_bytes()
        store_file.write_bytes(content.rstrip(b"\n"))

        # Should fail integrity check
        with pytest.raises(ProposalIntegrityError, match="incomplete tail"):
            proposal_store.snapshot()

    def test_duplicate_json_keys_rejected(self, proposal_store, sample_proposal, test_clock, integrity_key):
        """Evidence with duplicate JSON keys should be rejected."""
        # Manually craft malicious record with duplicate keys
        # Convert frozenset to list for JSON serialization
        payload = {
            **sample_proposal,
            "required_capabilities": list(sample_proposal["required_capabilities"]),
            "lifecycle_state": "validated",
            "created_at": "2026-08-09T12:00:00+00:00",
            "updated_at": "2026-08-09T12:00:00+00:00",
        }
        unsigned = {
            "schema_version": "proposal-store-v0.1",
            "sequence": 1,
            "event_type": "proposal_validated",
            "payload": payload,
            "predecessor_authentication_tag": "0" * 64,
        }

        auth_tag = authentication_tag(
            integrity_key, b"raghub.proposal-store.v0.1", json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        )

        # Create JSON with duplicate key
        malicious = '{"schema_version":"proposal-store-v0.1","sequence":1,"sequence":1,"event_type":"proposal_validated","payload":{},"predecessor_authentication_tag":"' + "0" * 64 + '","authentication_tag":"' + auth_tag + '"}\n'

        proposal_store.root.mkdir(parents=True, exist_ok=True)
        proposal_store.path.write_text(malicious)

        # Should fail with duplicate key error (wrapped in "malformed" message)
        with pytest.raises(ProposalIntegrityError, match="malformed|duplicate"):
            proposal_store.snapshot()

    def test_non_finite_json_rejected(self, proposal_store, sample_proposal, test_clock):
        """Evidence with NaN/Infinity should be rejected."""
        # Manually craft record with NaN (via string manipulation since json.dumps rejects NaN)
        proposal_store.root.mkdir(parents=True, exist_ok=True)

        # Write invalid JSON with NaN
        malicious = '{"schema_version":"proposal-store-v0.1","sequence":1,"event_type":"proposal_validated","payload":{"value":NaN},"predecessor_authentication_tag":"' + "0" * 64 + '","authentication_tag":"' + "1" * 64 + '"}\n'

        proposal_store.path.write_text(malicious)

        # Should fail
        with pytest.raises(ProposalIntegrityError, match="non-finite|malformed"):
            proposal_store.snapshot()


class TestImmutabilityProtection:
    """Test immutable field protection."""

    def test_cannot_change_immutable_fields(self, proposal_store, sample_proposal, test_clock):
        """Lifecycle updates cannot change immutable fields."""
        proposal_store.submit_proposal(**sample_proposal, clock=test_clock)

        # Manually craft update that tries to change immutable field
        # This should be caught by store validation
        from tools.ai_controller.operations_api.proposal_store import _ProposalSnapshot

        snapshot = proposal_store.snapshot()

        def malicious_callback(snap: _ProposalSnapshot):
            # Try to change immutable field
            update = {
                "proposal_id": "proposal-001",
                "proposal_fingerprint": "evil" + "0" * 60,  # Try to change fingerprint
            }
            return update, "lifecycle_updated", None

        # Should fail validation
        with pytest.raises(ProposalIntegrityError, match="cannot change proposal_fingerprint"):
            proposal_store.transact(malicious_callback)


class TestLifecycleTracking:
    """Test lifecycle state tracking."""

    def test_lifecycle_progression(self, proposal_store, sample_proposal, test_clock):
        """Test normal lifecycle progression."""
        # Submit
        record = proposal_store.submit_proposal(**sample_proposal, clock=test_clock)
        assert record.lifecycle_state == ProposalLifecycleState.VALIDATED

        # Route
        record = proposal_store.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.ROUTED,
            task_request_fingerprint="a" * 64,
            assignment_id="assignment-001",
            clock=test_clock,
        )
        assert record.lifecycle_state == ProposalLifecycleState.ROUTED
        assert record.task_request_fingerprint == "a" * 64
        assert record.assignment_id == "assignment-001"

        # Dispatch
        record = proposal_store.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.DISPATCHED,
            dispatch_offer_id="dispatch-001",
            target_node_id="node-001",
            clock=test_clock,
        )
        assert record.lifecycle_state == ProposalLifecycleState.DISPATCHED
        assert record.dispatch_offer_id == "dispatch-001"

        # Complete
        record = proposal_store.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.COMPLETED,
            job_id="job-001",
            result_reference="result-001",
            completed_at="2026-08-09T12:30:00+00:00",
            clock=test_clock,
        )
        assert record.lifecycle_state == ProposalLifecycleState.COMPLETED
        assert record.job_id == "job-001"
        assert record.result_reference == "result-001"
        assert record.completed_at is not None

    def test_update_nonexistent_proposal(self, proposal_store, test_clock):
        """Update nonexistent proposal should raise ProposalNotFound."""
        with pytest.raises(ProposalNotFound, match="proposal-999"):
            proposal_store.update_lifecycle(
                "proposal-999",
                lifecycle_state=ProposalLifecycleState.COMPLETED,
                clock=test_clock,
            )


class TestListProposals:
    """Test proposal listing and filtering."""

    def test_list_all_proposals(self, proposal_store, sample_proposal, test_clock):
        """List all proposals."""
        # Submit multiple proposals
        for i in range(3):
            modified = {
                **sample_proposal,
                "proposal_id": f"proposal-{i:03d}",
                "proposal_fingerprint": str(i) * 64,
            }
            proposal_store.submit_proposal(**modified, clock=test_clock)

        # List all
        proposals = proposal_store.list_proposals()
        assert len(proposals) == 3

    def test_filter_by_lifecycle_state(self, proposal_store, sample_proposal, test_clock):
        """Filter proposals by lifecycle state."""
        # Submit proposals and update some
        for i in range(3):
            modified = {
                **sample_proposal,
                "proposal_id": f"proposal-{i:03d}",
                "proposal_fingerprint": str(i) * 64,
            }
            proposal_store.submit_proposal(**modified, clock=test_clock)

        # Update one to ROUTED
        proposal_store.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.ROUTED,
            clock=test_clock,
        )

        # Filter by VALIDATED
        validated = proposal_store.list_proposals(lifecycle_state=ProposalLifecycleState.VALIDATED)
        assert len(validated) == 2

        # Filter by ROUTED
        routed = proposal_store.list_proposals(lifecycle_state=ProposalLifecycleState.ROUTED)
        assert len(routed) == 1
        assert routed[0].proposal_id == "proposal-001"

    def test_filter_by_mission(self, proposal_store, sample_proposal, test_clock):
        """Filter proposals by mission."""
        # Submit proposals with different missions
        for i in range(3):
            modified = {
                **sample_proposal,
                "proposal_id": f"proposal-{i:03d}",
                "mission_id": f"mission-{i % 2:03d}",
                "proposal_fingerprint": str(i) * 64,
            }
            proposal_store.submit_proposal(**modified, clock=test_clock)

        # Filter by mission-000
        mission_0 = proposal_store.list_proposals(mission_id="mission-000")
        assert len(mission_0) == 2  # proposals 0 and 2

        # Filter by mission-001
        mission_1 = proposal_store.list_proposals(mission_id="mission-001")
        assert len(mission_1) == 1  # proposal 1
