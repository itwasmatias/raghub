"""Tests for ControlDomain model."""

from datetime import datetime, timedelta, timezone

import pytest

from federation.control_domain import ControlDomain, DomainLifecycle


def test_control_domain_creation():
    """Test basic ControlDomain creation."""
    domain = ControlDomain(
        domain_id="example-domain",
        name="Example Organization",
        owner="example-owner",
    )

    assert domain.domain_id == "example-domain"
    assert domain.name == "Example Organization"
    assert domain.owner == "example-owner"
    assert domain.lifecycle == DomainLifecycle.ACTIVE
    assert isinstance(domain.created_at, datetime)
    assert domain.created_at.tzinfo is not None
    assert domain.created_at.utcoffset() == timedelta(0)


def test_control_domain_with_lifecycle():
    """Test ControlDomain creation with explicit lifecycle."""
    domain = ControlDomain(
        domain_id="suspended-domain",
        name="Suspended Org",
        owner="admin",
        lifecycle=DomainLifecycle.SUSPENDED,
    )

    assert domain.lifecycle == DomainLifecycle.SUSPENDED


def test_control_domain_is_active():
    """Test checking if domain is active."""
    active_domain = ControlDomain(
        domain_id="active",
        name="Active Org",
        owner="admin",
        lifecycle=DomainLifecycle.ACTIVE,
    )

    suspended_domain = ControlDomain(
        domain_id="suspended",
        name="Suspended Org",
        owner="admin",
        lifecycle=DomainLifecycle.SUSPENDED,
    )

    assert active_domain.is_active()
    assert not suspended_domain.is_active()


def test_control_domain_suspend():
    """Test suspending an active domain."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    assert domain.lifecycle == DomainLifecycle.ACTIVE
    domain.suspend()
    assert domain.lifecycle == DomainLifecycle.SUSPENDED


def test_control_domain_suspend_already_suspended():
    """Test suspending an already suspended domain (idempotent)."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
        lifecycle=DomainLifecycle.SUSPENDED,
    )

    domain.suspend()  # Should not raise
    assert domain.lifecycle == DomainLifecycle.SUSPENDED


def test_control_domain_cannot_suspend_archived():
    """Test that archived domains cannot be suspended (non-widening)."""
    domain = ControlDomain(
        domain_id="archived-domain",
        name="Archived Org",
        owner="admin",
        lifecycle=DomainLifecycle.ARCHIVED,
    )

    with pytest.raises(ValueError, match="non-widening"):
        domain.suspend()


def test_control_domain_archive():
    """Test archiving a domain."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    domain.archive()
    assert domain.lifecycle == DomainLifecycle.ARCHIVED


def test_control_domain_archive_from_suspended():
    """Test archiving a suspended domain."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
        lifecycle=DomainLifecycle.SUSPENDED,
    )

    domain.archive()
    assert domain.lifecycle == DomainLifecycle.ARCHIVED


def test_control_domain_archive_idempotent():
    """Test archiving an already archived domain (idempotent)."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
        lifecycle=DomainLifecycle.ARCHIVED,
    )

    domain.archive()  # Should not raise
    assert domain.lifecycle == DomainLifecycle.ARCHIVED


def test_control_domain_lifecycle_non_widening():
    """Test that lifecycle transitions are non-widening."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
    )

    # ACTIVE -> SUSPENDED -> ARCHIVED is allowed
    domain.suspend()
    domain.archive()

    # But once archived, cannot go back
    with pytest.raises(ValueError, match="non-widening"):
        domain.suspend()


def test_control_domain_validation_empty_domain_id():
    """Test that empty domain_id raises error."""
    with pytest.raises(ValueError, match="domain_id"):
        ControlDomain(
            domain_id="",
            name="Test Org",
            owner="admin",
        )


def test_control_domain_validation_empty_name():
    """Test that empty name raises error."""
    with pytest.raises(ValueError, match="name"):
        ControlDomain(
            domain_id="test-domain",
            name="",
            owner="admin",
        )


def test_control_domain_validation_empty_owner():
    """Test that empty owner raises error."""
    with pytest.raises(ValueError, match="owner"):
        ControlDomain(
            domain_id="test-domain",
            name="Test Org",
            owner="",
        )


def test_control_domain_lifecycle_string_conversion():
    """Test that string lifecycle values are converted to DomainLifecycle enum."""
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
        lifecycle="suspended",
    )

    assert domain.lifecycle == DomainLifecycle.SUSPENDED
    assert isinstance(domain.lifecycle, DomainLifecycle)


def test_control_domain_lifecycle_invalid_string():
    """Test that invalid lifecycle string raises error."""
    with pytest.raises(ValueError, match="Invalid lifecycle"):
        ControlDomain(
            domain_id="test-domain",
            name="Test Org",
            owner="admin",
            lifecycle="invalid",
        )


def test_control_domain_rejects_invalid_lifecycle_type():
    """Test that lifecycle must be DomainLifecycle or string."""
    with pytest.raises(TypeError, match="lifecycle"):
        ControlDomain(
            domain_id="test-domain",
            name="Test Org",
            owner="admin",
            lifecycle=1,
        )


def test_control_domain_normalizes_aware_created_at_to_utc():
    """Test that aware timestamps are stored in UTC."""
    eastern = timezone(timedelta(hours=-5))
    domain = ControlDomain(
        domain_id="test-domain",
        name="Test Org",
        owner="admin",
        created_at=datetime(2026, 8, 11, 9, 30, tzinfo=eastern),
    )

    assert domain.created_at == datetime(2026, 8, 11, 14, 30, tzinfo=timezone.utc)


def test_control_domain_rejects_naive_created_at():
    """Test that naive timestamps are rejected."""
    with pytest.raises(ValueError, match="timezone-aware"):
        ControlDomain(
            domain_id="test-domain",
            name="Test Org",
            owner="admin",
            created_at=datetime(2026, 8, 11, 14, 30),
        )


def test_control_domain_with_all_lifecycles():
    """Test creating domains with all possible lifecycle states."""
    for lifecycle in DomainLifecycle:
        domain = ControlDomain(
            domain_id="test-domain",
            name="Test Org",
            owner="admin",
            lifecycle=lifecycle,
        )
        assert domain.lifecycle == lifecycle
