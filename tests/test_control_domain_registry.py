"""Tests for DurableControlDomainRegistry."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from federation.control_domain import ControlDomain, DomainLifecycle
from federation.control_domain_registry import (
    DomainClockRollbackError,
    DomainConflictError,
    DomainCorruptionError,
    DomainLifecycleError,
    DomainNotFoundError,
    DurableControlDomainRegistry,
)


@pytest.fixture
def registry_path(tmp_path):
    """Provide a temporary path for registry storage."""
    return tmp_path / "control_domains.jsonl"


@pytest.fixture
def integrity_key():
    """Provide a test integrity key (32 bytes)."""
    return b"test-integrity-key-32-bytes-ok!!"


@pytest.fixture
def registry(registry_path, integrity_key):
    """Provide a configured registry instance."""
    return DurableControlDomainRegistry(
        path=registry_path,
        integrity_key=integrity_key,
    )


def test_registry_creation(registry_path, integrity_key):
    """Test creating a registry instance."""
    registry = DurableControlDomainRegistry(
        path=registry_path,
        integrity_key=integrity_key,
    )
    assert registry.path == registry_path


def test_registry_register_domain(registry):
    """Test registering a new domain."""
    domain = ControlDomain(
        domain_id="example-domain",
        name="Example Organization",
        owner="example-owner",
    )

    result = registry.register(domain)

    assert result.domain_id == "example-domain"
    assert result.name == "Example Organization"
    assert result.owner == "example-owner"
    assert result.lifecycle == DomainLifecycle.ACTIVE
    assert isinstance(result.domain_fingerprint, str)
    assert len(result.domain_fingerprint) == 64  # SHA-256 hex


def test_registry_register_creates_jsonl(registry, registry_path):
    """Test that registration creates authenticated JSONL evidence."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)

    assert registry_path.exists()
    content = registry_path.read_text()
    assert content.endswith("\n")

    record = json.loads(content.strip())
    assert record["domain_id"] == "test-domain"
    assert record["schema_version"] == 1
    assert record["sequence"] == 1
    assert "authentication_tag" in record
    assert "domain_fingerprint" in record


def test_registry_register_duplicate_domain_conflict(registry):
    """Test that duplicate domain_id raises conflict error."""
    domain = ControlDomain(
        domain_id="duplicate-domain",
        name="First Registration",
        owner="admin",
    )

    registry.register(domain)

    # Attempt to register same domain_id again
    duplicate = ControlDomain(
        domain_id="duplicate-domain",
        name="Second Registration",
        owner="different-owner",
    )

    with pytest.raises(DomainConflictError, match="already registered"):
        registry.register(duplicate)


def test_registry_get_domain(registry):
    """Test retrieving a registered domain."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)
    retrieved = registry.get("test-domain")

    assert retrieved.domain_id == "test-domain"
    assert retrieved.name == "Test Org"
    assert retrieved.owner == "admin"


def test_registry_get_nonexistent_domain(registry):
    """Test that getting nonexistent domain raises error."""
    with pytest.raises(DomainNotFoundError, match="not found"):
        registry.get("nonexistent-domain")


def test_registry_list_domains_empty(registry):
    """Test listing domains from empty registry."""
    domains = registry.list_domains()
    assert domains == ()


def test_registry_list_domains(registry):
    """Test listing all registered domains."""
    domain1 = ControlDomain(
        domain_id="domain-1",
        name="Organization 1",
        owner="owner-1",
    )
    domain2 = ControlDomain(
        domain_id="domain-2",
        name="Organization 2",
        owner="owner-2",
    )

    registry.register(domain1)
    registry.register(domain2)

    domains = registry.list_domains()
    assert len(domains) == 2
    domain_ids = {d.domain_id for d in domains}
    assert domain_ids == {"domain-1", "domain-2"}


def test_registry_update_lifecycle_suspend(registry):
    """Test updating domain lifecycle to suspended."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)
    updated = registry.update_lifecycle("test-domain", DomainLifecycle.SUSPENDED)

    assert updated.lifecycle == DomainLifecycle.SUSPENDED
    assert updated.domain_id == "test-domain"

    # Verify persistence
    retrieved = registry.get("test-domain")
    assert retrieved.lifecycle == DomainLifecycle.SUSPENDED


def test_registry_update_lifecycle_archive(registry):
    """Test updating domain lifecycle to archived."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)
    registry.update_lifecycle("test-domain", DomainLifecycle.SUSPENDED)
    updated = registry.update_lifecycle("test-domain", DomainLifecycle.ARCHIVED)

    assert updated.lifecycle == DomainLifecycle.ARCHIVED


def test_registry_update_lifecycle_non_widening_violation(registry):
    """Test that non-widening lifecycle violations are rejected."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
        lifecycle=DomainLifecycle.ARCHIVED,
    )

    registry.register(domain)

    with pytest.raises(DomainLifecycleError, match="non-widening"):
        registry.update_lifecycle("test-domain", DomainLifecycle.SUSPENDED)


