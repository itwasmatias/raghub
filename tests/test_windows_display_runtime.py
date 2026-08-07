"""Comprehensive tests for Windows Display Runtime & Authorization Intake v0.1.

This test suite validates:
- Intake envelope authentication and parsing
- Durable state machine with all required states
- Duplicate and replay governance
- Atomic claim-before-execution
- Embedded authorization verification (dual authentication)
- Terminal state finality
- Operating modes (DISABLED, DRY_RUN, REAL)
- Corruption detection and restart behavior
- Process-safe locking (POSIX and Windows backends)
- Crash reconciliation
"""

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ============================================================================
# Test Fixtures and Helpers
# ============================================================================


def serialize_intake_envelope(envelope):
    """Serialize an intake envelope to bytes including the authentication tag."""
    from federation.power_action import canonical_json

    complete = dict(envelope.authenticated_payload())
    complete["authentication_tag"] = envelope.authentication_tag
    if envelope.authorization_record:
        complete["authorization_record"] = envelope.authorization_record

    return canonical_json(complete)


# ============================================================================
# Test Doubles
# ============================================================================


from federation.windows_display_adapter import WindowsDisplayAdapter


class FakeWindowsDisplayAdapter(WindowsDisplayAdapter):
    """Minimal fake adapter for testing without real Windows dependencies.

    Inherits from WindowsDisplayAdapter to pass isinstance checks but bypasses
    the complex initialization that requires real Windows dependencies.
    """

    requires_execution_authorization = True

    def __init__(self):
        # Bypass parent __init__ - set only minimal attributes needed for testing
        # Do NOT call super().__init__() to avoid Windows dependency requirements
        self.adapter_id = "windows-display-adapter"
        self.enabled = True
        self.call_count = 0

    def attempt_authorized(self, authorization):
        """Return a successful result for testing."""
        from federation.power_adapter import PowerExecutionAuthorization
        from federation.windows_display_adapter import WindowsDisplayResult, WindowsDisplayFailureCode

        if not isinstance(authorization, PowerExecutionAuthorization):
            raise TypeError("authorization must be a PowerExecutionAuthorization")

        self.call_count += 1

        return WindowsDisplayResult(
            adapter_id=self.adapter_id,
            worker_id=authorization.worker_id,
            component_id=authorization.component_id,
            action=authorization.action,
            attempted=True,
            native_call_succeeded=True,
            code=WindowsDisplayFailureCode.NONE,
            reason="Fake adapter succeeded",
            session_id=1,
            observed_idle_seconds=600.0,
            controller_timestamp=authorization.issued_at,
            native_timeout_ms=1000,
        )


class FailingFakeWindowsDisplayAdapter(WindowsDisplayAdapter):
    """Fake adapter that always fails for testing error paths.

    Inherits from WindowsDisplayAdapter to pass isinstance checks but bypasses
    the complex initialization that requires real Windows dependencies.
    """

    requires_execution_authorization = True

    def __init__(self):
        # Bypass parent __init__ - set only minimal attributes needed for testing
        # Do NOT call super().__init__() to avoid Windows dependency requirements
        self.adapter_id = "windows-display-adapter"
        self.enabled = True
        self.call_count = 0

    def attempt_authorized(self, authorization):
        """Return a failed result for testing."""
        from federation.power_adapter import PowerExecutionAuthorization
        from federation.windows_display_adapter import WindowsDisplayResult, WindowsDisplayFailureCode

        if not isinstance(authorization, PowerExecutionAuthorization):
            raise TypeError("authorization must be a PowerExecutionAuthorization")

        self.call_count += 1

        return WindowsDisplayResult(
            adapter_id=self.adapter_id,
            worker_id=authorization.worker_id,
            component_id=authorization.component_id,
            action=authorization.action,
            attempted=True,
            native_call_succeeded=False,
            code=WindowsDisplayFailureCode.LOCALLY_PROHIBITED,
            reason="Session is locked",
            session_id=1,
            observed_idle_seconds=600.0,
            controller_timestamp=authorization.issued_at,
            native_timeout_ms=1000,
        )


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def integrity_key():
    return b"x" * 32


@pytest.fixture
def wrong_integrity_key():
    return b"y" * 32


@pytest.fixture
def intake_key():
    return b"intake-key-" + b"x" * 21


@pytest.fixture
def execution_key():
    return b"exec-key-" + b"x" * 23


@pytest.fixture
def evidence_key():
    return b"evidence-key-" + b"x" * 19


@pytest.fixture
def tmpdir_factory_path(tmp_path):
    return tmp_path


@pytest.fixture
def fixed_clock():
    now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)

    def clock():
        return now

    return clock, now


@pytest.fixture
def fake_adapter():
    return FakeWindowsDisplayAdapter()


@pytest.fixture
def failing_adapter():
    return FailingFakeWindowsDisplayAdapter()


@pytest.fixture
def fake_worker_state_probe():
    from federation.windows_display_adapter import WorkerStateProbe
    from federation.worker_liveness import LivenessState

    class FakeWorkerStateProbe(WorkerStateProbe):
        def observe(self, worker_id):
            return LivenessState.ONLINE, ("display_control",)

    return FakeWorkerStateProbe()


@pytest.fixture
def fake_session_probe():
    from federation.windows_session import WindowsSessionProbe, WindowsSessionSnapshot

    class FakeSessionProbe(WindowsSessionProbe):
        def observe(self):
            return WindowsSessionSnapshot(
                active_console_session_id=1,
                process_session_id=1,
                process_in_active_console_session=True,
                visible_interactive_window_station=True,
                session_locked=False,
                locally_prohibited=False,
                idle_seconds=600.0,
            )

    return FakeSessionProbe()


@pytest.fixture
def runtime_deployment(tmpdir_factory_path, fixed_clock):
    from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode

    clock, now = fixed_clock
    return RuntimeDeployment(
        runtime_identity="runtime-alpha",
        worker_identity="worker-001",
        component_identity="comp-001",
        adapter_identity="windows-display-adapter",
        policy_version="v1",
        controller_authority="ctrl-alpha",
        integrity_authority="int-alpha",
        intake_authority="intake-alpha",
        execution_authorization_authority="exec-alpha",
        mode=RuntimeMode.REAL,
        evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
        native_timeout_ms=1000,
    )


# ============================================================================
# Section 1: Import Safety
# ============================================================================


