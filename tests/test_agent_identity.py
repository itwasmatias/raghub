"""Tests for durable agent identity root values."""

from datetime import datetime, timedelta, timezone

import pytest

from federation.agent_identity import AgentIdentity, AgentIdentityLifecycle


def test_agent_identity_creation() -> None:
    identity = AgentIdentity(
        agent_id="agent-1",
        domain_id="domain-1",
        name="Example Agent",
    )

    assert identity.agent_id == "agent-1"
    assert identity.domain_id == "domain-1"
    assert identity.name == "Example Agent"
    assert identity.lifecycle is AgentIdentityLifecycle.ACTIVE
    assert isinstance(identity.created_at, datetime)
    assert identity.created_at.tzinfo is not None
    assert identity.created_at.utcoffset() == timedelta(0)


def test_agent_identity_with_lifecycle() -> None:
    identity = AgentIdentity(
        agent_id="agent-2",
        domain_id="domain-1",
        name="Example Agent",
        lifecycle=AgentIdentityLifecycle.REVOKED,
    )

    assert identity.lifecycle is AgentIdentityLifecycle.REVOKED


def test_agent_identity_is_active() -> None:
    active = AgentIdentity(
        agent_id="agent-active",
        domain_id="domain-1",
        name="Active Agent",
    )
    revoked = AgentIdentity(
        agent_id="agent-revoked",
        domain_id="domain-1",
        name="Revoked Agent",
        lifecycle=AgentIdentityLifecycle.REVOKED,
    )

    assert active.is_active()
    assert not revoked.is_active()


def test_agent_identity_revoke_and_archive_non_widening() -> None:
    identity = AgentIdentity(
        agent_id="agent-3",
        domain_id="domain-1",
        name="Example Agent",
    )

    identity.revoke()
    assert identity.lifecycle is AgentIdentityLifecycle.REVOKED
    identity.archive()
    assert identity.lifecycle is AgentIdentityLifecycle.ARCHIVED

    with pytest.raises(ValueError, match="non-widening"):
        identity.revoke()


def test_agent_identity_archive_is_idempotent() -> None:
    identity = AgentIdentity(
        agent_id="agent-4",
        domain_id="domain-1",
        name="Example Agent",
        lifecycle=AgentIdentityLifecycle.ARCHIVED,
    )

    identity.archive()
    assert identity.lifecycle is AgentIdentityLifecycle.ARCHIVED


def test_agent_identity_normalizes_aware_created_at_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    identity = AgentIdentity(
        agent_id="agent-5",
        domain_id="domain-1",
        name="Example Agent",
        created_at=datetime(2026, 8, 11, 9, 30, tzinfo=eastern),
    )

    assert identity.created_at == datetime(2026, 8, 11, 14, 30, tzinfo=timezone.utc)


def test_agent_identity_rejects_naive_created_at() -> None:
    with pytest.raises(TypeError, match="timezone-aware"):
        AgentIdentity(
            agent_id="agent-6",
            domain_id="domain-1",
            name="Example Agent",
            created_at=datetime(2026, 8, 11, 14, 30),
        )


def test_agent_identity_lifecycle_string_conversion() -> None:
    identity = AgentIdentity(
        agent_id="agent-7",
        domain_id="domain-1",
        name="Example Agent",
        lifecycle="revoked",
    )

    assert identity.lifecycle is AgentIdentityLifecycle.REVOKED


def test_agent_identity_lifecycle_invalid_string() -> None:
    with pytest.raises(ValueError, match="Invalid lifecycle"):
        AgentIdentity(
            agent_id="agent-8",
            domain_id="domain-1",
            name="Example Agent",
            lifecycle="invalid",
        )


def test_agent_identity_rejects_invalid_lifecycle_type() -> None:
    with pytest.raises(TypeError, match="lifecycle"):
        AgentIdentity(
            agent_id="agent-9",
            domain_id="domain-1",
            name="Example Agent",
            lifecycle=1,
        )

