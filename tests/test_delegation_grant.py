"""Tests for durable delegation grant root values."""

from datetime import datetime, timedelta, timezone

from federation.delegation_grant import (
    AuthoritativeDelegationGrant,
    DelegationGrant,
    DelegationGrantStatus,
    grant_fingerprint,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def test_delegation_grant_creation() -> None:
    grant = DelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        authority_scope=("read", "write"),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    assert grant.grant_id == "grant-1"
    assert grant.domain_id == "domain-1"
    assert grant.mission_id == "mission-1"
    assert grant.authority_scope == ("read", "write")
    assert grant.to_dict()["expires_at"] == (NOW + timedelta(hours=1)).isoformat()


def test_grant_fingerprint_ignores_lifecycle_metadata() -> None:
    common = dict(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        authority_scope=("read", "write"),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        parent_grant_id=None,
        parent_grant_fingerprint=None,
    )

    active = grant_fingerprint(
        **common,
        status=DelegationGrantStatus.ACTIVE,
        revoked_at=None,
        revocation_reason=None,
    )
    revoked = grant_fingerprint(
        **common,
        status=DelegationGrantStatus.REVOKED,
        revoked_at=NOW + timedelta(minutes=5),
        revocation_reason="compromised",
    )

    assert active == revoked
    assert len(active) == 64


def test_authoritative_grant_status_tracks_expiry_and_revocation() -> None:
    grant = AuthoritativeDelegationGrant(
        grant_id="grant-2",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        authority_scope=("read",),
        parent_grant_id=None,
        parent_grant_fingerprint=None,
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="f" * 64,
    )

    assert grant.current_status(NOW) is DelegationGrantStatus.ACTIVE
    assert grant.is_active(NOW)
    assert grant.current_status(NOW + timedelta(minutes=10)) is DelegationGrantStatus.EXPIRED
    assert not grant.is_active(NOW + timedelta(minutes=10))

    revoked = AuthoritativeDelegationGrant(
        grant_id="grant-2",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        authority_scope=("read",),
        parent_grant_id=None,
        parent_grant_fingerprint=None,
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        status=DelegationGrantStatus.REVOKED,
        grant_fingerprint="f" * 64,
        revoked_at=NOW + timedelta(minutes=1),
        revocation_reason="compromised",
    )

    assert revoked.current_status(NOW) is DelegationGrantStatus.REVOKED
    assert not revoked.is_active(NOW)