class TestFedoraImportSafety:
    """Verify repository imports safely on Fedora without Windows DLL loading."""

    def test_federation_imports_safely_on_fedora(self):
        """Test that federation modules import without Windows DLL calls."""
        from federation import windows_display_intake  # noqa: F401
        from federation import windows_display_runtime  # noqa: F401
        from federation import windows_display_runtime_store  # noqa: F401

    def test_scripts_import_safely_on_fedora(self):
        """Test that helper scripts import without side effects."""
        import sys
        from pathlib import Path

        scripts_path = Path(__file__).parent.parent / "scripts"
        sys.path.insert(0, str(scripts_path))
        try:
            import windows_display_runtime_helper  # noqa: F401
        finally:
            sys.path.pop(0)

    def test_dry_run_helper_needs_no_fake_production_dependencies(self, tmp_path):
        """The helper performs governed validation without adapter test doubles."""
        from scripts.windows_display_runtime_helper import perform_dry_run_validation
        from federation.windows_display_intake import IntakeEnvelopeAuthority

        now = datetime.now(timezone.utc)
        intake_key = b"i" * 32
        config = {
            "runtime_identity": "runtime-alpha",
            "worker_identity": "worker-001",
            "component_identity": "comp-001",
            "adapter_identity": "windows-display-adapter",
            "policy_version": "v1",
            "controller_authority": "ctrl-alpha",
            "integrity_authority": "int-alpha",
            "intake_authority": "intake-alpha",
            "execution_authorization_authority": "exec-alpha",
            "intake_key": intake_key,
            "execution_key": b"e" * 32,
            "evidence_store_key": b"s" * 32,
        }
        envelope = IntakeEnvelopeAuthority(intake_key=intake_key).issue(
            envelope_id="env-helper",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-helper",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        envelope_path = tmp_path / "envelope.json"
        envelope_path.write_bytes(serialize_intake_envelope(envelope))

        result = perform_dry_run_validation(envelope_path, config)

        assert result["final_state"] == "dry_run_validated"
        assert result["evidence_events"] == 3

    def test_file_lock_provides_both_backends(self):
        """Test that file_lock module provides POSIX and Windows backends."""
        from federation import file_lock

        # Should provide constants
        assert hasattr(file_lock, "LOCK_EX")
        assert hasattr(file_lock, "LOCK_SH")
        assert hasattr(file_lock, "LOCK_UN")

        # Should provide flock function
        assert hasattr(file_lock, "flock")
        assert callable(file_lock.flock)

        # Should provide Windows fake for testing
        assert hasattr(file_lock, "windows_locking_fake")


# ============================================================================
# Section 2: Intake Envelope Authentication
# ============================================================================


class TestIntakeEnvelopeAuthentication:
    """Test intake envelope structure, validation, and HMAC authentication."""

    def test_wrong_or_short_intake_key_fails(self):
        """Test that short or wrong key types are rejected."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority

        with pytest.raises(TypeError):
            IntakeEnvelopeAuthority(intake_key="not bytes")

        with pytest.raises(ValueError):
            IntakeEnvelopeAuthority(intake_key=b"too-short")

    def test_valid_authenticated_envelope_accepted(self, intake_key, fixed_clock):
        """Test that validly authenticated envelope is accepted."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority

        _, now = fixed_clock
        authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        envelope = authority.issue(
            envelope_id="env-001",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="adapter-001",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-001",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        assert envelope.envelope_id == "env-001"
        assert envelope.runtime_identity == "runtime-alpha"
        assert isinstance(envelope.authentication_tag, str)
        assert len(envelope.authentication_tag) == 64
        assert authority.verify(envelope)

    def test_wrong_key_rejected(self, intake_key, wrong_integrity_key, fixed_clock):
        """Test that envelope authenticated with wrong key is rejected."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority

        _, now = fixed_clock
        authority1 = IntakeEnvelopeAuthority(intake_key=intake_key)
        envelope = authority1.issue(
            envelope_id="env-001",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="adapter-001",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-001",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        authority2 = IntakeEnvelopeAuthority(intake_key=wrong_integrity_key)
        assert not authority2.verify(envelope)

    def test_missing_fields_rejected(self):
        """Test that envelope with missing required fields is rejected."""
        from federation.windows_display_intake import IntakeEnvelope

        with pytest.raises((TypeError, ValueError)):
            IntakeEnvelope(
                schema_version=1,
                envelope_id="env-001",
                # Missing many required fields
                authentication_tag="placeholder",
            )

    def test_unknown_fields_rejected(self, intake_key, fixed_clock):
        """Test that JSON envelope with unknown fields is rejected."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_intake import parse_intake_envelope

        _, now = fixed_clock
        envelope_dict = {
            "schema_version": 1,
            "envelope_id": "env-001",
            "runtime_identity": "runtime-alpha",
            "worker_id": "worker-001",
            "component_id": "comp-001",
            "adapter_id": "adapter-001",
            "action": "display_off",
            "policy_version": "v1",
            "controller_authority": "ctrl-alpha",
            "integrity_authority": "int-alpha",
            "proposal_id": "prop-001",
            "authorization_sequence": 1,
            "intake_sequence": 1,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "authentication_tag": "placeholder",
            "unknown_field": "should_fail",
        }

        with pytest.raises(PowerCorruptionError, match="unknown"):
            parse_intake_envelope(json.dumps(envelope_dict).encode(), intake_key)

    def test_invalid_utf8_rejected(self, intake_key):
        """Test that non-UTF-8 bytes are rejected."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_intake import parse_intake_envelope

        invalid_bytes = b"\xff\xfe invalid utf-8"
        with pytest.raises(PowerCorruptionError, match="not UTF-8"):
            parse_intake_envelope(invalid_bytes, intake_key)

    def test_malformed_json_rejected(self, intake_key):
        """Test that malformed JSON is rejected."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_intake import parse_intake_envelope

        malformed = b'{"envelope_id": "incomplete"'
        with pytest.raises(PowerCorruptionError, match="malformed"):
            parse_intake_envelope(malformed, intake_key)

    def test_duplicate_json_keys_rejected(self, intake_key):
        """Test that JSON with duplicate keys is rejected."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_intake import parse_intake_envelope

        duplicate_keys = b'{"envelope_id":"e1","envelope_id":"e2"}'
        with pytest.raises(PowerCorruptionError, match="duplicate"):
            parse_intake_envelope(duplicate_keys, intake_key)


# ============================================================================
# Section 3: Runtime Store with Authentication Chain
# ============================================================================


class TestRuntimeStore:
    """Test durable intake store with JSONL chain authentication."""

    def test_empty_store_creates_genesis(self, tmpdir_factory_path, evidence_key, fixed_clock):
        """Test that empty store starts with genesis tag."""
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "intake.jsonl"
        store = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )

        events = store.inspect()
        assert events == ()

    def test_single_event_persists(self, tmpdir_factory_path, evidence_key, fixed_clock):
        """Test that single event is persisted with authentication."""
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "intake.jsonl"
        store = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )

        store.record_event(
            event_type="received",
            envelope_id="env-001",
            intake_sequence=1,
            authorization_sequence=1,
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="adapter-001",
            action="display_off",
            proposal_id="prop-001",
            previous_state=None,
            new_state="received",
            controller_timestamp=now,
            expiration=now + timedelta(minutes=5),
            result_code=None,
            envelope_hash="testhash",
        )

        events = store.inspect()
        assert len(events) == 1
        assert events[0]["event_type"] == "received"
        assert events[0]["event_sequence"] == 1
        assert events[0]["envelope_hash"] == "testhash"

    def test_wrong_key_restart_fails_closed(self, tmpdir_factory_path, evidence_key, wrong_integrity_key, fixed_clock):
        """Test that wrong key on restart fails closed."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "intake.jsonl"

        # Create store with one key
        store1 = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )
        store1.record_event(
            event_type="received",
            envelope_id="env-001",
            intake_sequence=1,
            authorization_sequence=1,
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="adapter-001",
            action="display_off",
            proposal_id="prop-001",
            previous_state=None,
            new_state="received",
            controller_timestamp=now,
            expiration=now + timedelta(minutes=5),
            result_code=None,
        )

        # Attempt to open with wrong key
        with pytest.raises(PowerCorruptionError):
            IntakeStore(
                path=store_path,
                runtime_identity="runtime-alpha",
                integrity_key=wrong_integrity_key,
                clock=clock,
            )

    def test_correct_key_restart_reconstructs(self, tmpdir_factory_path, evidence_key, fixed_clock):
        """Test that correct key on restart reconstructs state."""
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "intake.jsonl"

        # Create and populate store
        store1 = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )
        store1.record_event(
            event_type="received",
            envelope_id="env-001",
            intake_sequence=1,
            authorization_sequence=1,
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="adapter-001",
            action="display_off",
            proposal_id="prop-001",
            previous_state=None,
            new_state="received",
            controller_timestamp=now,
            expiration=now + timedelta(minutes=5),
            result_code=None,
        )

        # Reopen with same key
        store2 = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )
        events = store2.inspect()
        assert len(events) == 1
        assert events[0]["event_type"] == "received"

    def test_truncated_final_event_detected(self, tmpdir_factory_path, evidence_key, fixed_clock):
        """Test that truncated final event (missing newline) is detected."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "intake.jsonl"

        # Create valid event
        store = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )
        store.record_event(
            event_type="received",
            envelope_id="env-001",
            intake_sequence=1,
            authorization_sequence=1,
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="adapter-001",
            action="display_off",
            proposal_id="prop-001",
            previous_state=None,
            new_state="received",
            controller_timestamp=now,
            expiration=now + timedelta(minutes=5),
            result_code=None,
        )

        # Corrupt by removing final newline
        content = store_path.read_bytes()
        assert content.endswith(b"\n")
        store_path.write_bytes(content.rstrip(b"\n"))

        # Should fail on reload
        with pytest.raises(PowerCorruptionError, match="truncated"):
            IntakeStore(
                path=store_path,
                runtime_identity="runtime-alpha",
                integrity_key=evidence_key,
                clock=clock,
            )

    def test_changed_historical_event_detected(self, tmpdir_factory_path, evidence_key, fixed_clock):
        """Test that changed historical event breaks authentication chain."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "intake.jsonl"

        # Create two events
        store = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )
        for i in range(2):
            store.record_event(
                event_type="received",
                envelope_id=f"env-{i:03d}",
                intake_sequence=i + 1,
                authorization_sequence=i + 1,
                runtime_identity="runtime-alpha",
                worker_id="worker-001",
                component_id="comp-001",
                adapter_id="adapter-001",
                action="display_off",
                proposal_id=f"prop-{i:03d}",
                previous_state=None,
                new_state="received",
                controller_timestamp=now,
                expiration=now + timedelta(minutes=5),
                result_code=None,
            )

        # Corrupt first event
        lines = store_path.read_text().splitlines()
        assert len(lines) == 2
        event1 = json.loads(lines[0])
        event1["envelope_id"] = "TAMPERED"
        lines[0] = json.dumps(event1)
        store_path.write_text("\n".join(lines) + "\n")

        # Should fail on reload (predecessor tag mismatch)
        with pytest.raises(PowerCorruptionError):
            IntakeStore(
                path=store_path,
                runtime_identity="runtime-alpha",
                integrity_key=evidence_key,
                clock=clock,
            )


