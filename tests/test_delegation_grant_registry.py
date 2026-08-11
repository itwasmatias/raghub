"""Adversarial tests for the durable delegation grant registry."""

from __future__ import annotations

import json
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation.agent_identity import AgentIdentity
from federation.agent_identity_registry import DurableAgentIdentityRegistry
from federation.control_domain import ControlDomain, DomainLifecycle
from federation.control_domain_registry import DurableControlDomainRegistry
from federation.delegation_grant import DelegationGrant, DelegationGrantStatus
from federation.delegation_grant_registry import (
    DelegationGrantConflictError,
    DelegationGrantCorruptionError,
    DelegationGrantDomainError,
    DelegationGrantIdentityError,
    DelegationGrantLifecycleError,
    DelegationGrantNotFoundError,
    DelegationGrantRegistry,
    DelegationGrantScopeError,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
KEY = b"delegation-grant-v0.1-test-integrity-key-0001"


class MutableClock:
    def __init__(self, current=NOW):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, delta):
        self.current += delta


def _register_identity(registry, *, domain_id: str, agent_id: str, name: str) -> None:
    registry.register(
        AgentIdentity(
            agent_id=agent_id,
            domain_id=domain_id,
            name=name,
            created_at=NOW,
        )
    )


def _grant(
    *,
    domain_id: str = "domain-a",
    mission_id: str = "mission-a",
    grant_id: str = "grant-1",
    grantor_identity: str = "grantor-a",
    grantee_identity: str = "grantee-a",
    authority_scope: tuple[str, ...] = ("read",),
    created_at: datetime = NOW,
    effective_at: datetime | None = None,
    expires_at: datetime | None = None,
    parent_grant_id: str | None = None,
) -> DelegationGrant:
    effective = created_at if effective_at is None else effective_at
    return DelegationGrant(
        grant_id=grant_id,
        domain_id=domain_id,
        mission_id=mission_id,
        grantor_identity=grantor_identity,
        grantee_identity=grantee_identity,
        authority_scope=authority_scope,
        parent_grant_id=parent_grant_id,
        created_at=created_at,
        effective_at=effective,
        expires_at=effective + timedelta(hours=1) if expires_at is None else expires_at,
    )


