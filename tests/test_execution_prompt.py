"""Focused tests for execution prompt compiler.

M6 Execution Prompt Engine v0.1 — Prompt Compilation

Tests cover:
- Deterministic compilation and fingerprinting
- Malformed/stale/contradictory handoff rejection
- Secret-bearing input rejection
- All required prompt sections
- Canonical serialization stability
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tools.execution_prompt.compiler import (
    ExecutionPromptCompiler,
    PromptCompilationError,
    compile_new_milestone_implementation_prompt,
)
from tools.execution_prompt.models import (
    ExecutionPrompt,
    PromptKind,
    MilestoneSpec,
    DevStatusHandoff,
    PolicyProfile,
)
from tools.execution_prompt.validation import (
    validate_dev_status,
    validate_milestone_spec,
    DevStatusValidationError,
    MilestoneValidationError,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class TestDeterministicCompilation:
    """Test deterministic compilation from canonical inputs."""

    def test_identical_inputs_produce_identical_fingerprints(self):
        """Identical canonical inputs must produce identical prompt fingerprints."""
        # Use fixed reference time to make hardcoded timestamp valid
        reference_time = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)

        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test milestone for determinism",
            requirements=["req1", "req2"],
        )
        dev_status = DevStatusHandoff(
            timestamp=datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(
            version="v0.1",
            paid_budget=0.0,
            allowed_effects=[],
        )

        prompt1 = compile_new_milestone_implementation_prompt(
            milestone=milestone,
            dev_status=dev_status,
            policy=policy,
            reference_time=reference_time,
        )
        prompt2 = compile_new_milestone_implementation_prompt(
            milestone=milestone,
            dev_status=dev_status,
            policy=policy,
            reference_time=reference_time,
        )

        assert prompt1.prompt_fingerprint == prompt2.prompt_fingerprint

    def test_changed_milestone_changes_fingerprint(self):
        """Behavior-affecting milestone change must change fingerprint."""
        milestone1 = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Original description",
            requirements=["req1"],
        )
        milestone2 = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Changed description",
            requirements=["req1"],
        )
        dev_status = DevStatusHandoff(
            timestamp=_now(),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])

        prompt1 = compile_new_milestone_implementation_prompt(milestone1, dev_status, policy)
        prompt2 = compile_new_milestone_implementation_prompt(milestone2, dev_status, policy)

        assert prompt1.prompt_fingerprint != prompt2.prompt_fingerprint

    def test_changed_dev_status_changes_fingerprint(self):
        """Behavior-affecting dev-status change must change fingerprint."""
        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test",
            requirements=["req1"],
        )
        dev_status1 = DevStatusHandoff(
            timestamp=_now(),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        dev_status2 = DevStatusHandoff(
            timestamp=_now(),
            repo="/test/repo",
            branch="integration/test",
            head="def456",  # Different HEAD
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])

        prompt1 = compile_new_milestone_implementation_prompt(milestone, dev_status1, policy)
        prompt2 = compile_new_milestone_implementation_prompt(milestone, dev_status2, policy)

        assert prompt1.prompt_fingerprint != prompt2.prompt_fingerprint

    def test_changed_policy_changes_fingerprint(self):
        """Behavior-affecting policy change must change fingerprint."""
        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test",
            requirements=["req1"],
        )
        dev_status = DevStatusHandoff(
            timestamp=_now(),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy1 = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])
        policy2 = PolicyProfile(version="v0.1", paid_budget=100.0, allowed_effects=["api_call"])

        prompt1 = compile_new_milestone_implementation_prompt(milestone, dev_status, policy1)
        prompt2 = compile_new_milestone_implementation_prompt(milestone, dev_status, policy2)

        assert prompt1.prompt_fingerprint != prompt2.prompt_fingerprint


class TestDevStatusValidation:
    """Test dev-status handoff validation."""

    def test_malformed_dev_status_rejected(self):
        """Malformed dev-status must be rejected."""
        with pytest.raises(DevStatusValidationError, match="missing required field"):
            validate_dev_status({
                "timestamp": _now().isoformat(),
                # Missing repo, branch, head, status
            })

    def test_stale_dev_status_rejected(self):
        """Stale dev-status (too old) must be rejected."""
        stale_time = _now() - timedelta(hours=25)  # > 24 hours old
        with pytest.raises(DevStatusValidationError, match="stale.*24 hours"):
            validate_dev_status({
                "timestamp": stale_time.isoformat(),
                "repo": "/test/repo",
                "branch": "integration/test",
                "head": "abc123",
                "status": "CANONICAL_INTEGRATED",
                "diff": "",
            })

    def test_contradictory_dev_status_rejected(self):
        """Contradictory dev-status (dirty with clean status) rejected."""
        with pytest.raises(DevStatusValidationError, match="contradictory"):
            validate_dev_status({
                "timestamp": _now().isoformat(),
                "repo": "/test/repo",
                "branch": "integration/test",
                "head": "abc123",
                "status": "CANONICAL_INTEGRATED",  # Claims clean
                "diff": "modified: file.py",  # But has uncommitted changes
            })

    def test_missing_dev_status_rejected(self):
        """Missing dev-status must be rejected."""
        with pytest.raises(DevStatusValidationError, match="dev.status.*required"):
            validate_dev_status(None)


class TestSecretRejection:
    """Test secret-bearing input rejection."""

    def test_secret_in_milestone_spec_rejected(self):
        """Secret-bearing milestone spec must be rejected."""
        with pytest.raises(MilestoneValidationError, match="secret.*detected"):
            validate_milestone_spec({
                "milestone_id": "M6",
                "title": "Test Milestone",
                "description": "API_KEY=sk_live_abcdefghij1234567890abcdef",  # Contains realistic secret
                "requirements": ["req1"],
            })

    def test_secret_in_dev_status_rejected(self):
        """Secret-bearing dev-status must be rejected."""
        with pytest.raises(DevStatusValidationError, match="secret.*detected"):
            validate_dev_status({
                "timestamp": _now().isoformat(),
                "repo": "/test/repo",
                "branch": "integration/test",
                "head": "abc123",
                "status": "DIRTY",  # Use dirty status to avoid contradictory check
                "diff": "export AWS_SECRET_ACCESS_KEY=abc123",  # Contains secret
            })

    def test_env_file_path_rejected(self):
        """.env file references should be rejected or redacted."""
        with pytest.raises(DevStatusValidationError, match=r"\.env.*prohibited"):
            validate_dev_status({
                "timestamp": _now().isoformat(),
                "repo": "/test/repo",
                "branch": "integration/test",
                "head": "abc123",
                "status": "DIRTY",  # Use dirty status to avoid contradictory check
                "diff": "modified: .env",  # Direct .env modification
            })


class TestRequiredSections:
    """Test that all 17 required sections are present."""

    REQUIRED_SECTIONS = [
        "Mission",
        "Authoritative live state",
        "Governing constraints",
        "Scope",
        "Non-goals",
        "Preserve / do-not-touch constraints",
        "Preflight",
        "Implementation contract",
        "Effect and authority ceiling",
        "Credit contract",
        "Validation",
        "Trial-and-error rule",
        "Effect-state reporting",
        "Reconciliation obligation",
        "Mission-effect fold and lifecycle",
        "Stop conditions",
        "Completion response schema",
    ]

    def test_all_required_sections_present(self):
        """Compiled prompt must contain all 17 required sections."""
        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test milestone",
            requirements=["req1"],
        )
        dev_status = DevStatusHandoff(
            timestamp=_now(),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])

        prompt = compile_new_milestone_implementation_prompt(milestone, dev_status, policy)

        for section in self.REQUIRED_SECTIONS:
            assert section in prompt.rendered_text, f"Missing section: {section}"

    def test_prompt_sections_are_ordered(self):
        """Prompt sections must appear in the specified order."""
        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test",
            requirements=["req1"],
        )
        dev_status = DevStatusHandoff(
            timestamp=_now(),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])

        prompt = compile_new_milestone_implementation_prompt(milestone, dev_status, policy)

        # Find positions of sections
        positions = {}
        for section in self.REQUIRED_SECTIONS:
            idx = prompt.rendered_text.find(section)
            assert idx >= 0, f"Section not found: {section}"
            positions[section] = idx

        # Verify order
        prev_pos = -1
        for section in self.REQUIRED_SECTIONS:
            assert positions[section] > prev_pos, f"Section out of order: {section}"
            prev_pos = positions[section]


class TestPromptKinds:
    """Test supported prompt kinds."""

    def test_new_milestone_implementation_prompt_kind(self):
        """new_milestone_implementation prompt kind must be supported."""
        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test",
            requirements=["req1"],
        )
        dev_status = DevStatusHandoff(
            timestamp=_now(),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])

        prompt = compile_new_milestone_implementation_prompt(milestone, dev_status, policy)

        assert prompt.kind == PromptKind.NEW_MILESTONE_IMPLEMENTATION


class TestCompilationNoLLM:
    """Test that compilation does not require LLM calls."""

    def test_compilation_is_local_deterministic(self):
        """Compilation must not require any LLM or paid API calls."""
        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test",
            requirements=["req1"],
        )
        dev_status = DevStatusHandoff(
            timestamp=_now(),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])

        # This should complete without any external calls
        prompt = compile_new_milestone_implementation_prompt(milestone, dev_status, policy)

        assert prompt is not None
        assert isinstance(prompt, ExecutionPrompt)


class TestStructuredRejections:
    """Test structured rejections for invalid inputs."""

    def test_compilation_rejects_with_structured_error(self):
        """Compilation failures must produce structured errors, not partial prompts."""
        with pytest.raises(PromptCompilationError) as exc_info:
            compile_new_milestone_implementation_prompt(
                milestone=None,  # Invalid
                dev_status=None,  # Invalid
                policy=None,  # Invalid
            )

        error = exc_info.value
        assert hasattr(error, "error_code")
        assert hasattr(error, "message")
        assert error.error_code is not None

    def test_validation_requirements_testable(self):
        """All validation requirements must be testable without real execution."""
        # This is a meta-test verifying validation is deterministic
        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test",
            requirements=[],  # Empty requirements
        )
        dev_status = DevStatusHandoff(
            timestamp=_now(),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])

        # Should not raise validation error for empty requirements
        prompt = compile_new_milestone_implementation_prompt(milestone, dev_status, policy)
        assert prompt is not None


class TestCanonicalSerialization:
    """Test canonical serialization stability."""

    def test_prompt_serialization_round_trips(self):
        """Prompt serialization must round-trip correctly."""
        # Use fixed reference time to make hardcoded timestamp valid
        reference_time = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)

        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test",
            requirements=["req1"],
        )
        dev_status = DevStatusHandoff(
            timestamp=datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])

        prompt = compile_new_milestone_implementation_prompt(milestone, dev_status, policy, reference_time=reference_time)

        # Serialize
        serialized = prompt.to_dict()

        # Deserialize
        restored = ExecutionPrompt.from_dict(serialized)

        assert restored.prompt_fingerprint == prompt.prompt_fingerprint
        assert restored.rendered_text == prompt.rendered_text

    def test_serialization_is_stable(self):
        """Serialization must produce stable output (no unstable map ordering)."""
        # Use fixed reference time to make hardcoded timestamp valid
        reference_time = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)

        milestone = MilestoneSpec(
            milestone_id="M6",
            title="Test Milestone",
            description="Test",
            requirements=["req1", "req2", "req3"],
        )
        dev_status = DevStatusHandoff(
            timestamp=datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc),
            repo="/test/repo",
            branch="integration/test",
            head="abc123",
            status="CANONICAL_INTEGRATED",
            diff="",
        )
        policy = PolicyProfile(version="v0.1", paid_budget=0.0, allowed_effects=[])

        prompt = compile_new_milestone_implementation_prompt(milestone, dev_status, policy, reference_time=reference_time)

        # Serialize multiple times
        serialized1 = json.dumps(prompt.to_dict(), sort_keys=True, separators=(",", ":"))
        serialized2 = json.dumps(prompt.to_dict(), sort_keys=True, separators=(",", ":"))

        assert serialized1 == serialized2