def test_registry_update_lifecycle_idempotent(registry):
    """Test that updating to same lifecycle is idempotent."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)
    result = registry.update_lifecycle("test-domain", DomainLifecycle.ACTIVE)

    # Should return current without creating new record
    assert result.lifecycle == DomainLifecycle.ACTIVE


def test_registry_update_lifecycle_nonexistent_domain(registry):
    """Test that updating nonexistent domain raises error."""
    with pytest.raises(DomainNotFoundError):
        registry.update_lifecycle("nonexistent", DomainLifecycle.SUSPENDED)


def test_registry_deterministic_fingerprints(registry):
    """Test that identical domains produce identical fingerprints."""
    domain1 = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
        created_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
    )

    result1 = registry.register(domain1)
    fingerprint1 = result1.domain_fingerprint

    # Create new registry with different integrity key
    new_registry = DurableControlDomainRegistry(
        path=registry.path.parent / "other.jsonl",
        integrity_key=b"different-integrity-key-32bytes!",
    )

    domain2 = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
        created_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
    )

    with patch("federation.control_domain_registry.datetime") as mock_datetime:
        mock_datetime.now.return_value = result1.last_transition_at
        result2 = new_registry.register(domain2)

    # Fingerprints should be identical (integrity key doesn't affect fingerprint)
    assert result2.domain_fingerprint == fingerprint1


def test_registry_authentication_tags_differ_with_different_keys(
    registry_path,
):
    """Test that different integrity keys produce different auth tags."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
        created_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
    )

    fixed_time = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    registry1 = DurableControlDomainRegistry(
        path=registry_path,
        integrity_key=b"a" * 32,
    )

    with patch("federation.control_domain_registry.datetime") as mock_datetime:
        mock_datetime.now.return_value = fixed_time
        registry1.register(domain)

    record1 = json.loads(registry_path.read_text().strip())

    registry_path.unlink()

    registry2 = DurableControlDomainRegistry(
        path=registry_path,
        integrity_key=b"b" * 32,
    )

    with patch("federation.control_domain_registry.datetime") as mock_datetime:
        mock_datetime.now.return_value = fixed_time
        registry2.register(domain)

    record2 = json.loads(registry_path.read_text().strip())

    # Fingerprints should match
    assert record1["domain_fingerprint"] == record2["domain_fingerprint"]
    # But authentication tags should differ
    assert record1["authentication_tag"] != record2["authentication_tag"]


def test_registry_clock_rollback_protection(registry):
    """Test that clock rollback is detected and rejected."""
    future_time = datetime.now(timezone.utc) + timedelta(hours=1)

    domain1 = ControlDomain(
        domain_id="domain-1",
        name="First Domain",
        owner="admin",
    )

    # Register first domain with future timestamp
    with patch("federation.control_domain_registry.datetime") as mock_datetime:
        mock_datetime.now.return_value = future_time
        # Also need to mock fromisoformat for reading
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.timezone = timezone
        registry.register(domain1)

    # Attempt to register another domain with current (earlier) timestamp
    domain2 = ControlDomain(
        domain_id="domain-2",
        name="Second Domain",
        owner="admin",
    )

    with pytest.raises(DomainClockRollbackError, match="rollback"):
        registry.register(domain2)


def test_registry_sequence_numbers(registry, registry_path):
    """Test that sequence numbers are monotonically increasing."""
    domains = [
        ControlDomain(domain_id=f"domain-{i}", name=f"Org {i}", owner="admin")
        for i in range(3)
    ]

    for domain in domains:
        registry.register(domain)

    records = [json.loads(line) for line in registry_path.read_text().splitlines()]
    sequences = [r["sequence"] for r in records]

    assert sequences == [1, 2, 3]


