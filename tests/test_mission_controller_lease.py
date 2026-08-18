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
from datetime import datetime, timedelta, timezone
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

def _concurrent_first_acquire_worker(db_path, domain, mission_id, controller_id, result_queue):
    """Worker for concurrent first-acquire test."""
    try:
        store = MissionRuntimeStore(db_path)
        lease = store.acquire_controller_lease(domain, mission_id, controller_id)
        result_queue.put(("success", controller_id, lease.generation))
    except MissionControllerLeaseConflictError as e:
        result_queue.put(("conflict", controller_id, str(e)))
    except Exception as e:
        result_queue.put(("error", controller_id, str(e)))


def test_concurrent_first_acquire_single_winner(mission):
    """Test that concurrent first acquire results in single generation 1."""
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

        # Launch concurrent acquire attempts
        result_queue = multiprocessing.Queue()
        processes = []

        for i in range(3):
            p = multiprocessing.Process(
                target=_concurrent_first_acquire_worker,
                args=(db_path, "test-domain", "mission-concurrent", f"controller-{i}", result_queue)
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join()

        # Collect results
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        # Exactly one success with generation 1
        successes = [r for r in results if r[0] == "success"]
        conflicts = [r for r in results if r[0] == "conflict"]

        assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}: {results}"
        assert successes[0][2] == 1  # generation 1
        assert len(conflicts) == 2  # Other two got conflicts

    finally:
        db_path.unlink(missing_ok=True)


def _concurrent_expired_takeover_worker(db_path, domain, mission_id, controller_id, result_queue):
    """Worker for concurrent expired-takeover test."""
    try:
        store = MissionRuntimeStore(db_path)
        lease = store.acquire_controller_lease(domain, mission_id, controller_id)
        result_queue.put(("success", controller_id, lease.generation))
    except MissionControllerLeaseConflictError as e:
        result_queue.put(("conflict", controller_id, str(e)))
    except Exception as e:
        result_queue.put(("error", controller_id, str(e)))


def test_concurrent_expired_takeover_single_generation_2(mission, fake_clock):
    """Test that concurrent takeover after expiry results in single generation 2."""
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

        # Launch concurrent takeover attempts
        result_queue = multiprocessing.Queue()
        processes = []

        for i in range(3):
            p = multiprocessing.Process(
                target=_concurrent_expired_takeover_worker,
                args=(db_path, "test-domain", "mission-takeover", f"controller-{i}", result_queue)
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join()

        # Collect results
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        # Exactly one success with generation 2
        successes = [r for r in results if r[0] == "success"]
        conflicts = [r for r in results if r[0] == "conflict"]

        assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}: {results}"
        assert successes[0][2] == 2  # generation 2
        assert len(conflicts) == 2  # Other two got conflicts

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

            INSERT INTO mission_runtime_schema (version) VALUES (1);

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
        conn.execute("INSERT INTO mission_runtime_schema (version) VALUES (1)")
        conn.execute("CREATE TABLE missions (mission_id TEXT)")  # Wrong columns
        conn.commit()
        conn.close()

        # Attempt to open should fail during migration
        with pytest.raises(MissionSchemaVersionError):
            MissionRuntimeStore(db_path)

    finally:
        db_path.unlink(missing_ok=True)
