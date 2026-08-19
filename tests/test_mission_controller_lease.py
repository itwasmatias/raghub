"""Tests for mission controller lease ownership and generation tracking.

MissionaryX v0.1 — Mission Controller Lease Phase A Tests

Tests cover:
- Basic lease operations (acquire, renew, release)
- Generation monotonicity and immutability
- Concurrent lease conflicts
- Clock rollback detection
- Validation attacks
- Schema migration (v1 → v2) with concurrency
- Lease history tracking
"""

import multiprocessing
import secrets
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from federation.control_domain import validate_domain_id
from federation.mission_controller_lease import (
    DEFAULT_CONTROLLER_LEASE_DURATION,
    MAX_CONTROLLER_LEASE_DURATION,
    MissionControllerClockError,
    MissionControllerLease,
    MissionControllerLeaseConflictError,
    MissionControllerLeaseExpiredError,
    MissionControllerLeaseNotFoundError,
    MissionControllerLeaseStaleError,
)
from federation.mission_runtime_store import (
    IllegalMissionTransitionError,
    MISSION_SCHEMA_VERSION,
    MissionNotFoundError,
    MissionRuntimeStore,
    MissionSchemaVersionError,
)
from federation.mission_state import MissionLifecycle, MissionSpecification


class FakeClock:
    """Deterministic clock for testing."""

    def __init__(self, start_time: datetime | None = None):
        self._current_time = start_time or datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._current_time

    def advance(self, delta: timedelta) -> None:
        """Advance clock forward."""
        self._current_time += delta

    def rollback(self, delta: timedelta) -> None:
        """Roll clock backward (simulates clock issues)."""
        self._current_time -= delta


@pytest.fixture
def test_domain():
    """Test control domain."""
    return "test-domain"


@pytest.fixture
def test_mission_id():
    """Test mission ID."""
    return "mission-001"


@pytest.fixture
def fake_clock():
    """Deterministic clock for testing."""
    return FakeClock()


@pytest.fixture
def store(fake_clock):
    """In-memory mission runtime store with fake clock."""
    return MissionRuntimeStore(clock=fake_clock)


@pytest.fixture
def mission(store, test_domain, test_mission_id):
    """Create a test mission."""
    spec = MissionSpecification(
        mission_id=test_mission_id,
        control_domain=test_domain,
        objective="Test mission for controller lease",
        owner_identity="test-owner",
        agent_identity="test-agent",
        metadata=None,
    )
    store.create_mission(spec)
    return spec


def test_acquire_first_lease_creates_generation_1(store, mission, test_domain, test_mission_id):
    """Test that first lease acquisition creates generation 1."""
    lease = store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    assert lease.generation == 1
    assert lease.controller_id == "controller-a"
    assert lease.control_domain == test_domain
    assert lease.mission_id == test_mission_id
    assert lease.released_at is None
    assert lease.acquired_at == lease.renewed_at
    assert lease.expires_at > lease.renewed_at


def test_acquire_idempotent_same_controller_active_lease(store, mission, test_domain, test_mission_id):
    """Test that same controller acquiring active lease is idempotent."""
    lease1 = store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")
    lease2 = store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    assert lease1.generation == lease2.generation == 1
    assert lease1.controller_id == lease2.controller_id
    assert lease1.acquired_at == lease2.acquired_at
    assert lease1.renewed_at == lease2.renewed_at
    assert lease1.expires_at == lease2.expires_at


def test_acquire_fails_conflict_different_controller(store, mission, test_domain, test_mission_id):
    """Test that different controller cannot acquire active lease."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    with pytest.raises(MissionControllerLeaseConflictError) as exc_info:
        store.acquire_controller_lease(test_domain, test_mission_id, "controller-b")

    assert "controller-a" in str(exc_info.value)


def test_acquire_after_expiry_creates_next_generation(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that acquiring after expiry creates generation N+1."""
    lease1 = store.acquire_controller_lease(
        test_domain, test_mission_id, "controller-a",
        lease_duration=timedelta(seconds=10)
    )

    # Advance past expiry
    fake_clock.advance(timedelta(seconds=11))

    lease2 = store.acquire_controller_lease(test_domain, test_mission_id, "controller-b")

    assert lease1.generation == 1
    assert lease2.generation == 2
    assert lease2.controller_id == "controller-b"


def test_acquire_after_release_creates_next_generation(store, mission, test_domain, test_mission_id):
    """Test that acquiring after release creates generation N+1."""
    lease1 = store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")
    store.release_controller_lease(test_domain, test_mission_id, "controller-a", 1)

    lease2 = store.acquire_controller_lease(test_domain, test_mission_id, "controller-b")

    assert lease1.generation == 1
    assert lease2.generation == 2


def test_acquire_fails_terminal_mission(store, mission, test_domain, test_mission_id):
    """Test that lease cannot be acquired for terminal mission."""
    # Transition to terminal state
    _, _, revision, _ = store.get_mission(test_domain, test_mission_id)
    store.transition_mission(test_domain, test_mission_id, revision, MissionLifecycle.RUNNING)
    _, _, revision2, _ = store.get_mission(test_domain, test_mission_id)
    store.transition_mission(test_domain, test_mission_id, revision2, MissionLifecycle.COMPLETED)

    with pytest.raises(IllegalMissionTransitionError):
        store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")


def test_acquire_fails_missing_mission(store, test_domain):
    """Test that lease acquisition fails for non-existent mission."""
    with pytest.raises(MissionNotFoundError):
        store.acquire_controller_lease(test_domain, "nonexistent-mission", "controller-a")