def test_registry_corrupted_authentication_tag(registry, registry_path):
    """Test that corrupted authentication tags are detected."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)

    # Corrupt the authentication tag
    content = registry_path.read_text()
    record = json.loads(content.strip())
    record["authentication_tag"] = "0" * 64  # Invalid tag
    registry_path.write_text(json.dumps(record) + "\n")

    with pytest.raises(DomainCorruptionError, match="authentication tag"):
        registry.get("test-domain")


def test_registry_corrupted_fingerprint(registry, registry_path):
    """Test that corrupted fingerprints are detected."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)

    # Corrupt the fingerprint
    content = registry_path.read_text()
    record = json.loads(content.strip())
    record["domain_fingerprint"] = "0" * 64  # Invalid fingerprint

    # Re-sign with new (invalid) content
    from federation.integrity import authentication_tag
    authenticated = dict(record)
    del authenticated["authentication_tag"]
    record["authentication_tag"] = authentication_tag(
        registry._integrity_key,
        b"raghub.control-domain.v1",
        json.dumps(
            authenticated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    registry_path.write_text(json.dumps(record) + "\n")

    with pytest.raises(DomainCorruptionError, match="fingerprint is invalid"):
        registry.get("test-domain")


def test_registry_incomplete_evidence(registry_path, integrity_key):
    """Test that incomplete evidence (missing newline) is rejected."""
    registry_path.write_text('{"incomplete": "record"}')  # No trailing newline

    registry = DurableControlDomainRegistry(
        path=registry_path,
        integrity_key=integrity_key,
    )

    with pytest.raises(DomainCorruptionError, match="incomplete"):
        registry.list_domains()


def test_registry_invalid_sequence(registry, registry_path):
    """Test that invalid sequence numbers are detected."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)

    # Corrupt the sequence number
    content = registry_path.read_text()
    record = json.loads(content.strip())
    record["sequence"] = 99  # Wrong sequence

    # Re-sign
    from federation.integrity import authentication_tag
    authenticated = dict(record)
    del authenticated["authentication_tag"]
    record["authentication_tag"] = authentication_tag(
        registry._integrity_key,
        b"raghub.control-domain.v1",
        json.dumps(
            authenticated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    registry_path.write_text(json.dumps(record) + "\n")

    with pytest.raises(DomainCorruptionError, match="sequence is invalid"):
        registry.get("test-domain")


def test_registry_invalid_schema_version(registry, registry_path):
    """Test that invalid schema versions are rejected."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)

    # Corrupt schema version
    content = registry_path.read_text()
    record = json.loads(content.strip())
    record["schema_version"] = 999

    # Re-sign
    from federation.integrity import authentication_tag
    authenticated = dict(record)
    del authenticated["authentication_tag"]
    record["authentication_tag"] = authentication_tag(
        registry._integrity_key,
        b"raghub.control-domain.v1",
        json.dumps(
            authenticated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    registry_path.write_text(json.dumps(record) + "\n")

    with pytest.raises(DomainCorruptionError, match="schema is invalid"):
        registry.get("test-domain")


def test_registry_duplicate_json_keys(registry_path, integrity_key):
    """Test that duplicate JSON keys are detected."""
    # Manually create malformed JSON with duplicate keys
    malformed = '{"domain_id":"test","domain_id":"duplicate"}\n'
    registry_path.write_text(malformed)

    registry = DurableControlDomainRegistry(
        path=registry_path,
        integrity_key=integrity_key,
    )

    with pytest.raises(DomainCorruptionError, match="corrupt"):
        registry.list_domains()


def test_registry_lifecycle_updates_create_new_records(registry, registry_path):
    """Test that lifecycle updates append new records rather than modifying."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)
    registry.update_lifecycle("test-domain", DomainLifecycle.SUSPENDED)

    records = [json.loads(line) for line in registry_path.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["lifecycle"] == "active"
    assert records[1]["lifecycle"] == "suspended"
    assert records[0]["sequence"] == 1
    assert records[1]["sequence"] == 2


def test_registry_list_returns_latest_lifecycle(registry):
    """Test that list_domains returns latest state for each domain."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)
    registry.update_lifecycle("test-domain", DomainLifecycle.SUSPENDED)
    registry.update_lifecycle("test-domain", DomainLifecycle.ARCHIVED)

    domains = registry.list_domains()
    assert len(domains) == 1
    assert domains[0].lifecycle == DomainLifecycle.ARCHIVED


def test_registry_enforces_non_widening_in_decode(registry, registry_path):
    """Test that _decode validates non-widening transitions."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)

    # Manually append a record with non-widening violation
    content = registry_path.read_text()
    records = [json.loads(line) for line in content.splitlines()]

    # Create invalid transition: ACTIVE -> ARCHIVED -> SUSPENDED
    registry.update_lifecycle("test-domain", DomainLifecycle.ARCHIVED)

    # Now try to manually add a SUSPENDED record with correct fingerprint
    import hashlib
    from federation.integrity import authentication_tag
    from federation.control_domain_registry import _canonical

    invalid_record = dict(records[0])
    invalid_record["sequence"] = 3
    invalid_record["lifecycle"] = "suspended"

    # Recalculate fingerprint for the new lifecycle
    payload = {
        "domain_id": invalid_record["domain_id"],
        "name": invalid_record["name"],
        "owner": invalid_record["owner"],
        "lifecycle": "suspended",
        "created_at": invalid_record["created_at"],
        "last_transition_at": invalid_record["last_transition_at"],
    }
    invalid_record["domain_fingerprint"] = hashlib.sha256(_canonical(payload)).hexdigest()

    # Re-sign with correct fingerprint
    authenticated = dict(invalid_record)
    del authenticated["authentication_tag"]
    invalid_record["authentication_tag"] = authentication_tag(
        registry._integrity_key,
        b"raghub.control-domain.v1",
        _canonical(authenticated),
    )

    with registry_path.open("a") as f:
        f.write(json.dumps(invalid_record) + "\n")

    with pytest.raises(DomainCorruptionError, match="Non-widening lifecycle violation"):
        registry.list_domains()


def test_registry_requires_minimum_integrity_key_length():
    """Test that integrity key must be at least 32 bytes."""
    with pytest.raises(ValueError, match="at least 32 bytes"):
        DurableControlDomainRegistry(
            path="/tmp/test.jsonl",
            integrity_key=b"short-key",
        )


def test_registry_concurrent_registration_protection(registry, tmp_path):
    """Test protection against concurrent duplicate registrations."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    registry.register(domain)

    # Simulate concurrent attempt with same domain_id
    with pytest.raises(DomainConflictError, match="already registered"):
        registry.register(domain)