def _concurrent_register(
    grant_path: str,
    domain_path: str,
    identity_path: str,
    grant_kwargs: dict[str, object],
    output,
) -> None:
    domain_registry = DurableControlDomainRegistry(
        Path(domain_path),
        integrity_key=KEY,
    )
    identity_registry = DurableAgentIdentityRegistry(
        Path(identity_path),
        domain_registry=domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    registry = DelegationGrantRegistry(
        Path(grant_path),
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    try:
        result = registry.register(_grant(**grant_kwargs))
    except Exception as exc:  # pragma: no cover - reported to the parent
        output.put((type(exc).__name__, str(exc)))
    else:
        output.put(("accepted", result.grant_id))


@pytest.fixture
def domain_registry_path(tmp_path):
    return tmp_path / "control-domains.jsonl"


@pytest.fixture
def identity_registry_path(tmp_path):
    return tmp_path / "agent-identities.jsonl"


@pytest.fixture
def grant_registry_path(tmp_path):
    return tmp_path / "delegation-grants.jsonl"


@pytest.fixture
def control_domain_registry(domain_registry_path):
    registry = DurableControlDomainRegistry(
        path=domain_registry_path,
        integrity_key=KEY,
    )
    registry.register(
        ControlDomain(
            domain_id="domain-a",
            name="Domain A",
            owner="owner-a",
            created_at=NOW,
        )
    )
    registry.register(
        ControlDomain(
            domain_id="domain-b",
            name="Domain B",
            owner="owner-b",
            created_at=NOW,
        )
    )
    return registry


@pytest.fixture
def identity_registry(identity_registry_path, control_domain_registry):
    registry = DurableAgentIdentityRegistry(
        identity_registry_path,
        domain_registry=control_domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    _register_identity(registry, domain_id="domain-a", agent_id="grantor-a", name="Grantor A")
    _register_identity(registry, domain_id="domain-a", agent_id="grantee-a", name="Grantee A")
    _register_identity(registry, domain_id="domain-a", agent_id="delegate-a", name="Delegate A")
    _register_identity(registry, domain_id="domain-b", agent_id="grantor-b", name="Grantor B")
    _register_identity(registry, domain_id="domain-b", agent_id="grantee-b", name="Grantee B")
    return registry


@pytest.fixture
def grant_clock():
    return MutableClock()


@pytest.fixture
def grant_registry(grant_registry_path, control_domain_registry, identity_registry, grant_clock):
    return DelegationGrantRegistry(
        grant_registry_path,
        domain_registry=control_domain_registry,
        identity_registry=identity_registry,
        integrity_key=KEY,
        clock=grant_clock,
    )


def test_register_and_get_grant(grant_registry, grant_registry_path):
    state = grant_registry.register(_grant())

    assert state.grant_id == "grant-1"
    assert state.domain_id == "domain-a"
    assert state.mission_id == "mission-a"
    assert state.grantor_identity == "grantor-a"
    assert state.grantee_identity == "grantee-a"
    assert state.status is DelegationGrantStatus.ACTIVE
    assert state.current_status(NOW) is DelegationGrantStatus.ACTIVE
    assert len(state.grant_fingerprint) == 64
    assert grant_registry_path.read_text().endswith("\n")

    retrieved = grant_registry.get("grant-1", domain_id="domain-a", mission_id="mission-a")
    assert retrieved == state
    assert grant_registry.list_grants(status_filter=DelegationGrantStatus.ACTIVE) == (state,)


def test_grant_lookup_requires_explicit_context(grant_registry):
    grant_registry.register(_grant())

    with pytest.raises(DelegationGrantDomainError, match="explicit ControlDomain and mission context"):
        grant_registry.get("grant-1")

    with pytest.raises(DelegationGrantDomainError, match="explicit ControlDomain and mission context"):
        grant_registry.revoke("grant-1", reason="compromised")


def test_missing_domain_rejected(grant_registry):
    with pytest.raises(DelegationGrantDomainError, match="not registered"):
        grant_registry.register(_grant(domain_id="missing-domain"))


def test_inactive_domain_rejected(control_domain_registry, identity_registry, grant_registry_path):
    control_domain_registry.update_lifecycle("domain-a", DomainLifecycle.ARCHIVED)
    registry = DelegationGrantRegistry(
        grant_registry_path,
        domain_registry=control_domain_registry,
        identity_registry=identity_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )

    with pytest.raises(DelegationGrantDomainError, match="not active"):
        registry.register(_grant())


def test_domain_mismatch_on_creation_rejected(grant_registry):
    with pytest.raises(DelegationGrantIdentityError, match="not registered in domain"):
        grant_registry.register(_grant(grantor_identity="grantor-b"))


def test_exact_duplicate_registration_is_idempotent(grant_registry):
    identity = _grant()
    first = grant_registry.register(identity)
    before = grant_registry.path.read_bytes()

    second = grant_registry.register(identity)

    assert second == first
    assert grant_registry.path.read_bytes() == before
    assert len(grant_registry.list_grants()) == 1


def test_exact_duplicate_registration_revalidates_domain_context(
    control_domain_registry,
    identity_registry,
    grant_registry_path,
):
    registry = DelegationGrantRegistry(
        grant_registry_path,
        domain_registry=control_domain_registry,
        identity_registry=identity_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    identity = _grant()
    registry.register(identity)
    control_domain_registry.update_lifecycle("domain-a", DomainLifecycle.ARCHIVED)

    with pytest.raises(DelegationGrantDomainError, match="not active"):
        registry.register(identity)


def test_conflicting_duplicate_registration_fails_closed(grant_registry):
    grant_registry.register(_grant())

    with pytest.raises(DelegationGrantConflictError, match="already exists"):
        grant_registry.register(_grant(grantee_identity="delegate-a"))


def test_attenuation_only_never_amplification(grant_registry):
    parent = grant_registry.register(
        _grant(
            grant_id="grant-parent",
            grantor_identity="grantor-a",
            grantee_identity="grantee-a",
            authority_scope=("read", "write"),
        )
    )

    child = grant_registry.register(
        _grant(
            grant_id="grant-child",
            grantor_identity="grantee-a",
            grantee_identity="delegate-a",
            authority_scope=("read",),
            parent_grant_id="grant-parent",
        )
    )

    assert child.parent_grant_id == parent.grant_id
    assert child.authority_scope == ("read",)
    assert child.status is DelegationGrantStatus.ACTIVE

    with pytest.raises(DelegationGrantScopeError, match="widens"):
        grant_registry.register(
            _grant(
                grant_id="grant-amplify",
                grantor_identity="grantee-a",
                grantee_identity="delegate-a",
                authority_scope=("read", "write", "admin"),
                parent_grant_id="grant-parent",
            )
        )


def test_revoke_is_non_widening_and_idempotent(grant_registry):
    grant_registry.register(_grant(grant_id="grant-revoke"))

    revoked = grant_registry.revoke(
        "grant-revoke",
        domain_id="domain-a",
        mission_id="mission-a",
        reason="compromised",
    )

    assert revoked.status is DelegationGrantStatus.REVOKED
    assert not revoked.is_active()
    assert revoked.revocation_reason == "compromised"
    assert grant_registry.get("grant-revoke", domain_id="domain-a", mission_id="mission-a").status is DelegationGrantStatus.REVOKED
    assert grant_registry.list_grants(status_filter=DelegationGrantStatus.REVOKED) == (revoked,)

    again = grant_registry.revoke(
        "grant-revoke",
        domain_id="domain-a",
        mission_id="mission-a",
        reason="compromised",
    )
    assert again == revoked

    with pytest.raises(DelegationGrantConflictError, match="revocation reason conflicts"):
        grant_registry.revoke(
            "grant-revoke",
            domain_id="domain-a",
            mission_id="mission-a",
            reason="different",
        )


def test_expiration_handling(grant_clock, grant_registry):
    grant_registry.register(
        _grant(
            grant_id="grant-expire",
            expires_at=NOW + timedelta(seconds=30),
        )
    )
    grant_clock.advance(timedelta(seconds=45))

    expired = grant_registry.get(
        "grant-expire",
        domain_id="domain-a",
        mission_id="mission-a",
    )

    assert expired.status is DelegationGrantStatus.EXPIRED
    assert not expired.is_active()
    assert expired.to_dict()["status"] == "expired"
    assert grant_registry.list_grants(status_filter=DelegationGrantStatus.EXPIRED)[0] == expired


def test_future_effective_registration_stays_pending_until_effective(
    grant_clock,
    grant_registry,
):
    grant_registry.register(
        _grant(
            grant_id="grant-future",
            effective_at=NOW + timedelta(minutes=5),
            expires_at=NOW + timedelta(minutes=15),
        )
    )

    pending = grant_registry.get(
        "grant-future",
        domain_id="domain-a",
        mission_id="mission-a",
    )

    assert pending.status is DelegationGrantStatus.PENDING
    assert not pending.is_active()
    assert grant_registry.list_grants(status_filter=DelegationGrantStatus.PENDING)[0] == pending

    grant_clock.advance(timedelta(minutes=6))
    active = grant_registry.get(
        "grant-future",
        domain_id="domain-a",
        mission_id="mission-a",
    )

    assert active.status is DelegationGrantStatus.ACTIVE
    assert active.is_active(grant_clock.current)


def test_revoked_to_expired_rewrite_is_rejected(grant_registry):
    grant_registry.register(_grant(grant_id="grant-rewrite"))
    grant_registry.revoke(
        "grant-rewrite",
        domain_id="domain-a",
        mission_id="mission-a",
        reason="compromised",
    )

    records = grant_registry._read()
    current = grant_registry._current_by_key(records)[
        ("domain-a", "mission-a", "grant-rewrite")
    ]
    forged_payload = current.to_dict()
    forged_payload["status"] = DelegationGrantStatus.EXPIRED.value
    forged_payload["revoked_at"] = None
    forged_payload["revocation_reason"] = None
    forged_record = grant_registry._append_record(records, forged_payload)
    serialized = json.dumps(
        forged_record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    grant_registry.path.write_text(grant_registry.path.read_text() + serialized + "\n")

    with pytest.raises(DelegationGrantCorruptionError, match="status is invalid"):
        grant_registry.get(
            "grant-rewrite",
            domain_id="domain-a",
            mission_id="mission-a",
        )


def test_tampering_detection(grant_registry):
    grant_registry.register(_grant())
    grant_registry.path.write_text(
        grant_registry.path.read_text().replace("grant-1", "grant-x", 1),
    )

    with pytest.raises(DelegationGrantCorruptionError, match="authentication failed"):
        grant_registry.get("grant-1", domain_id="domain-a", mission_id="mission-a")


def test_ambiguous_lookup_without_context_fails_closed(grant_registry):
    grant_registry.register(_grant())
    grant_registry.register(
        _grant(
            domain_id="domain-b",
            mission_id="mission-b",
            grant_id="grant-1",
            grantor_identity="grantor-b",
            grantee_identity="grantee-b",
        )
    )

    with pytest.raises(DelegationGrantDomainError, match="explicit ControlDomain and mission context"):
        grant_registry.get("grant-1")


def test_restart_reconstruction_is_deterministic(
    grant_registry_path,
    control_domain_registry,
    identity_registry,
):
    registry = DelegationGrantRegistry(
        grant_registry_path,
        domain_registry=control_domain_registry,
        identity_registry=identity_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    registry.register(_grant())
    registry.register(
        _grant(
            grant_id="grant-child",
            grantor_identity="grantee-a",
            grantee_identity="delegate-a",
            authority_scope=("read",),
            parent_grant_id="grant-1",
        )
    )

    reloaded = DelegationGrantRegistry(
        grant_registry_path,
        domain_registry=control_domain_registry,
        identity_registry=identity_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )

    assert reloaded.snapshot() == registry.snapshot()
    assert reloaded.get("grant-child", domain_id="domain-a", mission_id="mission-a") == registry.get(
        "grant-child",
        domain_id="domain-a",
        mission_id="mission-a",
    )


def test_path_handling_with_spaces(tmp_path):
    root = tmp_path / "grant registry with spaces"
    domain_registry_path = root / "control domains.jsonl"
    identity_registry_path = root / "agent identities.jsonl"
    grant_registry_path = root / "delegation grants.jsonl"

    domain_registry = DurableControlDomainRegistry(
        domain_registry_path,
        integrity_key=KEY,
    )
    domain_registry.register(
        ControlDomain(
            domain_id="domain-a",
            name="Domain A",
            owner="owner-a",
            created_at=NOW,
        )
    )
    identity_registry = DurableAgentIdentityRegistry(
        identity_registry_path,
        domain_registry=domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    _register_identity(identity_registry, domain_id="domain-a", agent_id="grantor-a", name="Grantor A")
    _register_identity(identity_registry, domain_id="domain-a", agent_id="grantee-a", name="Grantee A")

    registry = DelegationGrantRegistry(
        grant_registry_path,
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )

    created = registry.register(_grant())
    assert created.grant_id == "grant-1"
    assert registry.get("grant-1", domain_id="domain-a", mission_id="mission-a") == created


def test_concurrent_creation_conflict(tmp_path):
    root = tmp_path / "grant registry concurrency"
    domain_registry_path = root / "control-domains.jsonl"
    identity_registry_path = root / "agent-identities.jsonl"
    grant_registry_path = root / "delegation-grants.jsonl"

    domain_registry = DurableControlDomainRegistry(
        domain_registry_path,
        integrity_key=KEY,
    )
    domain_registry.register(
        ControlDomain(
            domain_id="domain-a",
            name="Domain A",
            owner="owner-a",
            created_at=NOW,
        )
    )
    identity_registry = DurableAgentIdentityRegistry(
        identity_registry_path,
        domain_registry=domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    _register_identity(identity_registry, domain_id="domain-a", agent_id="grantor-a", name="Grantor A")
    _register_identity(identity_registry, domain_id="domain-a", agent_id="grantee-a", name="Grantee A")

    output = multiprocessing.Queue()
    first = multiprocessing.Process(
        target=_concurrent_register,
        args=(
            str(grant_registry_path),
            str(domain_registry_path),
            str(identity_registry_path),
            {
                "grant_id": "grant-race",
                "authority_scope": ("read",),
            },
            output,
        ),
    )
    second = multiprocessing.Process(
        target=_concurrent_register,
        args=(
            str(grant_registry_path),
            str(domain_registry_path),
            str(identity_registry_path),
            {
                "grant_id": "grant-race",
                "authority_scope": ("read", "write"),
            },
            output,
        ),
    )

    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert first.exitcode == 0
    assert second.exitcode == 0

    outcomes = [output.get(timeout=5) for _ in range(2)]
    assert any(item[0] == "accepted" for item in outcomes)
    assert any(item[0] == "DelegationGrantConflictError" for item in outcomes)