def test_renew_extends_lease(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that renewal extends lease expiration."""
    lease1 = store.acquire_controller_lease(
        test_domain, test_mission_id, "controller-a",
        lease_duration=timedelta(seconds=30)
    )

    # Advance time but before expiry
    fake_clock.advance(timedelta(seconds=10))

    lease2 = store.renew_controller_lease(
        test_domain, test_mission_id, "controller-a", 1,
        lease_duration=timedelta(seconds=30)
    )

    assert lease2.generation == 1
    assert lease2.renewed_at > lease1.renewed_at
    assert lease2.expires_at > lease1.expires_at
    assert lease2.acquired_at == lease1.acquired_at  # acquired_at unchanged


def test_renew_fails_expired_lease(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that renewal fails after expiry."""
    store.acquire_controller_lease(
        test_domain, test_mission_id, "controller-a",
        lease_duration=timedelta(seconds=10)
    )

    # Advance past expiry
    fake_clock.advance(timedelta(seconds=11))

    with pytest.raises(MissionControllerLeaseExpiredError):
        store.renew_controller_lease(test_domain, test_mission_id, "controller-a", 1)


def test_renew_fails_stale_generation(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that renewal fails with stale generation."""
    lease1 = store.acquire_controller_lease(
        test_domain, test_mission_id, "controller-a",
        lease_duration=timedelta(seconds=10)
    )

    # Expire and create generation 2
    fake_clock.advance(timedelta(seconds=11))
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-b")

    # Try to renew generation 1
    with pytest.raises(MissionControllerLeaseStaleError):
        store.renew_controller_lease(test_domain, test_mission_id, "controller-a", 1)


def test_renew_fails_wrong_controller(store, mission, test_domain, test_mission_id):
    """Test that renewal fails with wrong controller_id."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    with pytest.raises(MissionControllerLeaseConflictError):
        store.renew_controller_lease(test_domain, test_mission_id, "controller-b", 1)


def test_renew_fails_released_lease(store, mission, test_domain, test_mission_id):
    """Test that renewal fails for released lease."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")
    store.release_controller_lease(test_domain, test_mission_id, "controller-a", 1)

    with pytest.raises(MissionControllerLeaseExpiredError):
        store.renew_controller_lease(test_domain, test_mission_id, "controller-a", 1)


def test_release_marks_lease_released(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that release marks lease as released."""
    lease1 = store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")
    assert lease1.released_at is None

    released_lease = store.release_controller_lease(test_domain, test_mission_id, "controller-a", 1)

    assert released_lease.released_at is not None
    assert released_lease.generation == 1
    assert not released_lease.is_active(fake_clock())


def test_release_idempotent(store, mission, test_domain, test_mission_id):
    """Test that duplicate release is idempotent."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    released1 = store.release_controller_lease(test_domain, test_mission_id, "controller-a", 1)
    released2 = store.release_controller_lease(test_domain, test_mission_id, "controller-a", 1)

    assert released1.released_at == released2.released_at
    assert released1.generation == released2.generation


def test_release_fails_stale_generation(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that stale generation cannot release newer generation."""
    lease1 = store.acquire_controller_lease(
        test_domain, test_mission_id, "controller-a",
        lease_duration=timedelta(seconds=10)
    )

    # Expire and create generation 2
    fake_clock.advance(timedelta(seconds=11))
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-b")

    # Try to release generation 1 after generation 2 exists
    with pytest.raises(MissionControllerLeaseStaleError):
        store.release_controller_lease(test_domain, test_mission_id, "controller-a", 1)


def test_release_fails_wrong_controller(store, mission, test_domain, test_mission_id):
    """Test that wrong controller cannot release lease."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    with pytest.raises(MissionControllerLeaseConflictError):
        store.release_controller_lease(test_domain, test_mission_id, "controller-b", 1)


def test_get_current_lease_returns_latest(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that get_current_controller_lease returns latest generation."""
    # Create generation 1
    lease1 = store.acquire_controller_lease(test_domain, test_mission_id, "controller-a",
                                           lease_duration=timedelta(seconds=10))

    current = store.get_current_controller_lease(test_domain, test_mission_id)
    assert current.generation == 1

    # Expire and create generation 2
    fake_clock.advance(timedelta(seconds=11))
    lease2 = store.acquire_controller_lease(test_domain, test_mission_id, "controller-b")

    current = store.get_current_controller_lease(test_domain, test_mission_id)
    assert current.generation == 2
    assert current.controller_id == "controller-b"


def test_get_current_lease_none_if_no_lease(store, mission, test_domain, test_mission_id):
    """Test that get_current_controller_lease returns None if no lease exists."""
    current = store.get_current_controller_lease(test_domain, test_mission_id)
    assert current is None


def test_list_lease_history_ordered_by_generation(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that lease history is ordered by generation (oldest first)."""
    # Create generation 1
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a",
                                  lease_duration=timedelta(seconds=10))

    # Expire and create generation 2
    fake_clock.advance(timedelta(seconds=11))
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-b",
                                  lease_duration=timedelta(seconds=10))

    # Expire and create generation 3
    fake_clock.advance(timedelta(seconds=11))
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-c")

    history = store.list_controller_lease_history(test_domain, test_mission_id)

    assert len(history) == 3
    assert history[0].generation == 1
    assert history[0].controller_id == "controller-a"
    assert history[1].generation == 2
    assert history[1].controller_id == "controller-b"
    assert history[2].generation == 3
    assert history[2].controller_id == "controller-c"


def test_lease_duration_bounded(store, mission, test_domain, test_mission_id):
    """Test that lease duration is bounded."""
    # Valid duration
    lease = store.acquire_controller_lease(
        test_domain, test_mission_id, "controller-a",
        lease_duration=timedelta(minutes=2)
    )
    assert lease.expires_at - lease.renewed_at == timedelta(minutes=2)

    # Exceeds maximum
    with pytest.raises(ValueError, match="must not exceed"):
        store.acquire_controller_lease(
            test_domain, test_mission_id, "controller-b",
            lease_duration=timedelta(hours=1)
        )


def test_clock_rollback_detected_on_acquire(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that clock rollback is detected during acquire."""
    lease1 = store.acquire_controller_lease(test_domain, test_mission_id, "controller-a",
                                           lease_duration=timedelta(seconds=5))

    # Roll clock backward
    fake_clock.rollback(timedelta(seconds=10))

    # After rollback, trying to acquire should detect clock went backward
    with pytest.raises(MissionControllerClockError, match="Clock rollback"):
        store.acquire_controller_lease(test_domain, test_mission_id, "controller-b")


def test_clock_rollback_detected_on_renew(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that clock rollback is detected during renew."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    # Roll clock backward
    fake_clock.rollback(timedelta(seconds=10))

    with pytest.raises(MissionControllerClockError, match="Clock rollback"):
        store.renew_controller_lease(test_domain, test_mission_id, "controller-a", 1)


def test_clock_rollback_detected_on_release(store, mission, test_domain, test_mission_id, fake_clock):
    """Test that clock rollback is detected during release."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    # Roll clock backward
    fake_clock.rollback(timedelta(seconds=10))

    with pytest.raises(MissionControllerClockError, match="Clock rollback"):
        store.release_controller_lease(test_domain, test_mission_id, "controller-a", 1)


# ============================================================================
# VALIDATION ATTACKS
# ============================================================================

def test_validation_blank_controller_id(store, mission, test_domain, test_mission_id):
    """Test that blank controller_id is rejected."""
    with pytest.raises(ValueError, match="non-empty"):
        store.acquire_controller_lease(test_domain, test_mission_id, "")


def test_validation_whitespace_controller_id(store, mission, test_domain, test_mission_id):
    """Test that whitespace-only controller_id is rejected."""
    with pytest.raises(ValueError, match="non-empty"):
        store.acquire_controller_lease(test_domain, test_mission_id, "   ")


def test_validation_nul_bearing_controller_id(store, mission, test_domain, test_mission_id):
    """Test that NUL-bearing controller_id is rejected."""
    with pytest.raises(ValueError, match="NUL"):
        store.acquire_controller_lease(test_domain, test_mission_id, "controller\x00evil")


def test_validation_invalid_control_domain(store, mission, test_mission_id):
    """Test that invalid control domain is rejected."""
    with pytest.raises(ValueError):
        store.acquire_controller_lease("", test_mission_id, "controller-a")


def test_validation_generation_true(store, mission, test_domain, test_mission_id):
    """Test that generation=True is rejected."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    with pytest.raises(TypeError, match="exact int"):
        store.renew_controller_lease(test_domain, test_mission_id, "controller-a", True)


def test_validation_generation_zero(store, mission, test_domain, test_mission_id):
    """Test that generation=0 is rejected."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    with pytest.raises(ValueError, match="positive integer"):
        store.renew_controller_lease(test_domain, test_mission_id, "controller-a", 0)


def test_validation_generation_negative(store, mission, test_domain, test_mission_id):
    """Test that negative generation is rejected."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    with pytest.raises(ValueError, match="positive integer"):
        store.renew_controller_lease(test_domain, test_mission_id, "controller-a", -1)


def test_validation_generation_string(store, mission, test_domain, test_mission_id):
    """Test that generation="1" is rejected."""
    store.acquire_controller_lease(test_domain, test_mission_id, "controller-a")

    with pytest.raises(TypeError, match="exact int"):
        store.renew_controller_lease(test_domain, test_mission_id, "controller-a", "1")


def test_validation_duration_zero(store, mission, test_domain, test_mission_id):
    """Test that duration <= 0 is rejected."""
    with pytest.raises(ValueError, match="positive"):
        store.acquire_controller_lease(
            test_domain, test_mission_id, "controller-a",
            lease_duration=timedelta(seconds=0)
        )


def test_validation_duration_negative(store, mission, test_domain, test_mission_id):
    """Test that negative duration is rejected."""
    with pytest.raises(ValueError, match="positive"):
        store.acquire_controller_lease(
            test_domain, test_mission_id, "controller-a",
            lease_duration=timedelta(seconds=-10)
        )


def test_validation_duration_exceeds_maximum(store, mission, test_domain, test_mission_id):
    """Test that duration > maximum is rejected."""
    with pytest.raises(ValueError, match="must not exceed"):
        store.acquire_controller_lease(
            test_domain, test_mission_id, "controller-a",
            lease_duration=MAX_CONTROLLER_LEASE_DURATION + timedelta(seconds=1)
        )


# ============================================================================
# CONCURRENCY ATTACKS
# ============================================================================

def _concurrent_first_acquire_worker(db_path, domain, mission_id, controller_id, barrier, result_queue):
    """Worker for concurrent first-acquire test with deterministic synchronization."""
    try:
        # Synchronize before opening store
        barrier.wait(timeout=5)

        # All processes attempt acquisition simultaneously
        store = MissionRuntimeStore(db_path)
        lease = store.acquire_controller_lease(domain, mission_id, controller_id)
        result_queue.put(("success", controller_id, lease.generation))
    except MissionControllerLeaseConflictError as e:
        result_queue.put(("conflict", controller_id, str(e)))
    except Exception as e:
        result_queue.put(("error", controller_id, str(e), type(e).__name__))


def test_concurrent_first_acquire_single_winner(mission):
    """Test that concurrent first acquire results in single generation 1.

    Uses deterministic Barrier synchronization to force true simultaneity.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create mission in file-backed database
        store = MissionRuntimeStore(db_path)
        spec = MissionSpecification(
            mission_id="mission-concurrent",
            control_domain="test-domain",
            objective="Concurrent test",
            owner_identity="test-owner",
            metadata=None,
        )
        store.create_mission(spec)

        # Launch concurrent acquire attempts with barrier synchronization
        num_processes = 3
        barrier = multiprocessing.Barrier(num_processes)
        result_queue = multiprocessing.Queue()
        processes = []

        for i in range(num_processes):
            p = multiprocessing.Process(
                target=_concurrent_first_acquire_worker,
                args=(db_path, "test-domain", "mission-concurrent", f"controller-{i}", barrier, result_queue)
            )
            processes.append(p)
            p.start()

        # Wait for all processes with timeout and assert exit codes
        for p in processes:
            p.join(timeout=10)
            assert p.exitcode == 0, f"Process failed with exit code {p.exitcode}"

        # Collect exactly num_processes results (no Queue.empty())
        results = []
        for _ in range(num_processes):
            results.append(result_queue.get(timeout=1))

        # Exactly one success with generation 1
        successes = [r for r in results if r[0] == "success"]
        conflicts = [r for r in results if r[0] == "conflict"]
        errors = [r for r in results if r[0] == "error"]

        assert len(results) == num_processes, f"Expected {num_processes} results, got {len(results)}"
        assert len(errors) == 0, f"Unexpected errors: {errors}"
        assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}: {results}"
        assert successes[0][2] == 1, "Winner must have generation 1"
        assert len(conflicts) == num_processes - 1, f"Expected {num_processes - 1} conflicts, got {len(conflicts)}"

        # Verify exactly one generation-1 durable row
        verify_store = MissionRuntimeStore(db_path)
        history = verify_store.list_controller_lease_history("test-domain", "mission-concurrent")
        assert len(history) == 1, f"Expected exactly 1 lease in history, got {len(history)}"
        assert history[0].generation == 1, "Durable lease must be generation 1"

    finally:
        db_path.unlink(missing_ok=True)


def _concurrent_expired_takeover_worker(db_path, domain, mission_id, controller_id, barrier, result_queue):
    """Worker for concurrent expired-takeover test with deterministic synchronization."""
    try:
        # Synchronize before opening store
        barrier.wait(timeout=5)

        # All processes attempt acquisition simultaneously
        store = MissionRuntimeStore(db_path)
        lease = store.acquire_controller_lease(domain, mission_id, controller_id)
        result_queue.put(("success", controller_id, lease.generation))
    except MissionControllerLeaseConflictError as e:
        result_queue.put(("conflict", controller_id, str(e)))
    except Exception as e:
        result_queue.put(("error", controller_id, str(e), type(e).__name__))


def test_concurrent_expired_takeover_single_generation_2(mission, fake_clock):
    """Test that concurrent takeover after expiry results in single generation 2.

    Uses deterministic Barrier synchronization to force true simultaneity.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create mission and generation 1 in file-backed database
        store = MissionRuntimeStore(db_path)
        spec = MissionSpecification(
            mission_id="mission-takeover",
            control_domain="test-domain",
            objective="Takeover test",
            owner_identity="test-owner",
            metadata=None,
        )
        store.create_mission(spec)

        # Create generation 1 with short lease
        lease1 = store.acquire_controller_lease(
            "test-domain", "mission-takeover", "controller-original",
            lease_duration=timedelta(seconds=1)
        )

        # Wait for expiry
        import time
        time.sleep(2)

        # Launch concurrent takeover attempts with barrier synchronization
        num_processes = 3
        barrier = multiprocessing.Barrier(num_processes)
        result_queue = multiprocessing.Queue()
        processes = []

        for i in range(num_processes):
            p = multiprocessing.Process(
                target=_concurrent_expired_takeover_worker,
                args=(db_path, "test-domain", "mission-takeover", f"controller-{i}", barrier, result_queue)
            )
            processes.append(p)
            p.start()

        # Wait for all processes with timeout and assert exit codes
        for p in processes:
            p.join(timeout=10)
            assert p.exitcode == 0, f"Process failed with exit code {p.exitcode}"

        # Collect exactly num_processes results (no Queue.empty())
        results = []
        for _ in range(num_processes):
            results.append(result_queue.get(timeout=1))

        # Exactly one success with generation 2
        successes = [r for r in results if r[0] == "success"]
        conflicts = [r for r in results if r[0] == "conflict"]
        errors = [r for r in results if r[0] == "error"]

        assert len(results) == num_processes, f"Expected {num_processes} results, got {len(results)}"
        assert len(errors) == 0, f"Unexpected errors: {errors}"
        assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}: {results}"
        assert successes[0][2] == 2, "Winner must have generation 2"
        assert len(conflicts) == num_processes - 1, f"Expected {num_processes - 1} conflicts, got {len(conflicts)}"

        # Verify exactly two durable rows (generation 1 + generation 2)
        verify_store = MissionRuntimeStore(db_path)
        history = verify_store.list_controller_lease_history("test-domain", "mission-takeover")
        assert len(history) == 2, f"Expected exactly 2 leases in history, got {len(history)}"
        assert history[0].generation == 1, "First durable lease must be generation 1"
        assert history[1].generation == 2, "Second durable lease must be generation 2"

    finally:
        db_path.unlink(missing_ok=True)


# ============================================================================
# SCHEMA MIGRATION
# ============================================================================

def _create_v1_database_with_data(db_path: Path):
    """Create a genuine v1 database with real mission data."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        # Create v1 schema
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (1, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (
                    current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')
                ),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state
                ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission
                ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission
                ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)

        # Insert representative data
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO missions (
                control_domain, mission_id, specification_fingerprint,
                objective, owner_identity, agent_identity,
                success_criteria, constraints, deadline, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "test-domain",
                "migration-test-mission",
                "fingerprint-v1",
                "Test migration objective",
                "test-owner",
                "test-agent",
                None,
                None,
                None,
                None,
                now,
            ),
        )

        conn.execute(
            """
            INSERT INTO mission_state (
                control_domain, mission_id, current_state, revision, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "test-domain",
                "migration-test-mission",
                "created",
                1,
                now,
            ),
        )

        conn.execute(
            """
            INSERT INTO mission_transitions (
                control_domain, transition_id, mission_id,
                from_state, to_state, revision, reason, transitioned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "test-domain",
                "transition-001",
                "migration-test-mission",
                "created",
                "created",
                1,
                "Mission created",
                now,
            ),
        )

        conn.execute(
            """
            INSERT INTO mission_checkpoints (
                control_domain, checkpoint_id, mission_id, sequence,
                mission_state, progress_data_json, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "test-domain",
                "checkpoint-001",
                "migration-test-mission",
                0,
                "created",
                None,
                "Initial checkpoint",
                now,
            ),
        )

        conn.commit()
    finally:
        conn.close()


def test_migration_v1_to_v2_preserves_data():
    """Test that v1 → v2 migration preserves all existing data."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create v1 database
        _create_v1_database_with_data(db_path)

        # Open with v2 store (triggers migration)
        store = MissionRuntimeStore(db_path)

        # Verify mission data preserved
        spec, state, revision, updated_at = store.get_mission("test-domain", "migration-test-mission")
        assert spec.objective == "Test migration objective"
        assert spec.owner_identity == "test-owner"
        assert state == MissionLifecycle.CREATED
        assert revision == 1

        # Verify transitions preserved
        transitions = store.list_transitions("test-domain", "migration-test-mission")
        assert len(transitions) == 1
        assert transitions[0].from_state == MissionLifecycle.CREATED

        # Verify checkpoints preserved
        checkpoints = store.list_checkpoints("test-domain", "migration-test-mission")
        assert len(checkpoints) == 1
        assert checkpoints[0].sequence == 0

        # Verify lease table exists and is empty
        leases = store.list_controller_lease_history("test-domain", "migration-test-mission")
        assert len(leases) == 0

        # Verify can acquire lease on migrated database
        lease = store.acquire_controller_lease("test-domain", "migration-test-mission", "controller-a")
        assert lease.generation == 1

    finally:
        db_path.unlink(missing_ok=True)


def _concurrent_migration_worker(db_path, result_queue):
    """Worker for concurrent migration test."""
    try:
        store = MissionRuntimeStore(db_path)
        # Read mission to verify migration succeeded
        spec, state, revision, _ = store.get_mission("test-domain", "migration-test-mission")
        result_queue.put(("success", spec.objective))
    except Exception as e:
        result_queue.put(("error", str(e)))


def test_migration_concurrent_converges():
    """Test that concurrent v1 → v2 migration converges correctly."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create v1 database
        _create_v1_database_with_data(db_path)

        # Launch concurrent migration attempts
        result_queue = multiprocessing.Queue()
        processes = []

        for i in range(3):
            p = multiprocessing.Process(
                target=_concurrent_migration_worker,
                args=(db_path, result_queue)
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join()

        # Collect results
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        # All processes should succeed
        assert len(results) == 3
        assert all(r[0] == "success" for r in results), f"Some migrations failed: {results}"

        # Verify final database state
        store = MissionRuntimeStore(db_path)

        # Check version
        conn = store._connect()
        try:
            cursor = conn.execute("SELECT version FROM mission_runtime_schema")
            versions = [row[0] for row in cursor.fetchall()]
            assert versions == [MISSION_SCHEMA_VERSION]
        finally:
            if store._memory_connection is None:
                conn.close()

        # Verify lease table exists
        leases = store.list_controller_lease_history("test-domain", "migration-test-mission")
        assert isinstance(leases, tuple)

        # Verify data integrity
        spec, _, _, _ = store.get_mission("test-domain", "migration-test-mission")
        assert spec.objective == "Test migration objective"

    finally:
        db_path.unlink(missing_ok=True)


def test_schema_validation_rejects_future_version():
    """Test that future schema version is rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create database with future version
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT INTO mission_runtime_schema (version, applied_at) VALUES (999, '2026-08-18')")
        conn.commit()
        conn.close()

        # Attempt to open should fail
        with pytest.raises(MissionSchemaVersionError, match="newer than supported"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_schema_validation_rejects_malformed_v1():
    """Test that malformed v1 schema is rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create incomplete v1 schema
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT INTO mission_runtime_schema (version, applied_at) VALUES (1, '2026-08-18T12:00:00')")
        conn.execute("CREATE TABLE missions (mission_id TEXT)")  # Wrong columns
        conn.commit()
        conn.close()

        # Attempt to open should fail during migration
        with pytest.raises(MissionSchemaVersionError):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


# ============================================================================
# BLOCKER REGRESSION TESTS
# ============================================================================

def test_fresh_schema_rollback_on_post_ddl_failure():
    """Test that fresh schema creation rolls back if validation fails after DDL.

    This regression test proves that _create_schema_v2 no longer uses executescript(),
    which would implicitly commit and prevent rollback.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create a fresh database and force validation failure by corrupting it mid-init
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")

        # Manually start transaction and create schema
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Create schema tables
            store = MissionRuntimeStore.__new__(MissionRuntimeStore)
            store._clock = lambda: datetime.now(timezone.utc)
            store._create_schema_v2(conn)

            # Force a validation failure by corrupting the schema before commit
            conn.execute("DROP TABLE mission_controller_leases")

            # Attempt validation - should fail
            store._validate_schema_contract(conn)

            # Should not reach here
            assert False, "Validation should have failed"
        except MissionSchemaVersionError:
            # Expected failure - rollback
            conn.rollback()

        # Verify database is clean (rollback succeeded)
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        assert len(tables) == 0, f"Rollback failed - tables remain: {tables}"

        # Verify not in transaction
        assert not conn.in_transaction, "Connection still in transaction after rollback"

        conn.close()

        # Verify normal initialization still works
        store = MissionRuntimeStore(db_path)
        spec = MissionSpecification(
            mission_id="test-mission",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

    finally:
        db_path.unlink(missing_ok=True)


def test_malformed_v2_missing_primary_key():
    """Test that v2 schema without PRIMARY KEY on leases fails validation."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create v2 schema with lease table missing PRIMARY KEY
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")

        # Create base v2 tables (simplified)
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (
                    current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')
                ),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- MALFORMED: lease table WITHOUT PRIMARY KEY
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        # Attempt to open should fail validation
        with pytest.raises(MissionSchemaVersionError, match="PRIMARY KEY"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_malformed_v2_missing_foreign_key():
    """Test that v2 schema without FOREIGN KEY on leases fails validation."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")

        # Create base tables
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (
                    current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')
                ),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- MALFORMED: lease table WITHOUT FOREIGN KEY
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="FOREIGN KEY"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_malformed_v2_missing_check_constraint():
    """Test that v2 schema without CHECK constraint on generation fails validation."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")

        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (
                    current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')
                ),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- MALFORMED: lease table WITHOUT generation CHECK
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="CHECK"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_naive_clock_on_first_acquire():
    """Test that naive clock on first acquire raises before commit."""
    fake_clock = FakeClock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create store with valid clock
        store = MissionRuntimeStore(db_path, clock=fake_clock)
        spec = MissionSpecification(
            mission_id="mission-naive-clock",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

        # Replace clock with naive datetime generator
        def naive_clock():
            return datetime(2026, 8, 18, 12, 0, 0)  # No timezone

        store._clock = naive_clock

        # Attempt acquire with naive clock - should raise before commit
        with pytest.raises(MissionControllerClockError):
            store.acquire_controller_lease("test-domain", "mission-naive-clock", "controller-a")

        # Verify NO lease row was persisted
        conn = store._connect()
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM mission_controller_leases WHERE mission_id = ?",
                ("mission-naive-clock",)
            )
            count = cursor.fetchone()[0]
            assert count == 0, f"Expected 0 lease rows, found {count}"

            # Verify not in transaction
            assert not conn.in_transaction, "Connection in transaction after naive clock failure"
        finally:
            conn.close()

        # Verify subsequent valid operation succeeds
        store._clock = fake_clock
        lease = store.acquire_controller_lease("test-domain", "mission-naive-clock", "controller-a")
        assert lease.generation == 1

    finally:
        db_path.unlink(missing_ok=True)


def test_naive_clock_on_renew():
    """Test that naive clock on renew raises before commit and leaves old lease unchanged."""
    fake_clock = FakeClock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        store = MissionRuntimeStore(db_path, clock=fake_clock)
        spec = MissionSpecification(
            mission_id="mission-renew-naive",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

        # Acquire valid lease
        lease1 = store.acquire_controller_lease("test-domain", "mission-renew-naive", "controller-a")
        original_renewed_at = lease1.renewed_at

        # Replace clock with naive generator
        store._clock = lambda: datetime(2026, 8, 18, 13, 0, 0)

        # Attempt renew - should raise before commit
        with pytest.raises(MissionControllerClockError):
            store.renew_controller_lease("test-domain", "mission-renew-naive", "controller-a", 1)

        # Verify lease row unchanged
        current = store.get_current_controller_lease("test-domain", "mission-renew-naive")
        assert current.renewed_at == original_renewed_at, "Lease was mutated despite naive clock failure"

        # Subsequent valid operation succeeds
        store._clock = fake_clock
        fake_clock.advance(timedelta(seconds=5))
        lease2 = store.renew_controller_lease("test-domain", "mission-renew-naive", "controller-a", 1)
        assert lease2.renewed_at > original_renewed_at

    finally:
        db_path.unlink(missing_ok=True)


def test_naive_clock_on_release():
    """Test that naive clock on release raises before commit and leaves released_at unchanged."""
    fake_clock = FakeClock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        store = MissionRuntimeStore(db_path, clock=fake_clock)
        spec = MissionSpecification(
            mission_id="mission-release-naive",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

        # Acquire valid lease
        store.acquire_controller_lease("test-domain", "mission-release-naive", "controller-a")

        # Replace clock with naive generator
        store._clock = lambda: datetime(2026, 8, 18, 14, 0, 0)

        # Attempt release - should raise before commit
        with pytest.raises(MissionControllerClockError):
            store.release_controller_lease("test-domain", "mission-release-naive", "controller-a", 1)

        # Verify lease NOT released
        current = store.get_current_controller_lease("test-domain", "mission-release-naive")
        assert current.released_at is None, "Lease was released despite naive clock failure"

        # Subsequent valid operation succeeds
        store._clock = fake_clock
        fake_clock.advance(timedelta(seconds=5))
        released = store.release_controller_lease("test-domain", "mission-release-naive", "controller-a", 1)
        assert released.released_at is not None

    finally:
        db_path.unlink(missing_ok=True)


def test_malformed_schema_version_text():
    """Test that non-integer schema version metadata yields typed error."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE mission_runtime_schema (version TEXT PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT INTO mission_runtime_schema (version, applied_at) VALUES ('malformed', '2026-08-18')")
        conn.commit()
        conn.close()

        # Attempt to open should raise typed MissionSchemaVersionError
        with pytest.raises(MissionSchemaVersionError, match="INTEGER storage class|non-integer"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def _concurrent_fresh_init_worker(db_path, barrier, result_queue):
    """Worker for deterministic concurrent fresh initialization test."""
    try:
        # Synchronize before opening store
        barrier.wait(timeout=5)

        # All processes attempt fresh initialization simultaneously
        store = MissionRuntimeStore(db_path)

        result_queue.put(("success", "initialized"))
    except Exception as e:
        result_queue.put(("error", str(e), type(e).__name__))


def test_fresh_concurrent_initialization_converges():
    """Test that fresh database concurrent initialization converges without raw lock errors.

    Uses deterministic synchronization (Barrier) to force true simultaneity.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        num_processes = 3
        barrier = multiprocessing.Barrier(num_processes)
        result_queue = multiprocessing.Queue()
        processes = []

        for i in range(num_processes):
            p = multiprocessing.Process(
                target=_concurrent_fresh_init_worker,
                args=(db_path, barrier, result_queue)
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=10)
            assert p.exitcode == 0, f"Process failed with exit code {p.exitcode}"

        # Collect results
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        # All processes should succeed
        assert len(results) == num_processes, f"Expected {num_processes} results, got {len(results)}"
        for result in results:
            assert result[0] == "success", f"Process failed: {result}"

        # Verify final database state
        store = MissionRuntimeStore(db_path)
        conn = store._connect()
        try:
            # Exactly one version row
            cursor = conn.execute("SELECT version FROM mission_runtime_schema")
            versions = [row[0] for row in cursor.fetchall()]
            assert versions == [2], f"Expected [2], found {versions}"

            # All v2 tables exist once
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='mission_controller_leases'"
            )
            assert cursor.fetchone() is not None, "mission_controller_leases table missing"

        finally:
            conn.close()

    finally:
        db_path.unlink(missing_ok=True)


def test_v1_migration_preserves_effect_references():
    """Test that v1→v2 migration preserves mission_effect_references data."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        # Create v1 database with effect reference
        _create_v1_database_with_data(db_path)

        # Add effect reference to v1 database
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO mission_effect_references (
                control_domain, mission_id, effect_intent_id,
                effect_dispatch_id, gateway_claim_id, referenced_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "test-domain",
                "migration-test-mission",
                "intent-001",
                "dispatch-001",
                "claim-001",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        # Migrate to v2
        store = MissionRuntimeStore(db_path)

        # Verify effect reference preserved
        refs = store.list_effect_references("test-domain", "migration-test-mission")
        assert len(refs) == 1
        assert refs[0].effect_intent_id == "intent-001"
        assert refs[0].effect_dispatch_id == "dispatch-001"
        assert refs[0].gateway_claim_id == "claim-001"

    finally:
        db_path.unlink(missing_ok=True)


# ============================================================================
# ATTACK MATRIX TESTS - Phase A Second Correction
# ============================================================================

def test_attack_altered_generation_type():
    """ATTACK 1: Altered generation column type rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: generation as REAL instead of INTEGER
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation REAL NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected type INTEGER, found REAL"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_altered_controller_id_type():
    """ATTACK 2: Altered controller_id column type rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        # Create minimal v2 schema with altered controller_id type
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: controller_id as INTEGER instead of TEXT
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected type TEXT, found INTEGER"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_altered_v1_revision_type():
    """ATTACK 3: Altered representative v1 column type (revision) rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        # Create v1 schema with altered revision type
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (1, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            -- ATTACK: revision as TEXT instead of INTEGER
            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision TEXT NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected type INTEGER, found TEXT"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)

def test_attack_altered_acquired_at_type():
    """ATTACK 4: Altered acquired_at column type rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: acquired_at as INTEGER instead of TEXT
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at INTEGER NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected type TEXT, found INTEGER"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_weakened_generation_check():
    """ATTACK 5: Weakened generation CHECK constraint rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: generation CHECK allows 0 (should be >= 1)
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 0),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="CHECK"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_version_coercion_real_2_5():
    """ATTACK 6: Schema version REAL 2.5 coercing to 2 rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        # Create schema version table with REAL column that stores 2.5
        conn.execute("CREATE TABLE mission_runtime_schema (version REAL PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2.5, '2026-08-18T12:00:00')")
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="INTEGER storage class"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_version_coercion_real_2_0():
    """ATTACK 7: Schema version REAL 2.0 coercing to 2 rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE mission_runtime_schema (version REAL PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2.0, '2026-08-18T12:00:00')")
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="INTEGER storage class"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_version_multiple_rows():
    """ATTACK 8: Multiple schema version rows rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE mission_runtime_schema (version INTEGER, applied_at TEXT)")
        conn.execute("INSERT INTO mission_runtime_schema (version, applied_at) VALUES (1, '2026-08-18T12:00:00')")
        conn.execute("INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00')")
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_missing_not_null_on_generation():
    """ATTACK 9: Missing NOT NULL on generation rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: generation without NOT NULL
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="NOT NULL"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_clock_rollback_on_generation_n_plus_1():
    """ATTACK 10: Clock rollback during generation N+1 acquire rolls back."""
    fake_clock = FakeClock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        store = MissionRuntimeStore(db_path, clock=fake_clock)
        spec = MissionSpecification(
            mission_id="mission-rollback-g2",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

        # Create generation 1
        lease1 = store.acquire_controller_lease(
            "test-domain", "mission-rollback-g2", "controller-a",
            lease_duration=timedelta(seconds=5)
        )

        # Expire generation 1
        fake_clock.advance(timedelta(seconds=6))

        # Roll clock back so expires_at <= renewed_at
        fake_clock.rollback(timedelta(seconds=10))

        # Attempt acquire generation 2 - should raise before commit
        with pytest.raises(MissionControllerClockError, match="Clock rollback"):
            store.acquire_controller_lease("test-domain", "mission-rollback-g2", "controller-b")

        # Verify NO generation 2 row persisted
        history = store.list_controller_lease_history("test-domain", "mission-rollback-g2")
        assert len(history) == 1, f"Expected 1 lease, found {len(history)}"
        assert history[0].generation == 1, "Only generation 1 should exist"

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_malicious_tzinfo_utcoffset_runtime_error():
    """ATTACK 11: Malicious tzinfo raising RuntimeError normalized to MissionControllerClockError."""
    class MaliciousTzInfo(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("Malicious tzinfo attack")

    fake_clock = FakeClock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        store = MissionRuntimeStore(db_path, clock=fake_clock)
        spec = MissionSpecification(
            mission_id="mission-malicious-tz",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

        # Replace clock with malicious tzinfo
        def malicious_clock():
            return datetime(2026, 8, 18, 12, 0, 0, tzinfo=MaliciousTzInfo())

        store._clock = malicious_clock

        # Attempt acquire - should normalize to MissionControllerClockError
        with pytest.raises(MissionControllerClockError, match="RuntimeError"):
            store.acquire_controller_lease("test-domain", "mission-malicious-tz", "controller-a")

        # Verify no lease row persisted
        history = store.list_controller_lease_history("test-domain", "mission-malicious-tz")
        assert len(history) == 0, "No lease should be persisted after tzinfo failure"

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_malicious_tzinfo_type_error():
    """ATTACK 12: Malicious tzinfo raising TypeError normalized to MissionControllerClockError."""
    class MaliciousTzInfo(tzinfo):
        def utcoffset(self, dt):
            raise TypeError("Malicious type error")

    fake_clock = FakeClock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        store = MissionRuntimeStore(db_path, clock=fake_clock)
        spec = MissionSpecification(
            mission_id="mission-malicious-tz2",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

        def malicious_clock():
            return datetime(2026, 8, 18, 12, 0, 0, tzinfo=MaliciousTzInfo())

        store._clock = malicious_clock

        with pytest.raises(MissionControllerClockError, match="TypeError"):
            store.acquire_controller_lease("test-domain", "mission-malicious-tz2", "controller-a")

        history = store.list_controller_lease_history("test-domain", "mission-malicious-tz2")
        assert len(history) == 0

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_malicious_tzinfo_custom_exception():
    """ATTACK 13: Malicious tzinfo raising custom exception normalized."""
    class CustomException(Exception):
        pass

    class MaliciousTzInfo(tzinfo):
        def utcoffset(self, dt):
            raise CustomException("Custom attack")

    fake_clock = FakeClock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        store = MissionRuntimeStore(db_path, clock=fake_clock)
        spec = MissionSpecification(
            mission_id="mission-custom-exc",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

        def malicious_clock():
            return datetime(2026, 8, 18, 12, 0, 0, tzinfo=MaliciousTzInfo())

        store._clock = malicious_clock

        with pytest.raises(MissionControllerClockError, match="unexpected exception"):
            store.acquire_controller_lease("test-domain", "mission-custom-exc", "controller-a")

        history = store.list_controller_lease_history("test-domain", "mission-custom-exc")
        assert len(history) == 0

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_invalid_lease_construction_on_renew_rollback():
    """ATTACK 14: Invalid lease construction on renew rolls back UPDATE."""
    fake_clock = FakeClock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        store = MissionRuntimeStore(db_path, clock=fake_clock)
        spec = MissionSpecification(
            mission_id="mission-renew-rollback",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

        # Acquire valid lease
        lease1 = store.acquire_controller_lease("test-domain", "mission-renew-rollback", "controller-a")
        original_renewed_at = lease1.renewed_at
        original_expires_at = lease1.expires_at

        # Replace clock with one that returns naive datetime
        fake_clock.advance(timedelta(seconds=5))

        def naive_clock():
            return datetime(2026, 8, 18, 13, 0, 0)  # No timezone

        store._clock = naive_clock

        # Attempt renew - should fail during lease construction before commit
        with pytest.raises(MissionControllerClockError):
            store.renew_controller_lease("test-domain", "mission-renew-rollback", "controller-a", 1)

        # Verify lease row UNCHANGED in database
        verify_store = MissionRuntimeStore(db_path)
        current = verify_store.get_current_controller_lease("test-domain", "mission-renew-rollback")
        assert current.renewed_at == original_renewed_at, "renewed_at should be unchanged"
        assert current.expires_at == original_expires_at, "expires_at should be unchanged"

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_invalid_lease_construction_on_release_rollback():
    """ATTACK 15: Invalid lease construction on release rolls back UPDATE."""
    fake_clock = FakeClock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        store = MissionRuntimeStore(db_path, clock=fake_clock)
        spec = MissionSpecification(
            mission_id="mission-release-rollback",
            control_domain="test-domain",
            objective="Test",
            owner_identity="owner",
            metadata=None,
        )
        store.create_mission(spec)

        # Acquire valid lease
        store.acquire_controller_lease("test-domain", "mission-release-rollback", "controller-a")

        # Replace clock with naive datetime generator
        fake_clock.advance(timedelta(seconds=5))

        def naive_clock():
            return datetime(2026, 8, 18, 14, 0, 0)  # No timezone

        store._clock = naive_clock

        # Attempt release - should fail before commit
        with pytest.raises(MissionControllerClockError):
            store.release_controller_lease("test-domain", "mission-release-rollback", "controller-a", 1)

        # Verify lease NOT released in database
        verify_store = MissionRuntimeStore(db_path)
        current = verify_store.get_current_controller_lease("test-domain", "mission-release-rollback")
        assert current.released_at is None, "Lease should still be active"

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_altered_control_domain_type():
    """ATTACK 16: Altered control_domain column type in leases rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: control_domain as INTEGER instead of TEXT
            CREATE TABLE mission_controller_leases (
                control_domain INTEGER NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected type TEXT, found INTEGER"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_altered_mission_id_type():
    """ATTACK 17: Altered mission_id column type in leases rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: mission_id as REAL instead of TEXT
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id REAL NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected type TEXT, found REAL"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_whitespace_in_check_constraint():
    """ATTACK 18: CHECK constraint with different whitespace (semantically same) accepted."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- CHECK with extra whitespace/newlines (semantically identical)
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (
                    generation    >=    1
                ),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        # Should succeed - whitespace differences are normalized
        store = MissionRuntimeStore(db_path)
        assert store is not None

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_missing_controller_id_not_null():
    """ATTACK 19: Missing NOT NULL on controller_id rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: controller_id without NOT NULL
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="NOT NULL"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_extra_column_in_leases():
    """ATTACK 20: Extra column in mission_controller_leases rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: extra column 'evil_backdoor'
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                evil_backdoor TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="has 9 columns|column count"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_missing_column_in_leases():
    """ATTACK 21: Missing released_at column in mission_controller_leases rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: missing released_at column
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected 8 columns|has 7 columns"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_reordered_columns():
    """ATTACK 22: Reordered columns in mission_controller_leases rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: columns reordered (generation before mission_id)
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                mission_id TEXT NOT NULL,
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected name"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_version_as_text_string():
    """ATTACK 23: Schema version as TEXT '2' rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE mission_runtime_schema (version TEXT PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT INTO mission_runtime_schema (version, applied_at) VALUES ('2', '2026-08-18T12:00:00')")
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="non-integer|INTEGER"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_version_null():
    """ATTACK 24: Schema version NULL rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE mission_runtime_schema (version INTEGER, applied_at TEXT)")
        conn.execute("INSERT INTO mission_runtime_schema (version, applied_at) VALUES (NULL, '2026-08-18T12:00:00')")
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_altered_renewed_at_type():
    """ATTACK 25: Altered renewed_at column type rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: renewed_at as BLOB instead of TEXT
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at BLOB NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected type TEXT, found BLOB"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_altered_expires_at_type():
    """ATTACK 26: Altered expires_at column type rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: expires_at as REAL instead of TEXT
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected type TEXT, found REAL"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_altered_released_at_type():
    """ATTACK 27: Altered released_at column type rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: released_at as INTEGER instead of TEXT
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at INTEGER,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="expected type TEXT, found INTEGER"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_missing_generation_check_constraint():
    """ATTACK 28: Missing CHECK constraint on generation rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: missing CHECK constraint on generation
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="CHECK"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_wrong_primary_key_columns():
    """ATTACK 29: Wrong PRIMARY KEY columns rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: PRIMARY KEY on (mission_id, generation) instead of (control_domain, mission_id, generation)
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (mission_id, generation),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="PRIMARY KEY"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)


def test_attack_wrong_foreign_key_columns():
    """ATTACK 30: Wrong FOREIGN KEY columns rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        db_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE mission_runtime_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO mission_runtime_schema (version, applied_at) VALUES (2, '2026-08-18T12:00:00');

            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            );

            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            );

            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id) REFERENCES missions(control_domain, mission_id)
            );

            -- ATTACK: FOREIGN KEY on (mission_id) instead of (control_domain, mission_id)
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (mission_id) REFERENCES missions(mission_id)
            );

            CREATE INDEX idx_missions_state ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
        """)
        conn.commit()
        conn.close()

        with pytest.raises(MissionSchemaVersionError, match="FOREIGN KEY"):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)



# PHASE A EXACT STRUCTURAL SEMANTICS REGRESSIONS


def test_attack_extra_check_constraint_rejected_exact_set():
    import sqlite3
    from federation.mission_runtime_store import (
        MissionRuntimeStore,
        MissionSchemaVersionError,
    )

    store = MissionRuntimeStore()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("""
            CREATE TABLE probe (
                generation INTEGER NOT NULL
                    CHECK (generation >= 1)
                    CHECK (generation <= 1)
            )
        """)

        with pytest.raises(MissionSchemaVersionError):
            store._validate_check_constraints(
                conn, "probe", ["generation >= 1"]
            )
    finally:
        conn.close()


def test_attack_extra_unique_constraint_rejected_exact_set():
    import sqlite3
    from federation.mission_runtime_store import (
        MissionRuntimeStore,
        MissionSchemaVersionError,
    )

    store = MissionRuntimeStore()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("""
            CREATE TABLE probe (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                PRIMARY KEY (control_domain, mission_id, generation),
                UNIQUE (control_domain, mission_id)
            )
        """)

        with pytest.raises(MissionSchemaVersionError):
            store._validate_unique_constraints(conn, "probe", [])
    finally:
        conn.close()


def test_attack_foreign_key_actions_rejected():
    import sqlite3
    from federation.mission_runtime_store import (
        MissionRuntimeStore,
        MissionSchemaVersionError,
    )

    store = MissionRuntimeStore()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE parent (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            )
        """)
        conn.execute("""
            CREATE TABLE child (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES parent(control_domain, mission_id)
                    ON DELETE CASCADE
                    ON UPDATE CASCADE
            )
        """)

        expected = [{
            "columns": ["control_domain", "mission_id"],
            "ref_table": "parent",
            "ref_columns": ["control_domain", "mission_id"],
        }]

        with pytest.raises(MissionSchemaVersionError):
            store._validate_foreign_keys(conn, "child", expected)
    finally:
        conn.close()


def test_attack_primary_key_nocase_collation_rejected():
    import sqlite3
    from federation.mission_runtime_store import (
        MissionRuntimeStore,
        MissionSchemaVersionError,
    )

    store = MissionRuntimeStore()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("""
            CREATE TABLE probe (
                control_domain TEXT NOT NULL COLLATE NOCASE,
                mission_id TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            )
        """)

        with pytest.raises(MissionSchemaVersionError):
            store._validate_primary_key(
                conn,
                "probe",
                ["control_domain", "mission_id"],
            )
    finally:
        conn.close()


def test_attack_unique_nocase_collation_rejected():
    import sqlite3
    from federation.mission_runtime_store import (
        MissionRuntimeStore,
        MissionSchemaVersionError,
    )

    store = MissionRuntimeStore()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("""
            CREATE TABLE probe (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                UNIQUE (
                    control_domain COLLATE NOCASE,
                    mission_id,
                    sequence
                )
            )
        """)

        with pytest.raises(MissionSchemaVersionError):
            store._validate_unique_constraints(
                conn,
                "probe",
                [["control_domain", "mission_id", "sequence"]],
            )
    finally:
        conn.close()


# PHASE A FOREIGN KEY DEFERRABILITY REGRESSIONS


def test_attack_deferrable_initially_deferred_foreign_key_rejected():
    import sqlite3
    from federation.mission_runtime_store import (
        MissionRuntimeStore,
        MissionSchemaVersionError,
    )

    store = MissionRuntimeStore()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE parent (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            )
        """)
        conn.execute("""
            CREATE TABLE child (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES parent(control_domain, mission_id)
                    ON DELETE NO ACTION
                    ON UPDATE NO ACTION
                    DEFERRABLE INITIALLY DEFERRED
            )
        """)

        expected = [{
            "columns": ["control_domain", "mission_id"],
            "ref_table": "parent",
            "ref_columns": ["control_domain", "mission_id"],
        }]

        with pytest.raises(
            MissionSchemaVersionError,
            match="DEFERRABLE|INITIALLY DEFERRED",
        ):
            store._validate_foreign_keys(conn, "child", expected)
    finally:
        conn.close()


def test_explicit_not_deferrable_initially_immediate_foreign_key_accepted():
    import sqlite3
    from federation.mission_runtime_store import MissionRuntimeStore

    store = MissionRuntimeStore()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE parent (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            )
        """)
        conn.execute("""
            CREATE TABLE child (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES parent(control_domain, mission_id)
                    ON DELETE NO ACTION
                    ON UPDATE NO ACTION
                    NOT DEFERRABLE INITIALLY IMMEDIATE
            )
        """)

        expected = [{
            "columns": ["control_domain", "mission_id"],
            "ref_table": "parent",
            "ref_columns": ["control_domain", "mission_id"],
        }]

        store._validate_foreign_keys(conn, "child", expected)
    finally:
        conn.close()
