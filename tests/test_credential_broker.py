"""Adversarial tests for the credential broker boundary."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from federation import (
    AgentIdentity,
    ComponentKind,
    CredentialBroker,
    CredentialBrokerAuthorityError,
    CredentialBrokerNotFoundError,
    CredentialRef,
    DelegationGrant,
    EffectAuthorityError,
    EffectBoundary,
    EffectRequest,
    GovernedPowerComponent,
    NodeCapability,
    NodeRecord,
    NodeRegistry,
    PowerAction,
    PowerExecutionAuthorizationAuthority,
    PowerPolicy,
    PowerCoordinator,
    PowerProposal,
    PowerStatus,
    PowerRefusalError,
    RecordingPowerAdapter,
)
from federation.control_domain import ControlDomain
from federation.control_domain_registry import DurableControlDomainRegistry
from federation.delegation_grant_registry import DelegationGrantRegistry
from federation.agent_identity_registry import DurableAgentIdentityRegistry
from federation.subprocess_environment import build_subprocess_environment
from federation.worker_liveness import LivenessState


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
KEY = b"credential-broker-v0.1-test-integrity-key-0001"
SECRET_VALUE = "synthetic-broker-secret-0001"

CHILD_PROBE = (
    "import json, os, sys\n"
    "print(json.dumps({'argv': sys.argv[1:], 'env_values': list(os.environ.values())}, sort_keys=True))\n"
)


class MutableClock:
    def __init__(self, current=NOW):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, delta):
        self.current += delta


class Liveness:
    def __init__(self, state=LivenessState.ONLINE, capabilities=()):
        self.state = state
        self.power_capabilities = tuple(capabilities)
        self.authentication_tag = "authenticated-heartbeat"


class LivenessRegistry:
    def __init__(self, lease):
        self.lease = lease

    def inspect(self, worker_id):
        return self.lease if worker_id == "worker-1" else None


class SecretStore:
    def __init__(self, secrets):
        self.secrets = dict(secrets)
        self.calls = []

    def __call__(self, credential_ref):
        self.calls.append(credential_ref)
        return self.secrets[credential_ref.lookup_key()]


def _component(
    *,
    actions=(PowerAction.DISPLAY_OFF, PowerAction.DISPLAY_ON),
    auto_display=True,
    coordinator_id=None,
):
    return GovernedPowerComponent(
        worker_id="worker-1",
        component_id="display-1",
        kind=ComponentKind.DISPLAY,
        supported_actions=actions,
        policy=PowerPolicy(
            version="policy-v1",
            automatic_display_control=auto_display,
            interactive_session_prohibited=False,
            approved_wake_coordinator_id=coordinator_id,
        ),
        controller_authority="controller-1",
        integrity_authority="integrity-key-1",
        adapter_id="recording-1",
    )


def _proposal(
    action=PowerAction.DISPLAY_OFF,
    *,
    sequence=1,
    component_id="display-1",
):
    return PowerProposal.create(
        worker_id="worker-1",
        component_id=component_id,
        action=action,
        sequence=sequence,
        controller_authority="controller-1",
        integrity_authority="integrity-key-1",
        policy_version="policy-v1",
        requested_at=NOW,
        checkpoint_evidence=None,
        protected_work_safe=True,
        wake_path_evidence=None,
        wake_coordinator_id=None,
        execute_after=None,
        expires_at=None,
        integrity_key=KEY,
    )


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
    grantee_identity: str = "agent-a",
    authority_scope: tuple[str, ...] = ("display_off",),
):
    return DelegationGrant(
        grant_id=grant_id,
        domain_id=domain_id,
        mission_id=mission_id,
        grantor_identity=grantor_identity,
        grantee_identity=grantee_identity,
        authority_scope=authority_scope,
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def _registries(tmp_path):
    domain_registry = DurableControlDomainRegistry(
        tmp_path / "control-domains.jsonl",
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
    domain_registry.register(
        ControlDomain(
            domain_id="domain-b",
            name="Domain B",
            owner="owner-b",
            created_at=NOW,
        )
    )

    identity_registry = DurableAgentIdentityRegistry(
        tmp_path / "agent-identities.jsonl",
        domain_registry=domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    _register_identity(identity_registry, domain_id="domain-a", agent_id="grantor-a", name="Grantor A")
    _register_identity(identity_registry, domain_id="domain-a", agent_id="agent-a", name="Agent A")
    _register_identity(identity_registry, domain_id="domain-b", agent_id="grantor-b", name="Grantor B")
    _register_identity(identity_registry, domain_id="domain-b", agent_id="agent-b", name="Agent B")

    grant_registry = DelegationGrantRegistry(
        tmp_path / "delegation-grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    return domain_registry, identity_registry, grant_registry


def _make_coordinator(
    tmp_path,
    *,
    governed_component=None,
    state=LivenessState.ONLINE,
    capabilities=("display_control",),
    clock=None,
):
    nodes = NodeRegistry()
    nodes.register(
        NodeRecord(
            domain_id="domain-a",
            node_id="worker-1",
            hostname="worker",
            operating_system="test",
            capabilities={NodeCapability("display_control")},
        )
    )
    adapter = RecordingPowerAdapter("recording-1")
    coordinator = PowerCoordinator(
        tmp_path / "power.evidence",
        node_registry=nodes,
        heartbeat_registry=LivenessRegistry(Liveness(state, capabilities)),
        controller_authority="controller-1",
        integrity_authority="integrity-key-1",
        integrity_key=KEY,
        execution_authorization_authority=PowerExecutionAuthorizationAuthority(KEY),
        adapters={adapter.adapter_id: adapter},
        clock=clock or MutableClock(),
    )
    coordinator.register_component(governed_component or _component())
    return coordinator, adapter


def _effect_request(
    *,
    domain_id="domain-a",
    mission_id="mission-a",
    agent_id="agent-a",
    grant_id="grant-1",
    proposal=None,
):
    from federation import EffectRequest

    proposal = _proposal() if proposal is None else proposal
    return EffectRequest.create(
        domain_id=domain_id,
        mission_id=mission_id,
        agent_id=agent_id,
        grant_id=grant_id,
        proposal=proposal,
        integrity_key=KEY,
    )


def _boundary(
    tmp_path,
    *,
    governed_component=None,
    state=LivenessState.ONLINE,
    capabilities=("display_control",),
    grant_kwargs=None,
):
    domain_registry, identity_registry, grant_registry = _registries(tmp_path)
    grant_registry.register(_grant(**(grant_kwargs or {})))
    coordinator, _ = _make_coordinator(
        tmp_path,
        governed_component=governed_component,
        state=state,
        capabilities=capabilities,
    )
    boundary = EffectBoundary(
        coordinator=coordinator,
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        grant_registry=grant_registry,
        integrity_key=KEY,
    )
    return boundary, coordinator, grant_registry


def _broker(tmp_path, **boundary_kwargs):
    boundary, coordinator, grant_registry = _boundary(tmp_path, **boundary_kwargs)
    broker = CredentialBroker(
        effect_boundary=boundary,
        secret_source=SecretStore({("synthetic", "api_key", "credential-1"): SECRET_VALUE}),
    )
    return boundary, broker, coordinator, grant_registry


def test_authorized_release_returns_only_bound_secret_and_redacts_it(tmp_path):
    boundary, broker, _, grant_registry = _broker(tmp_path)
    request = _effect_request()

    boundary.propose(request)
    boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))
    lease = broker.release(request, CredentialRef("credential-1", provider="synthetic", kind="api_key"))

    assert lease.secret_text() == SECRET_VALUE
    assert lease.credential_ref.credential_id == "credential-1"
    assert lease.request_id == request.effect_request_id
    assert lease.effect_request_id == request.effect_request_id
    assert lease.decision.snapshot.status is PowerStatus.EXECUTION_AUTHORIZED
    assert SECRET_VALUE not in repr(lease)
    assert SECRET_VALUE not in str(lease)
    assert SECRET_VALUE not in json.dumps(lease.to_dict())
    assert lease.to_dict()["credential_ref"]["credential_id"] == "credential-1"
    assert lease.to_dict()["agent_id"] == "agent-a"
    assert grant_registry.get("grant-1", domain_id="domain-a", mission_id="mission-a").status.value == "active"

    result = subprocess.run(
        [sys.executable, "-c", CHILD_PROBE, lease.credential_ref.credential_id],
        input=lease.secret_text(),
        text=True,
        capture_output=True,
        env=build_subprocess_environment(trusted_additions={"TERM": "dumb"}),
        check=True,
    )
    payload = json.loads(result.stdout)
    assert SECRET_VALUE not in result.stdout
    assert SECRET_VALUE not in result.stderr
    assert SECRET_VALUE not in json.dumps(payload)
    assert SECRET_VALUE not in payload["argv"]
    assert SECRET_VALUE not in payload["env_values"]
    assert payload["argv"] == ["credential-1"]


def test_unknown_credential_rejected_without_leaking_secret(tmp_path):
    boundary, broker, _, _ = _broker(tmp_path)
    request = _effect_request()

    boundary.propose(request)
    boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))

    with pytest.raises(CredentialBrokerNotFoundError) as excinfo:
        broker.release(request, CredentialRef("missing-credential", provider="synthetic", kind="api_key"))

    assert SECRET_VALUE not in str(excinfo.value)
    with pytest.raises(CredentialBrokerNotFoundError):
        broker.release(request, CredentialRef("credential-1", provider="other", kind="api_key"))
    with pytest.raises(CredentialBrokerNotFoundError):
        broker.release(request, CredentialRef("credential-1", provider="synthetic", kind="other"))


@pytest.mark.parametrize(
    "request_kwargs, governed_component, grant_kwargs, expected",
    [
        (
            {"domain_id": "domain-a", "agent_id": "agent-b"},
            None,
            None,
            "not registered|not found",
        ),
        (
            {"domain_id": "domain-b", "agent_id": "agent-b"},
            None,
            {"domain_id": "domain-a", "grantor_identity": "grantor-a", "grantee_identity": "agent-a"},
            "requested ControlDomain",
        ),
        (
            {"domain_id": "domain-a", "proposal": _proposal(action=PowerAction.DISPLAY_ON)},
            None,
            None,
            "scope",
        ),
    ],
)
def test_authority_and_scope_mismatches_fail_closed(
    tmp_path,
    request_kwargs,
    governed_component,
    grant_kwargs,
    expected,
):
    boundary, broker, _, _ = _broker(
        tmp_path,
        governed_component=governed_component,
        grant_kwargs=grant_kwargs,
    )
    request = _effect_request(**request_kwargs)

    with pytest.raises(EffectAuthorityError, match=expected):
        boundary.propose(request)

    with pytest.raises(EffectAuthorityError, match=expected):
        boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))

    with pytest.raises(EffectAuthorityError, match=expected):
        boundary.execute(request)

    with pytest.raises(EffectAuthorityError, match=expected):
        boundary.inspect(request)

    with pytest.raises(CredentialBrokerAuthorityError):
        broker.release(request, CredentialRef("credential-1", provider="synthetic", kind="api_key"))


def test_missing_capability_refuses_without_releasing_secret(tmp_path):
    boundary, broker, _, _ = _broker(
        tmp_path,
        governed_component=_component(actions=(PowerAction.DISPLAY_ON,)),
    )
    request = _effect_request()

    decision = boundary.propose(request)

    assert decision.snapshot.status is PowerStatus.REFUSED
    assert "component capability is unsupported" in decision.snapshot.refusal_reason

    with pytest.raises(PowerRefusalError, match="valid approval is required"):
        boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))

    with pytest.raises(CredentialBrokerAuthorityError, match="execution authorized"):
        broker.release(request, CredentialRef("credential-1", provider="synthetic", kind="api_key"))


def test_revoked_delegation_fails_safe_at_release_time(tmp_path):
    boundary, broker, _, grant_registry = _broker(tmp_path)
    request = _effect_request()

    boundary.propose(request)
    boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))
    grant_registry.revoke(
        "grant-1",
        domain_id="domain-a",
        mission_id="mission-a",
        reason="revoked for test",
    )

    with pytest.raises(CredentialBrokerAuthorityError, match="not active|revoked|not registered"):
        broker.release(request, CredentialRef("credential-1", provider="synthetic"))


def test_credential_existence_does_not_authorize_effect(tmp_path):
    boundary, broker, _, _ = _broker(tmp_path)
    request = _effect_request()

    boundary.propose(request)

    with pytest.raises(CredentialBrokerAuthorityError, match="execution authorized"):
        broker.release(request, CredentialRef("credential-1", provider="synthetic"))


def test_plaintext_secret_is_not_persisted_across_restart(tmp_path):
    boundary, broker, _, _ = _broker(tmp_path)
    request = _effect_request()

    boundary.propose(request)
    boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))
    lease = broker.release(request, CredentialRef("credential-1", provider="synthetic", kind="api_key"))

    assert all("credential" not in path.name for path in tmp_path.iterdir())
    assert SECRET_VALUE not in json.dumps(lease.to_dict())

    restarted = CredentialBroker(
        effect_boundary=boundary,
        secret_source=SecretStore({}),
    )

    with pytest.raises(CredentialBrokerNotFoundError):
        restarted.release(request, CredentialRef("credential-1", provider="synthetic", kind="api_key"))
