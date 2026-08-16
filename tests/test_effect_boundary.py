"""Adversarial tests for the generic effect boundary facade."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import pytest

from federation import (
    AgentIdentity,
    ComponentKind,
    DelegationGrant,
    EffectAuthorityError,
    EffectBoundary,
    EffectRequest,
    EffectRequestError,
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
    RecordingPowerAdapter,
)
from federation.control_domain import ControlDomain
from federation.control_domain_registry import DurableControlDomainRegistry
from federation.agent_identity_registry import DurableAgentIdentityRegistry
from federation.delegation_grant import DelegationGrantStatus
from federation.delegation_grant_registry import DelegationGrantRegistry
from federation.worker_liveness import LivenessState


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
KEY = b"effect-boundary-v0.1-test-integrity-key-0001"


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


def _component(
    *,
    kind=ComponentKind.DISPLAY,
    component_id="display-1",
    actions=(PowerAction.DISPLAY_OFF, PowerAction.DISPLAY_ON),
    auto_display=True,
    interactive_prohibited=False,
    coordinator_id=None,
):
    return GovernedPowerComponent(
        worker_id="worker-1",
        component_id=component_id,
        kind=kind,
        supported_actions=actions,
        policy=PowerPolicy(
            version="policy-v1",
            automatic_display_control=auto_display,
            interactive_session_prohibited=interactive_prohibited,
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
    checkpoint=False,
    protected_work_safe=True,
    wake_path=False,
    wake_coordinator_id=None,
    execute_after=None,
    expires_at=None,
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
        checkpoint_evidence="checkpoint-1" if checkpoint else None,
        protected_work_safe=protected_work_safe,
        wake_path_evidence="verified-wake-path" if wake_path else None,
        wake_coordinator_id=wake_coordinator_id,
        execute_after=execute_after,
        expires_at=expires_at,
        integrity_key=KEY,
    )


def _make_coordinator(
    tmp_path,
    *,
    governed_component=None,
    state=LivenessState.ONLINE,
    capabilities=("display_control",),
    adapter=None,
    key=KEY,
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
    adapter = adapter or RecordingPowerAdapter("recording-1")
    coordinator = PowerCoordinator(
        tmp_path / "power.evidence",
        node_registry=nodes,
        heartbeat_registry=LivenessRegistry(Liveness(state, capabilities)),
        controller_authority="controller-1",
        integrity_authority="integrity-key-1",
        integrity_key=key,
        execution_authorization_authority=PowerExecutionAuthorizationAuthority(key),
        adapters={adapter.adapter_id: adapter},
        clock=clock or MutableClock(),
    )
    coordinator.register_component(governed_component or _component())
    return coordinator, adapter


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


def _boundary(
    tmp_path,
    *,
    state=LivenessState.ONLINE,
    capabilities=("display_control",),
    governed_component=None,
    grant_kwargs=None,
):
    domain_registry, identity_registry, grant_registry = _registries(tmp_path)
    grant_registry.register(_grant(**(grant_kwargs or {})))
    coordinator, adapter = _make_coordinator(
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
    return boundary, coordinator, adapter


def _request(
    *,
    domain_id="domain-a",
    mission_id="mission-a",
    agent_id="agent-a",
    grant_id="grant-1",
    proposal=None,
):
    proposal = _proposal() if proposal is None else proposal
    return EffectRequest.create(
        domain_id=domain_id,
        mission_id=mission_id,
        agent_id=agent_id,
        grant_id=grant_id,
        proposal=proposal,
        integrity_key=KEY,
    )


def test_request_round_trip_is_authenticated_and_repeatable(tmp_path):
    request = _request()
    reloaded = EffectRequest.from_record(request.to_dict(), integrity_key=KEY)

    assert reloaded == request
    assert reloaded.effect_id == request.effect_id

    tampered = request.to_dict()
    tampered["agent_id"] = "agent-b"

    with pytest.raises(EffectRequestError, match="authentication failed"):
        EffectRequest.from_record(tampered, integrity_key=KEY)


def test_effect_boundary_request_authorize_execute_and_inspect(tmp_path):
    boundary, coordinator, adapter = _boundary(tmp_path)
    request = _request()

    decision = boundary.propose(request)
    attempt = boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))
    outcome = boundary.execute(request)
    evidence = boundary.inspect(request)

    assert decision.request == request
    assert decision.domain.domain_id == "domain-a"
    assert decision.identity.agent_id == "agent-a"
    assert decision.grant.grant_id == "grant-1"
    assert decision.snapshot.status is PowerStatus.AUTO_APPROVED

    assert attempt.request == request
    assert attempt.authorization_expiration == NOW + timedelta(minutes=5)
    assert attempt.snapshot.status is PowerStatus.EXECUTION_AUTHORIZED

    assert outcome.request == request
    assert outcome.snapshot.status is PowerStatus.SUCCEEDED
    assert adapter.attempts == ((PowerAction.DISPLAY_OFF, "worker-1", "display-1"),)

    assert evidence.request == request
    assert evidence.outcome.snapshot.status is PowerStatus.SUCCEEDED
    assert evidence.attempt is not None
    assert evidence.evidence_fingerprint
    assert [event.event_type for event in evidence.audit_history] == [
        "proposed",
        "policy_evaluated",
        "auto_approved",
        "execution_authorized",
        "executing",
        "succeeded",
    ]
    assert evidence == boundary.inspect(request)


def test_local_refusal_remains_authoritative_after_authorization(tmp_path):
    boundary, coordinator, _ = _boundary(tmp_path)
    request = _request()

    boundary.propose(request)
    boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))
    coordinator.heartbeat_registry.lease.state = LivenessState.OFFLINE

    outcome = boundary.execute(request)
    evidence = boundary.inspect(request)

    assert outcome.snapshot.status is PowerStatus.FAILED
    assert outcome.snapshot.latest_result == "local_refusal"
    assert "local worker state" in outcome.snapshot.refusal_reason
    assert evidence.outcome.snapshot.status is PowerStatus.FAILED
    assert any(event.event_type == "failed" for event in evidence.audit_history)


@pytest.mark.parametrize(
    "request_domain, request_agent, grant_kwargs, expected",
    [
        ("domain-b", "agent-a", {"domain_id": "domain-a"}, "not found|not registered"),
        ("domain-b", "agent-b", {"domain_id": "domain-a"}, "not registered"),
        ("domain-a", "agent-a", {"domain_id": "domain-a", "authority_scope": ("display_on",)}, "scope does not cover"),
    ],
)
def test_cross_domain_or_scope_mismatch_fails_closed(
    tmp_path,
    request_domain,
    request_agent,
    grant_kwargs,
    expected,
):
    boundary, _, _ = _boundary(tmp_path, grant_kwargs=grant_kwargs)
    request = _request(domain_id=request_domain, agent_id=request_agent)

    with pytest.raises(EffectAuthorityError, match=expected):
        boundary.propose(request)


def test_valid_request_cannot_target_worker_in_other_domain(tmp_path):
    boundary, _, _ = _boundary(
        tmp_path,
        grant_kwargs={
            "domain_id": "domain-b",
            "grantor_identity": "grantor-b",
            "grantee_identity": "agent-b",
        },
    )
    request = _request(domain_id="domain-b", agent_id="agent-b")

    with pytest.raises(
        EffectAuthorityError,
        match="requested ControlDomain",
    ):
        boundary.propose(request)

    with pytest.raises(
        EffectAuthorityError,
        match="requested ControlDomain",
    ):
        boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))

    with pytest.raises(
        EffectAuthorityError,
        match="requested ControlDomain",
    ):
        boundary.execute(request)


def test_revoked_grant_fails_closed(tmp_path):
    domain_registry, identity_registry, grant_registry = _registries(tmp_path)
    grant_registry.register(_grant())
    grant_registry.revoke(
        "grant-1",
        domain_id="domain-a",
        mission_id="mission-a",
        reason="revoked for test",
    )
    coordinator, _ = _make_coordinator(tmp_path)
    boundary = EffectBoundary(
        coordinator=coordinator,
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        grant_registry=grant_registry,
        integrity_key=KEY,
    )

    with pytest.raises(EffectAuthorityError, match="not active"):
        boundary.propose(_request())


def test_concurrent_grant_revocation_blocks_until_execute_completes(
    tmp_path,
    monkeypatch,
):
    domain_registry, identity_registry, grant_registry = _registries(tmp_path)
    grant_registry.register(_grant())
    coordinator, _ = _make_coordinator(tmp_path)
    boundary = EffectBoundary(
        coordinator=coordinator,
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        grant_registry=grant_registry,
        integrity_key=KEY,
    )
    request = _request()

    boundary.propose(request)
    boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))

    started = threading.Event()
    finished = threading.Event()
    state = {}
    original_execute = coordinator.execute

    def racing_execute(proposal_id):
        def revoke():
            started.set()
            grant_registry.revoke(
                "grant-1",
                domain_id="domain-a",
                mission_id="mission-a",
                reason="revoked during execute",
            )
            finished.set()

        worker = threading.Thread(target=revoke, daemon=True)
        state["worker"] = worker
        worker.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.1)
        return original_execute(proposal_id)

    monkeypatch.setattr(coordinator, "execute", racing_execute)

    outcome = boundary.execute(request)

    assert outcome.snapshot.status is PowerStatus.SUCCEEDED
    assert finished.wait(timeout=1)
    state["worker"].join(timeout=1)
    assert not state["worker"].is_alive()
    revoked = grant_registry.get(
        "grant-1",
        domain_id="domain-a",
        mission_id="mission-a",
    )
    assert revoked.status is DelegationGrantStatus.REVOKED


def test_inspect_is_idempotent(tmp_path):
    boundary, _, _ = _boundary(tmp_path)
    request = _request()

    boundary.propose(request)
    boundary.authorize(request, expires_at=NOW + timedelta(minutes=5))
    boundary.execute(request)

    first = boundary.inspect(request)
    second = boundary.inspect(request)

    assert first == second
    assert first.evidence_fingerprint == second.evidence_fingerprint


def test_request_fails_closed_on_tampered_identity_context(tmp_path):
    boundary, _, _ = _boundary(tmp_path)
    request = _request(domain_id="domain-b", agent_id="agent-a")

    with pytest.raises(EffectAuthorityError, match="not found|not registered"):
        boundary.propose(request)