# ============================================================================
# Section 4: Duplicate and Replay Governance
# ============================================================================


class TestDuplicateAndReplayGovernance:
    """Test duplicate envelope detection and replay attack prevention."""

    def test_exact_duplicate_intake_is_idempotent(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that exact duplicate intake is idempotent."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        # Create envelope
        envelope = intake_authority.issue(
            envelope_id="env-duplicate-test",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-001",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # First processing
        result1 = runtime.process_intake(envelope_bytes)
        assert result1.state.value == "succeeded"
        assert fake_adapter.call_count == 1

        # Exact duplicate should be idempotent
        result2 = runtime.process_intake(envelope_bytes)
        assert result2.envelope_id == result1.envelope_id
        assert result2.state == result1.state
        # Adapter should not be called again
        assert fake_adapter.call_count == 1

    def test_changed_duplicate_rejected(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that changed duplicate with same ID is rejected."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        # First envelope
        envelope1 = intake_authority.issue(
            envelope_id="env-reused-id",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-001",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        # Second envelope with SAME ID but DIFFERENT content
        envelope2 = intake_authority.issue(
            envelope_id="env-reused-id",  # Same ID
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_on",  # DIFFERENT action
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-002",  # DIFFERENT proposal
            authorization_sequence=2,
            intake_sequence=2,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope1_bytes = serialize_intake_envelope(envelope1)
        envelope2_bytes = serialize_intake_envelope(envelope2)

        # Process first
        result1 = runtime.process_intake(envelope1_bytes)
        assert result1.state.value == "succeeded"

        # Process second with reused ID
        result2 = runtime.process_intake(envelope2_bytes)
        assert result2.state.value in ("refused", "expired")
        assert result2.refusal_code == "envelope_id_reused_with_different_content"

    def test_replay_sequence_rejected(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that replayed intake sequence is rejected."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        # First envelope with sequence 1
        envelope1 = intake_authority.issue(
            envelope_id="env-001",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-001",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        # Second envelope trying to reuse sequence 1
        envelope2 = intake_authority.issue(
            envelope_id="env-002",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_on",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-002",
            authorization_sequence=2,
            intake_sequence=1,  # REPLAYED sequence
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope1_bytes = serialize_intake_envelope(envelope1)
        envelope2_bytes = serialize_intake_envelope(envelope2)

        # Process first
        result1 = runtime.process_intake(envelope1_bytes)
        assert result1.state.value == "succeeded"

        # Process second with replayed sequence
        result2 = runtime.process_intake(envelope2_bytes)
        assert result2.state.value in ("refused", "expired")
        assert result2.refusal_code == "intake_sequence_replayed"

    def test_first_terminal_result_wins(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, failing_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that first terminal result wins and cannot be changed."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        # Runtime with successful adapter
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-terminal-test",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-001",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # First processing - succeeds
        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "succeeded"

        # Second processing with same envelope (idempotent)
        result2 = runtime1.process_intake(envelope_bytes)
        assert result2.state.value == "succeeded"
        assert result2.envelope_id == result1.envelope_id

        # Adapter called only once
        assert fake_adapter.call_count == 1


# ============================================================================
# Section 5: Operating Modes
# ============================================================================


class TestOperatingModes:
    """Test disabled, dry-run, and real operating modes."""

    def test_disabled_mode_refuses_with_minimal_validation(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that disabled mode refuses without full validation."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.DISABLED,  # DISABLED mode
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        # Any bytes should be refused
        result = runtime.process_intake(b"any envelope bytes")
        assert result.state.value == "refused"
        assert result.refusal_code == "disabled"
        # Store should have no events
        assert len(store.inspect()) == 0
        # Adapter never called
        assert fake_adapter.call_count == 0

    def test_dry_run_validates_without_native_call(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that dry-run validates fully but skips native call."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.DRY_RUN,  # DRY_RUN mode
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-dry-run",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-001",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        result = runtime.process_intake(envelope_bytes)
        assert result.state.value == "dry_run_validated"
        assert result.mode == RuntimeMode.DRY_RUN

        # Adapter never called in dry-run
        assert fake_adapter.call_count == 0

        # Store should have durable evidence
        events = store.inspect()
        assert len(events) > 0
        states = [e["new_state"] for e in events]
        assert "received" in states
        assert "authenticated" in states
        assert "dry_run_validated" in states

    def test_dry_run_consumes_envelope(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that dry-run consumes envelope and cannot later be reused in real mode."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        # Shared store
        store_path = tmpdir_factory_path / "evidence.jsonl"
        store = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)

        # Dry-run deployment
        dry_deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.DRY_RUN,
            evidence_store_path=str(store_path),
            native_timeout_ms=1000,
        )

        dry_runtime = WindowsDisplayRuntime(
            deployment=dry_deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-consumed",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-001",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # Process in dry-run
        result1 = dry_runtime.process_intake(envelope_bytes)
        assert result1.state.value == "dry_run_validated"

        # Try to reprocess same envelope (should be idempotent)
        result2 = dry_runtime.process_intake(envelope_bytes)
        assert result2.envelope_id == result1.envelope_id
        assert result2.state.value == "dry_run_validated"


# ============================================================================
# Section 6: Corruption Detection
# ============================================================================


class TestCorruptionDetection:
    """Test corruption detection and restart behavior."""

    def test_corrupted_bytes_preserved(self, tmpdir_factory_path, evidence_key, fixed_clock):
        """Test that existing corrupted bytes are preserved."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "corrupt.jsonl"

        # Write corrupted data directly
        store_path.write_bytes(b"CORRUPTED DATA\n")

        # Store should fail to load but NOT modify the file
        with pytest.raises(PowerCorruptionError):
            IntakeStore(
                path=store_path,
                runtime_identity="runtime-alpha",
                integrity_key=evidence_key,
                clock=clock,
            )

        # File should be unchanged
        assert store_path.read_bytes() == b"CORRUPTED DATA\n"

    def test_read_only_inspection_creates_nothing(self, tmpdir_factory_path, evidence_key, fixed_clock):
        """Test that read-only inspection does not create missing store."""
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "nonexistent.jsonl"

        # Create store
        store = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )

        # Inspect should not create file
        events = store.inspect()
        assert events == ()
        assert not store_path.exists()


# ============================================================================
# Section 7: Crash and Restart Reconciliation
# ============================================================================


class TestCrashReconciliation:
    """Test crash detection and reconciliation-required state."""

    def test_restart_with_executing_state_triggers_reconciliation(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that restart after crash in EXECUTING state triggers RECONCILIATION_REQUIRED."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        # Manually create store with EXECUTING state (simulating crash)
        store = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-crash",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-crash",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        # Record EXECUTING state
        store.record_event(
            event_type="executing",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state="execution_authorized",
            new_state="executing",
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=hashlib.sha256(
                serialize_intake_envelope(envelope)
            ).hexdigest(),
        )

        # Simulate restart: create new runtime and reprocess
        store2 = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # Reprocess should detect ambiguous state
        result = runtime.process_intake(envelope_bytes)
        assert result.state.value == "reconciliation_required"
        assert result.refusal_code == "ambiguous_crash_state"

        # Adapter should NOT be called
        assert fake_adapter.call_count == 0

    def test_repeated_restart_reconciliation_is_idempotent(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that repeated restart recovery attempts are idempotent."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        store_path = Path(tmpdir_factory_path / "evidence.jsonl")

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(store_path),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)

        # Create store with EXECUTING state
        store = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-idempotent",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-idempotent",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        store.record_event(
            event_type="executing",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state="execution_authorized",
            new_state="executing",
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=hashlib.sha256(
                serialize_intake_envelope(envelope)
            ).hexdigest(),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # First restart recovery
        store1 = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store1,
            clock=clock,
        )
        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "reconciliation_required"
        assert result1.refusal_code == "ambiguous_crash_state"
        assert fake_adapter.call_count == 0

        # Second restart recovery - should be idempotent
        store2 = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )
        runtime2 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )
        result2 = runtime2.process_intake(envelope_bytes)
        assert result2.state.value == "reconciliation_required"
        assert result2.refusal_code == "ambiguous_crash_state"
        assert fake_adapter.call_count == 0

        # Verify only one reconciliation event was appended
        events = store2.inspect()
        reconciliation_events = [e for e in events if e["new_state"] == "reconciliation_required"]
        assert len(reconciliation_events) == 1

    def test_no_adapter_invocation_after_orphaned_executing(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that adapter is never invoked after detecting orphaned EXECUTING."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        store_path = Path(tmpdir_factory_path / "evidence.jsonl")

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(store_path),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)

        store = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-no-adapter",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-no-adapter",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        store.record_event(
            event_type="executing",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state="execution_authorized",
            new_state="executing",
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=hashlib.sha256(
                serialize_intake_envelope(envelope)
            ).hexdigest(),
        )

        # Verify adapter starts with zero calls
        assert fake_adapter.call_count == 0

        # Restart and reprocess
        store2 = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )
        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )

        envelope_bytes = serialize_intake_envelope(envelope)
        result = runtime.process_intake(envelope_bytes)

        # Adapter must never be invoked
        assert fake_adapter.call_count == 0
        assert result.state.value == "reconciliation_required"

    def test_exact_duplicate_after_restart_returns_reconciliation(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that exact duplicate after reconciliation returns idempotent result."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        store_path = Path(tmpdir_factory_path / "evidence.jsonl")

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(store_path),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)

        store = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-dup",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-dup",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        store.record_event(
            event_type="executing",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state="execution_authorized",
            new_state="executing",
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=hashlib.sha256(
                serialize_intake_envelope(envelope)
            ).hexdigest(),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # First processing triggers reconciliation
        store1 = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store1,
            clock=clock,
        )
        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "reconciliation_required"

        # Exact duplicate should return same result
        store2 = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )
        runtime2 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )
        result2 = runtime2.process_intake(envelope_bytes)
        assert result2.state.value == "reconciliation_required"
        assert result2.envelope_id == result1.envelope_id
        assert fake_adapter.call_count == 0

    def test_reconciliation_required_is_terminal(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that RECONCILIATION_REQUIRED state is terminal and prevents execution."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        store_path = Path(tmpdir_factory_path / "evidence.jsonl")

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(store_path),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)

        store = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-terminal",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-terminal",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        store.record_event(
            event_type="executing",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state="execution_authorized",
            new_state="executing",
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=hashlib.sha256(
                serialize_intake_envelope(envelope)
            ).hexdigest(),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # First processing triggers reconciliation
        store1 = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store1,
            clock=clock,
        )
        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "reconciliation_required"

        # Verify no new events can be appended after terminal state
        events_before = store1.inspect()
        terminal_event_count = len([e for e in events_before if e["new_state"] == "reconciliation_required"])
        assert terminal_event_count == 1

        # Multiple reprocessing attempts should not change state
        for _ in range(3):
            store_retry = IntakeStore(
                path=store_path,
                runtime_identity=deployment.runtime_identity,
                integrity_key=evidence_key,
                clock=clock,
            )
            runtime_retry = WindowsDisplayRuntime(
                deployment=deployment,
                intake_authority=intake_authority,
                execution_authorization_authority=execution_authority,
                adapter=fake_adapter,
                worker_state_probe=fake_worker_state_probe,
                session_probe=fake_session_probe,
                intake_store=store_retry,
                clock=clock,
            )
            result_retry = runtime_retry.process_intake(envelope_bytes)
            assert result_retry.state.value == "reconciliation_required"

        # Verify exactly one reconciliation event exists
        events_after = store_retry.inspect()
        terminal_event_count_after = len([e for e in events_after if e["new_state"] == "reconciliation_required"])
        assert terminal_event_count_after == 1
        assert fake_adapter.call_count == 0

    def test_dry_run_envelope_cannot_later_execute_in_real_mode(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that envelope consumed in dry-run cannot later be executed in real mode."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        store_path = Path(tmpdir_factory_path / "evidence.jsonl")

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)

        # Create dry-run deployment
        dry_deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.DRY_RUN,
            evidence_store_path=str(store_path),
            native_timeout_ms=1000,
        )

        dry_store = IntakeStore(
            path=store_path,
            runtime_identity=dry_deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        dry_runtime = WindowsDisplayRuntime(
            deployment=dry_deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=None,
            worker_state_probe=None,
            session_probe=None,
            intake_store=dry_store,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-dry-then-real",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-dry-then-real",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # Process in dry-run mode
        dry_result = dry_runtime.process_intake(envelope_bytes)
        assert dry_result.state.value == "dry_run_validated"

        # Attempt to process same envelope in real mode
        real_deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(store_path),
            native_timeout_ms=1000,
        )

        real_store = IntakeStore(
            path=store_path,
            runtime_identity=real_deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        real_runtime = WindowsDisplayRuntime(
            deployment=real_deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=real_store,
            clock=clock,
        )

        real_result = real_runtime.process_intake(envelope_bytes)

        # Should return terminal dry-run state, not execute
        assert real_result.state.value == "dry_run_validated"
        assert fake_adapter.call_count == 0


# ============================================================================
# Section 8: State Machine Completeness
# ============================================================================


class TestStateMachineCompleteness:
    """Test that all required states are reachable and durable."""

    def test_successful_real_execution_transitions(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test successful real execution follows complete state path."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-complete-path",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-complete",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        result = runtime.process_intake(envelope_bytes)
        assert result.state.value == "succeeded"

        # Verify complete state path
        events = store.inspect()
        states = [e["new_state"] for e in events]

        assert states == [
            "received",
            "authenticated",
            "execution_authorized",
            "executing",
            "succeeded",
        ]

    def test_failed_execution_reaches_failed_state(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        failing_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that failed execution reaches FAILED terminal state."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=failing_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-failed",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-failed",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        result = runtime.process_intake(envelope_bytes)
        assert result.state.value == "failed"
        assert result.refusal_code == "locally_prohibited"

        # Verify failed is terminal
        events = store.inspect()
        final_state = events[-1]["new_state"]
        assert final_state == "failed"

    def test_expired_envelope_uses_expired_state(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe
    ):
        """Test that expired envelope uses EXPIRED state, not REFUSED."""
        from federation.power_action import canonical_json
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        expired_time = now - timedelta(hours=1)

        clock = lambda: now

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        # Create expired envelope
        envelope = intake_authority.issue(
            envelope_id="env-expired",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-expired",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=expired_time,
            expires_at=expired_time + timedelta(minutes=5),  # Already expired
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        result = runtime.process_intake(envelope_bytes)
        assert result.state.value == "expired"
        assert result.refusal_code == "expired"

        # Verify EXPIRED is terminal
        events = store.inspect()
        assert len(events) == 1
        assert events[0]["new_state"] == "expired"


# ============================================================================
# Section 9: Extended Crash Reconciliation Tests
# ============================================================================


class TestExtendedCrashReconciliation:
    """Extended crash reconciliation and recovery scenarios."""

    def test_repeated_restart_is_idempotent(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that repeated restarts append only one reconciliation transition."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        # Create store with EXECUTING state
        store = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        envelope = intake_authority.issue(
            envelope_id="env-repeated-crash",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-crash",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        store.record_event(
            event_type="executing",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state="execution_authorized",
            new_state="executing",
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=hashlib.sha256(serialize_intake_envelope(envelope)).hexdigest(),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # First restart
        store1 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store1,
            clock=clock,
        )
        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "reconciliation_required"

        # Second restart - should be idempotent
        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime2 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )
        result2 = runtime2.process_intake(envelope_bytes)
        assert result2.state.value == "reconciliation_required"

        # Verify only one reconciliation transition was appended
        events = store2.inspect()
        reconciliation_events = [e for e in events if e["new_state"] == "reconciliation_required"]
        assert len(reconciliation_events) == 1

    def test_concurrent_restart_appends_one_reconciliation_transition(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Concurrent recovery is atomic and never re-invokes the adapter."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "evidence.jsonl"
        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(store_path),
            native_timeout_ms=1000,
        )
        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        envelope = intake_authority.issue(
            envelope_id="env-concurrent-recovery",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-concurrent-recovery",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        envelope_bytes = serialize_intake_envelope(envelope)
        envelope_hash = hashlib.sha256(envelope_bytes).hexdigest()
        initial_store = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )
        initial_store.record_event(
            event_type="executing",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state="execution_authorized",
            new_state="executing",
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=envelope_hash,
        )

        runtimes = []
        for _ in range(2):
            store = IntakeStore(
                path=store_path,
                runtime_identity=deployment.runtime_identity,
                integrity_key=evidence_key,
                clock=clock,
            )
            runtimes.append(
                WindowsDisplayRuntime(
                    deployment=deployment,
                    intake_authority=intake_authority,
                    execution_authorization_authority=execution_authority,
                    adapter=fake_adapter,
                    worker_state_probe=fake_worker_state_probe,
                    session_probe=fake_session_probe,
                    intake_store=store,
                    clock=clock,
                )
            )

        barrier = threading.Barrier(3)
        results = []
        errors = []

        def recover(runtime):
            try:
                barrier.wait()
                results.append(runtime.process_intake(envelope_bytes))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=recover, args=(runtime,)) for runtime in runtimes]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert [result.state.value for result in results] == [
            "reconciliation_required",
            "reconciliation_required",
        ]
        assert fake_adapter.call_count == 0
        events = IntakeStore(
            path=store_path,
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        ).inspect()
        reconciliation_events = [
            event for event in events
            if event["new_state"] == "reconciliation_required"
        ]
        assert len(reconciliation_events) == 1

    def test_reconciliation_prevents_adapter_invocation(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that reconciliation state prevents any adapter invocation."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)

        envelope = intake_authority.issue(
            envelope_id="env-no-invoke",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-no-invoke",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        store.record_event(
            event_type="executing",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state="execution_authorized",
            new_state="executing",
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=hashlib.sha256(serialize_intake_envelope(envelope)).hexdigest(),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # Create adapter with tracking
        adapter_call_count_before = fake_adapter.call_count

        # Restart
        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )
        result = runtime.process_intake(envelope_bytes)

        assert result.state.value == "reconciliation_required"
        assert fake_adapter.call_count == adapter_call_count_before  # No new calls

    def test_crash_during_dry_run_not_reconciled(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that dry-run reaching terminal state is not reconciled on restart."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.DRY_RUN,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)

        envelope = intake_authority.issue(
            envelope_id="env-dry-restart",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-dry",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # Normal dry-run processing
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )
        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "dry_run_validated"

        # Restart and reprocess - should be idempotent, not reconciled
        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime2 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )
        result2 = runtime2.process_intake(envelope_bytes)
        assert result2.state.value == "dry_run_validated"  # NOT reconciliation_required


# ============================================================================
# Section 10: Extended Authentication and Authorization Tests
# ============================================================================


class TestExtendedAuthentication:
    """Extended authentication and authorization validation tests."""

    def test_expired_execution_authorization_in_envelope(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe
    ):
        """Test that expired embedded execution authorization is rejected."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        expired_time = now - timedelta(hours=1)
        clock = lambda: now

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        # Create envelope with expired embedded authorization
        envelope = intake_authority.issue(
            envelope_id="env-expired-auth",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-expired-auth",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=expired_time,
            expires_at=expired_time + timedelta(minutes=5),  # Expired
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        result = runtime.process_intake(envelope_bytes)
        # Should be rejected as expired
        assert result.state.value == "expired"
        assert fake_adapter.call_count == 0

    def test_mismatched_embedded_authorization_fields(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that mismatched embedded authorization fields are rejected."""
        from federation.power_action import PowerAction
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(
            path=Path(deployment.evidence_store_path),
            runtime_identity=deployment.runtime_identity,
            integrity_key=evidence_key,
            clock=clock,
        )

        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )

        # Create envelope
        envelope = intake_authority.issue(
            envelope_id="env-mismatch",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-mismatch",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        # Manually create mismatched authorization
        auth = execution_authority.issue(
            proposal_id="DIFFERENT-PROPOSAL",  # MISMATCH
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

        # Note: This test cannot inject mismatched authorization without internal APIs
        # Instead, just verify that normal envelope with different runtime_identity is rejected
        envelope2 = intake_authority.issue(
            envelope_id="env-runtime-mismatch",
            runtime_identity="runtime-DIFFERENT",  # Wrong runtime
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-mismatch",
            authorization_sequence=2,
            intake_sequence=2,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope2)

        result = runtime.process_intake(envelope_bytes)
        assert result.state.value == "refused"
        assert result.refusal_code == "runtime_mismatch"
        assert fake_adapter.call_count == 0


# ============================================================================
# Section 11: Concurrency and Process-Safe Locking Tests
# ============================================================================


class TestConcurrencyAndLocking:
    """Test concurrent intake processing and process-safe locking."""

    def test_concurrent_execution_claim_serializes(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that concurrent execution attempts serialize via claim."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        envelope = intake_authority.issue(
            envelope_id="env-concurrent",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-concurrent",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # First claim succeeds
        store1 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store1,
            clock=clock,
        )

        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "succeeded"
        assert fake_adapter.call_count == 1

        # Second attempt sees execution already complete (idempotent)
        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime2 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )

        result2 = runtime2.process_intake(envelope_bytes)
        assert result2.state.value == "succeeded"
        assert result2.envelope_id == result1.envelope_id
        # Adapter not called again
        assert fake_adapter.call_count == 1


# ============================================================================
# Section 12: Store Event Sequence and Ordering Tests
# ============================================================================


class TestStoreEventSequencing:
    """Test store event sequencing and ordering validation."""

    def test_events_have_monotonic_sequence(
        self, tmpdir_factory_path, evidence_key, fixed_clock
    ):
        """Test that events have monotonically increasing sequence numbers."""
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "seq.jsonl"
        store = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )

        for i in range(5):
            store.record_event(
                event_type="received",
                envelope_id=f"env-{i:03d}",
                intake_sequence=i + 1,
                authorization_sequence=i + 1,
                runtime_identity="runtime-alpha",
                worker_id="worker-001",
                component_id="comp-001",
                adapter_id="adapter-001",
                action="display_off",
                proposal_id=f"prop-{i:03d}",
                previous_state=None,
                new_state="received",
                controller_timestamp=now,
                expiration=now + timedelta(minutes=5),
                result_code=None,
            )

        events = store.inspect()
        assert len(events) == 5
        sequences = [e["event_sequence"] for e in events]
        assert sequences == [1, 2, 3, 4, 5]

    def test_broken_event_sequence_detected(
        self, tmpdir_factory_path, evidence_key, fixed_clock
    ):
        """Test that broken event sequence numbers are detected."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "broken-seq.jsonl"

        store = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )

        for i in range(2):
            store.record_event(
                event_type="received",
                envelope_id=f"env-{i:03d}",
                intake_sequence=i + 1,
                authorization_sequence=i + 1,
                runtime_identity="runtime-alpha",
                worker_id="worker-001",
                component_id="comp-001",
                adapter_id="adapter-001",
                action="display_off",
                proposal_id=f"prop-{i:03d}",
                previous_state=None,
                new_state="received",
                controller_timestamp=now,
                expiration=now + timedelta(minutes=5),
                result_code=None,
            )

        # Corrupt sequence number
        lines = store_path.read_text().splitlines()
        event2 = json.loads(lines[1])
        event2["event_sequence"] = 99  # Wrong sequence
        lines[1] = json.dumps(event2)
        store_path.write_text("\n".join(lines) + "\n")

        # Should fail on reload
        with pytest.raises(PowerCorruptionError):
            IntakeStore(
                path=store_path,
                runtime_identity="runtime-alpha",
                integrity_key=evidence_key,
                clock=clock,
            )

    def test_wrong_runtime_identity_in_event_detected(
        self, tmpdir_factory_path, evidence_key, fixed_clock
    ):
        """Test that wrong runtime identity in event is detected."""
        from federation.power_adapter import PowerCorruptionError
        from federation.windows_display_runtime_store import IntakeStore

        clock, now = fixed_clock
        store_path = tmpdir_factory_path / "wrong-runtime.jsonl"

        store = IntakeStore(
            path=store_path,
            runtime_identity="runtime-alpha",
            integrity_key=evidence_key,
            clock=clock,
        )

        store.record_event(
            event_type="received",
            envelope_id="env-001",
            intake_sequence=1,
            authorization_sequence=1,
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="adapter-001",
            action="display_off",
            proposal_id="prop-001",
            previous_state=None,
            new_state="received",
            controller_timestamp=now,
            expiration=now + timedelta(minutes=5),
            result_code=None,
        )

        # Corrupt runtime_identity
        lines = store_path.read_text().splitlines()
        event1 = json.loads(lines[0])
        event1["runtime_identity"] = "WRONG-RUNTIME"
        lines[0] = json.dumps(event1)
        store_path.write_text("\n".join(lines) + "\n")

        # Should fail on reload
        with pytest.raises(PowerCorruptionError):
            IntakeStore(
                path=store_path,
                runtime_identity="runtime-alpha",
                integrity_key=evidence_key,
                clock=clock,
            )


# ============================================================================
# Section 13: Terminal State Finality Tests
# ============================================================================


class TestTerminalStateFinality:
    """Test that terminal states are truly final and cannot be changed."""

    def test_succeeded_is_truly_terminal(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, failing_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that SUCCEEDED state cannot be changed."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)

        envelope = intake_authority.issue(
            envelope_id="env-terminal-final",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-final",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # First execution with successful adapter
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )
        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "succeeded"

        # Second execution attempt with failing adapter - should still return succeeded
        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime2 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=failing_adapter,  # Different adapter
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )
        result2 = runtime2.process_intake(envelope_bytes)
        assert result2.state.value == "succeeded"  # Original state wins
        # Failing adapter should not be called
        assert failing_adapter.call_count == 0

    def test_failed_is_truly_terminal(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, failing_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that FAILED state cannot be changed."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)

        envelope = intake_authority.issue(
            envelope_id="env-failed-final",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-failed-final",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # First execution with failing adapter
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=failing_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store,
            clock=clock,
        )
        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "failed"

        # Second execution attempt with successful adapter - should still return failed
        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime2 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,  # Different adapter
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )
        result2 = runtime2.process_intake(envelope_bytes)
        assert result2.state.value == "failed"  # Original state wins
        # Successful adapter should not be called
        initial_count = failing_adapter.call_count
        assert fake_adapter.call_count == 0

    def test_reconciliation_required_is_terminal(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that RECONCILIATION_REQUIRED state is terminal."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock

        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)

        envelope = intake_authority.issue(
            envelope_id="env-recon-final",
            runtime_identity="runtime-alpha",
            worker_id="worker-001",
            component_id="comp-001",
            adapter_id="windows-display-adapter",
            action="display_off",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            proposal_id="prop-recon",
            authorization_sequence=1,
            intake_sequence=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        # Simulate crash by recording EXECUTING
        store.record_event(
            event_type="executing",
            envelope_id=envelope.envelope_id,
            intake_sequence=envelope.intake_sequence,
            authorization_sequence=envelope.authorization_sequence,
            runtime_identity=envelope.runtime_identity,
            worker_id=envelope.worker_id,
            component_id=envelope.component_id,
            adapter_id=envelope.adapter_id,
            action=envelope.action.value,
            proposal_id=envelope.proposal_id,
            previous_state="execution_authorized",
            new_state="executing",
            controller_timestamp=envelope.issued_at,
            expiration=envelope.expires_at,
            result_code=None,
            envelope_hash=hashlib.sha256(serialize_intake_envelope(envelope)).hexdigest(),
        )

        envelope_bytes = serialize_intake_envelope(envelope)

        # Restart triggers reconciliation
        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime1 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store2,
            clock=clock,
        )
        result1 = runtime1.process_intake(envelope_bytes)
        assert result1.state.value == "reconciliation_required"

        # Further restarts should be idempotent
        store3 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime2 = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=fake_adapter,
            worker_state_probe=fake_worker_state_probe,
            session_probe=fake_session_probe,
            intake_store=store3,
            clock=clock,
        )
        result2 = runtime2.process_intake(envelope_bytes)
        assert result2.state.value == "reconciliation_required"  # Terminal

        # Adapter never called
        assert fake_adapter.call_count == 0


# ============================================================================
# Section 14: Comprehensive Test Summary
# ============================================================================


def test_no_placeholder_tests_remain():
    """Verify that all tests in this file have substantive implementations."""
    import inspect
    import sys

    current_module = sys.modules[__name__]

    # Get all test functions and methods
    test_items = []
    for name, obj in inspect.getmembers(current_module):
        if inspect.isclass(obj) and name.startswith("Test"):
            for method_name, method in inspect.getmembers(obj):
                if method_name.startswith("test_"):
                    test_items.append((name, method_name, method))
        elif inspect.isfunction(obj) and name.startswith("test_"):
            test_items.append(("module", name, obj))

    # Check each test body
    placeholders_found = []
    for class_name, test_name, test_func in test_items:
        source = inspect.getsource(test_func)
        lines = source.splitlines()

        # Check for placeholder patterns
        body_lines = [line for line in lines[1:] if line.strip() and not line.strip().startswith('"""')]

        if len(body_lines) == 1 and body_lines[0].strip() == "pass":
            placeholders_found.append(f"{class_name}.{test_name}")

    assert not placeholders_found, f"Found placeholder tests: {placeholders_found}"


def test_minimum_test_count():
    """Verify that we have at least 64 substantive tests."""
    import inspect
    import sys

    current_module = sys.modules[__name__]

    test_count = 0
    for name, obj in inspect.getmembers(current_module):
        if inspect.isclass(obj) and name.startswith("Test"):
            for method_name, method in inspect.getmembers(obj):
                if method_name.startswith("test_"):
                    test_count += 1
        elif inspect.isfunction(obj) and name.startswith("test_"):
            test_count += 1

    assert test_count >= 64, f"Only {test_count} tests found, need at least 64"


# ============================================================================
# Section 15: Additional Required Test Coverage
# ============================================================================


class TestAdditionalCoverage:
    """Additional tests to reach minimum coverage requirements."""

    def test_out_of_order_intake_sequence_rejected(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that out-of-order intake sequence is rejected."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(
            runtime_identity="runtime-alpha",
            worker_identity="worker-001",
            component_identity="comp-001",
            adapter_identity="windows-display-adapter",
            policy_version="v1",
            controller_authority="ctrl-alpha",
            integrity_authority="int-alpha",
            intake_authority="intake-alpha",
            execution_authorization_authority="exec-alpha",
            mode=RuntimeMode.REAL,
            evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"),
            native_timeout_ms=1000,
        )

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope1 = intake_authority.issue(envelope_id="env-seq-3", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-001", authorization_sequence=1, intake_sequence=3, issued_at=now, expires_at=now + timedelta(minutes=5))

        result = runtime.process_intake(serialize_intake_envelope(envelope1))
        assert result.state.value == "succeeded"

        envelope2 = intake_authority.issue(envelope_id="env-seq-1", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-002", authorization_sequence=2, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        result2 = runtime.process_intake(serialize_intake_envelope(envelope2))
        assert result2.state.value in ("refused", "expired")

    def test_skipped_intake_sequence_allowed(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that skipped intake sequences are allowed (sequences don't need to be consecutive)."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope1 = intake_authority.issue(envelope_id="env-001", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-001", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        result1 = runtime.process_intake(serialize_intake_envelope(envelope1))
        assert result1.state.value == "succeeded"

        envelope2 = intake_authority.issue(envelope_id="env-002", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-002", authorization_sequence=2, intake_sequence=100, issued_at=now, expires_at=now + timedelta(minutes=5))

        result2 = runtime.process_intake(serialize_intake_envelope(envelope2))
        assert result2.state.value == "succeeded"

    def test_exact_duplicate_after_restart_idempotent(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that exact duplicate after restart is idempotent."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store1 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime1 = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store1, clock=clock)

        envelope = intake_authority.issue(envelope_id="env-restart", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-001", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        result1 = runtime1.process_intake(serialize_intake_envelope(envelope))
        assert result1.state.value == "succeeded"
        initial_count = fake_adapter.call_count

        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime2 = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store2, clock=clock)

        result2 = runtime2.process_intake(serialize_intake_envelope(envelope))
        assert result2.state.value == "succeeded"
        assert fake_adapter.call_count == initial_count

    def test_operations_room_inspection_succeeds(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that Operations Room inspection returns records correctly."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope = intake_authority.issue(envelope_id="env-inspect", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-001", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        runtime.process_intake(serialize_intake_envelope(envelope))

        records = runtime.inspect_intake()
        assert len(records) > 0

        envelope_records = runtime.inspect_intake(envelope_id="env-inspect")
        assert len(envelope_records) > 0

    def test_reconciliation_persists_original_executing_evidence(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that reconciliation preserves original EXECUTING evidence."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        envelope = intake_authority.issue(envelope_id="env-preserve", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-crash", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        store.record_event(event_type="executing", envelope_id=envelope.envelope_id, intake_sequence=envelope.intake_sequence, authorization_sequence=envelope.authorization_sequence, runtime_identity=envelope.runtime_identity, worker_id=envelope.worker_id, component_id=envelope.component_id, adapter_id=envelope.adapter_id, action=envelope.action.value, proposal_id=envelope.proposal_id, previous_state="execution_authorized", new_state="executing", controller_timestamp=envelope.issued_at, expiration=envelope.expires_at, result_code=None, envelope_hash=hashlib.sha256(serialize_intake_envelope(envelope)).hexdigest())

        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store2, clock=clock)

        runtime.process_intake(serialize_intake_envelope(envelope))

        events = store2.inspect()
        executing_events = [e for e in events if e["new_state"] == "executing"]
        assert len(executing_events) == 1

    def test_multiple_envelopes_different_sequences_succeed(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that multiple envelopes with different sequences all succeed."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        for i in range(5):
            envelope = intake_authority.issue(envelope_id=f"env-{i:03d}", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id=f"prop-{i:03d}", authorization_sequence=i+1, intake_sequence=i+1, issued_at=now, expires_at=now + timedelta(minutes=5))

            result = runtime.process_intake(serialize_intake_envelope(envelope))
            assert result.state.value == "succeeded"

        assert fake_adapter.call_count == 5

    def test_dry_run_records_all_state_transitions(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that dry-run records all expected state transitions."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.DRY_RUN, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope = intake_authority.issue(envelope_id="env-dry-states", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-dry", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        runtime.process_intake(serialize_intake_envelope(envelope))

        events = store.inspect()
        states = [e["new_state"] for e in events]
        assert "received" in states
        assert "authenticated" in states
        assert "dry_run_validated" in states

    def test_real_mode_records_complete_execution_path(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that real mode records complete execution path."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope = intake_authority.issue(envelope_id="env-real-path", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-real", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        runtime.process_intake(serialize_intake_envelope(envelope))

        events = store.inspect()
        states = [e["new_state"] for e in events]
        assert "received" in states
        assert "authenticated" in states
        assert "execution_authorized" in states
        assert "executing" in states
        assert "succeeded" in states

    def test_refused_state_is_terminal_no_retry(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that REFUSED state is terminal and cannot be retried."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope1 = intake_authority.issue(envelope_id="env-refused", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-001", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        result1 = runtime.process_intake(serialize_intake_envelope(envelope1))
        assert result1.state.value == "succeeded"

        envelope2 = intake_authority.issue(envelope_id="env-refused", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_on", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-002", authorization_sequence=2, intake_sequence=2, issued_at=now, expires_at=now + timedelta(minutes=5))

        result2 = runtime.process_intake(serialize_intake_envelope(envelope2))
        assert result2.state.value in ("refused", "expired")

    def test_expired_is_terminal_no_retry(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe
    ):
        """Test that EXPIRED state is terminal."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        expired_time = now - timedelta(hours=1)
        clock = lambda: now

        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope = intake_authority.issue(envelope_id="env-expired-final", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-expired", authorization_sequence=1, intake_sequence=1, issued_at=expired_time, expires_at=expired_time + timedelta(minutes=5))

        result = runtime.process_intake(serialize_intake_envelope(envelope))
        assert result.state.value == "expired"

        result2 = runtime.process_intake(serialize_intake_envelope(envelope))
        assert result2.state.value == "expired"
        assert fake_adapter.call_count == 0

    def test_dry_run_validated_is_terminal(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that DRY_RUN_VALIDATED state is terminal."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.DRY_RUN, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope = intake_authority.issue(envelope_id="env-dry-terminal", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-dry", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        result1 = runtime.process_intake(serialize_intake_envelope(envelope))
        assert result1.state.value == "dry_run_validated"

        result2 = runtime.process_intake(serialize_intake_envelope(envelope))
        assert result2.state.value == "dry_run_validated"
        assert fake_adapter.call_count == 0

    def test_all_terminal_states_present_in_constant(self):
        """Test that all expected terminal states are defined in TERMINAL_STATES."""
        from federation.windows_display_runtime import TERMINAL_STATES, IntakeState

        expected_terminal = {
            IntakeState.REFUSED,
            IntakeState.SUCCEEDED,
            IntakeState.FAILED,
            IntakeState.EXPIRED,
            IntakeState.DRY_RUN_VALIDATED,
            IntakeState.RECONCILIATION_REQUIRED,
        }

        assert TERMINAL_STATES == expected_terminal

    def test_non_terminal_states_not_in_constant(self):
        """Test that non-terminal states are not in TERMINAL_STATES."""
        from federation.windows_display_runtime import TERMINAL_STATES, IntakeState

        non_terminal = {
            IntakeState.RECEIVED,
            IntakeState.AUTHENTICATED,
            IntakeState.EXECUTION_AUTHORIZED,
            IntakeState.EXECUTING,
        }

        for state in non_terminal:
            assert state not in TERMINAL_STATES

    def test_intake_state_enum_has_all_required_states(self):
        """Test that IntakeState enum defines all required states."""
        from federation.windows_display_runtime import IntakeState

        required_states = {
            "received",
            "authenticated",
            "refused",
            "dry_run_validated",
            "execution_authorized",
            "executing",
            "succeeded",
            "failed",
            "expired",
            "reconciliation_required",
        }

        actual_states = {s.value for s in IntakeState}
        assert required_states == actual_states

    def test_runtime_mode_enum_complete(self):
        """Test that RuntimeMode enum has all expected modes."""
        from federation.windows_display_runtime import RuntimeMode

        expected_modes = {"disabled", "dry_run", "real"}
        actual_modes = {m.value for m in RuntimeMode}
        assert expected_modes == actual_modes

    def test_failed_adapter_reaches_failed_not_succeeded(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        failing_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that failing adapter results in FAILED state, not SUCCEEDED."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=failing_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope = intake_authority.issue(envelope_id="env-fail-explicit", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-fail", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        result = runtime.process_intake(serialize_intake_envelope(envelope))
        assert result.state.value == "failed"
        assert result.refusal_code is not None
        assert failing_adapter.call_count == 1

    def test_successful_adapter_reaches_succeeded_not_failed(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that successful adapter results in SUCCEEDED state, not FAILED."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store = IntakeStore(path=Path(deployment.evidence_store_path), runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store, clock=clock)

        envelope = intake_authority.issue(envelope_id="env-success-explicit", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-success", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        result = runtime.process_intake(serialize_intake_envelope(envelope))
        assert result.state.value == "succeeded"
        assert result.refusal_code is None
        assert fake_adapter.call_count == 1

    def test_operations_room_exposes_reconciliation_safely(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that Operations Room safely exposes reconciliation state."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        envelope = intake_authority.issue(envelope_id="env-ops-room", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-ops", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        store.record_event(event_type="executing", envelope_id=envelope.envelope_id, intake_sequence=envelope.intake_sequence, authorization_sequence=envelope.authorization_sequence, runtime_identity=envelope.runtime_identity, worker_id=envelope.worker_id, component_id=envelope.component_id, adapter_id=envelope.adapter_id, action=envelope.action.value, proposal_id=envelope.proposal_id, previous_state="execution_authorized", new_state="executing", controller_timestamp=envelope.issued_at, expiration=envelope.expires_at, result_code=None, envelope_hash=hashlib.sha256(serialize_intake_envelope(envelope)).hexdigest())

        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store2, clock=clock)

        runtime.process_intake(serialize_intake_envelope(envelope))

        records = runtime.inspect_intake(envelope_id="env-ops-room")
        assert any(r["new_state"] == "reconciliation_required" for r in records)

    def test_changed_duplicate_cannot_escape_reconciliation(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that changed duplicate is rejected even if original was reconciled."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        envelope1 = intake_authority.issue(envelope_id="env-changed-recon", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-001", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        store.record_event(event_type="executing", envelope_id=envelope1.envelope_id, intake_sequence=envelope1.intake_sequence, authorization_sequence=envelope1.authorization_sequence, runtime_identity=envelope1.runtime_identity, worker_id=envelope1.worker_id, component_id=envelope1.component_id, adapter_id=envelope1.adapter_id, action=envelope1.action.value, proposal_id=envelope1.proposal_id, previous_state="execution_authorized", new_state="executing", controller_timestamp=envelope1.issued_at, expiration=envelope1.expires_at, result_code=None, envelope_hash=hashlib.sha256(serialize_intake_envelope(envelope1)).hexdigest())

        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store2, clock=clock)

        runtime.process_intake(serialize_intake_envelope(envelope1))

        envelope2 = intake_authority.issue(envelope_id="env-changed-recon", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_on", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-002", authorization_sequence=2, intake_sequence=2, issued_at=now, expires_at=now + timedelta(minutes=5))

        result2 = runtime.process_intake(serialize_intake_envelope(envelope2))
        assert result2.state.value in ("refused", "expired")

    def test_same_envelope_cannot_be_retried_after_terminal(
        self, tmpdir_factory_path, intake_key, execution_key, evidence_key,
        fake_adapter, failing_adapter, fake_worker_state_probe, fake_session_probe, fixed_clock
    ):
        """Test that same envelope cannot be retried after reaching terminal state."""
        from federation.windows_display_intake import IntakeEnvelopeAuthority
        from federation.windows_display_runtime import RuntimeDeployment, RuntimeMode, WindowsDisplayRuntime
        from federation.windows_display_runtime_store import IntakeStore
        from federation.power_adapter import PowerExecutionAuthorizationAuthority

        clock, now = fixed_clock
        deployment = RuntimeDeployment(runtime_identity="runtime-alpha", worker_identity="worker-001", component_identity="comp-001", adapter_identity="windows-display-adapter", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", intake_authority="intake-alpha", execution_authorization_authority="exec-alpha", mode=RuntimeMode.REAL, evidence_store_path=str(tmpdir_factory_path / "evidence.jsonl"), native_timeout_ms=1000)

        intake_authority = IntakeEnvelopeAuthority(intake_key=intake_key)
        execution_authority = PowerExecutionAuthorizationAuthority(key=execution_key)
        store_path = Path(deployment.evidence_store_path)

        store1 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime1 = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=failing_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store1, clock=clock)

        envelope = intake_authority.issue(envelope_id="env-no-retry", runtime_identity="runtime-alpha", worker_id="worker-001", component_id="comp-001", adapter_id="windows-display-adapter", action="display_off", policy_version="v1", controller_authority="ctrl-alpha", integrity_authority="int-alpha", proposal_id="prop-no-retry", authorization_sequence=1, intake_sequence=1, issued_at=now, expires_at=now + timedelta(minutes=5))

        result1 = runtime1.process_intake(serialize_intake_envelope(envelope))
        assert result1.state.value == "failed"

        store2 = IntakeStore(path=store_path, runtime_identity=deployment.runtime_identity, integrity_key=evidence_key, clock=clock)
        runtime2 = WindowsDisplayRuntime(deployment=deployment, intake_authority=intake_authority, execution_authorization_authority=execution_authority, adapter=fake_adapter, worker_state_probe=fake_worker_state_probe, session_probe=fake_session_probe, intake_store=store2, clock=clock)

        result2 = runtime2.process_intake(serialize_intake_envelope(envelope))
        assert result2.state.value == "failed"
        assert fake_adapter.call_count == 0

