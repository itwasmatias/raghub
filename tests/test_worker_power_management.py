"""Adversarial contracts for governed worker power management."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from federation import (
    ApprovalType,
    ComponentKind,
    DisabledFedoraPowerAdapter,
    DisabledWindowsPowerAdapter,
    GovernedPowerComponent,
    LivenessState,
    NodeCapability,
    NodeRecord,
    NodeRegistry,
    PowerAction,
    PowerApproval,
    PowerConflictError,
    PowerCoordinator,
    PowerCorruptionError,
    PowerPolicy,
    PowerProposal,
    PowerRefusalError,
    PowerState,
    PowerStatus,
    RecordingPowerAdapter,
)


KEY = b"k" * 32
WRONG_KEY = b"w" * 32
NOW = datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


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


class ReconciliationAdapter:
    adapter_id = "reconciliation-1"
    enabled = True

    def __init__(self):
        self.attempts = ()

    def attempt(self, action, worker_id, component_id):
        assert isinstance(action, PowerAction)
        self.attempts = (*self.attempts, (action, worker_id, component_id))
        return "reconciliation_required"


def component(
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


def proposal(
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


def make_coordinator(
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
        adapters={adapter.adapter_id: adapter},
        clock=clock or MutableClock(),
    )
    coordinator.register_component(governed_component or component())
    return coordinator, adapter


def approval(proposal_id, *, at=NOW, expires=None):
    return PowerApproval(
        proposal_id=proposal_id,
        worker_id="worker-1",
        component_id="system-1",
        action=PowerAction.SLEEP,
        approval_type=ApprovalType.EXPLICIT_USER,
        actor="user-1",
        approved_at=at,
        expires_at=expires or at + timedelta(minutes=10),
        checkpoint_evidence="checkpoint-1",
        protected_work_safe=True,
    )


@pytest.mark.parametrize(
    "action",
    [PowerAction.DISPLAY_OFF, PowerAction.DISPLAY_ON],
)
def test_safe_display_actions_auto_approve(tmp_path, action):
    coordinator, _ = make_coordinator(tmp_path)

    snapshot = coordinator.propose(proposal(action))

    assert snapshot.status is PowerStatus.AUTO_APPROVED
    assert snapshot.approval_requirement == "automatic_display_policy"


def test_display_action_is_refused_when_unsupported(tmp_path):
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=component(actions=(PowerAction.DISPLAY_ON,)),
    )

    snapshot = coordinator.propose(proposal(PowerAction.DISPLAY_OFF))

    assert snapshot.status is PowerStatus.REFUSED
    assert "unsupported" in snapshot.refusal_reason


@pytest.mark.parametrize(
    "state",
    [
        LivenessState.OFFLINE,
        LivenessState.STALE,
        LivenessState.INTENTIONALLY_SLEEPING,
        LivenessState.WAKING,
    ],
)
def test_display_action_is_refused_for_non_online_worker(tmp_path, state):
    coordinator, _ = make_coordinator(tmp_path, state=state)

    snapshot = coordinator.propose(proposal())

    assert snapshot.status is PowerStatus.REFUSED
    assert state.value in snapshot.refusal_reason


@pytest.mark.parametrize(
    ("auto_display", "interactive_prohibited"),
    [(False, False), (True, True)],
)
def test_local_policy_can_refuse_automatic_display_control(
    tmp_path,
    auto_display,
    interactive_prohibited,
):
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=component(
            auto_display=auto_display,
            interactive_prohibited=interactive_prohibited,
        ),
    )

    assert coordinator.propose(proposal()).status is PowerStatus.REFUSED


@pytest.mark.parametrize(
    "unexpected",
    [
        {"checkpoint": True},
        {"wake_path": True},
        {"wake_coordinator_id": "wake-1"},
        {"execute_after": NOW + timedelta(minutes=1)},
        {"expires_at": NOW + timedelta(minutes=5)},
        {"protected_work_safe": False},
    ],
)
def test_display_auto_approval_rejects_unrelated_parameters(tmp_path, unexpected):
    coordinator, _ = make_coordinator(tmp_path)

    snapshot = coordinator.propose(proposal(**unexpected))

    assert snapshot.status is PowerStatus.REFUSED
    assert "parameters" in snapshot.refusal_reason


def test_exact_duplicate_proposal_is_idempotent(tmp_path):
    coordinator, _ = make_coordinator(tmp_path)
    request = proposal()

    first = coordinator.propose(request)
    before = (tmp_path / "power.evidence").read_bytes()
    second = coordinator.propose(request)

    assert second == first
    assert (tmp_path / "power.evidence").read_bytes() == before


def test_changed_duplicate_proposal_is_rejected(tmp_path):
    coordinator, _ = make_coordinator(tmp_path)
    coordinator.propose(proposal())

    with pytest.raises(PowerConflictError, match="changed content"):
        coordinator.propose(proposal(PowerAction.DISPLAY_ON))


@pytest.mark.parametrize("sequence", [3, 5])
def test_replayed_and_out_of_order_proposals_are_rejected(tmp_path, sequence):
    coordinator, _ = make_coordinator(tmp_path)
    coordinator.propose(proposal())

    with pytest.raises(PowerConflictError, match="sequence"):
        coordinator.propose(proposal(sequence=sequence))


@pytest.mark.parametrize(
    "changed",
    [
        {"worker_id": "worker-2"},
        {"component_id": "display-2"},
        {"controller_authority": "controller-2"},
        {"integrity_authority": "integrity-key-2"},
        {"policy_version": "policy-v2"},
    ],
)
def test_identity_or_authority_substitution_is_rejected(tmp_path, changed):
    coordinator, _ = make_coordinator(tmp_path)
    request = proposal()
    values = request.unsigned_record()
    values.update(changed)
    values["action"] = PowerAction(values["action"])
    values["requested_at"] = request.requested_at
    values["execute_after"] = request.execute_after
    values["expires_at"] = request.expires_at
    substituted = PowerProposal.create(**values, integrity_key=KEY)

    with pytest.raises(PowerRefusalError):
        coordinator.propose(substituted)


@pytest.mark.parametrize(
    "action",
    [PowerAction.SLEEP, PowerAction.HIBERNATE, PowerAction.SHUTDOWN],
)
def test_system_power_actions_require_explicit_approval(tmp_path, action):
    system = component(
        kind=ComponentKind.SYSTEM_POWER,
        component_id="system-1",
        actions=(action,),
        coordinator_id="wake-1",
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=system,
        capabilities=(action.value,),
    )
    request = proposal(
        action,
        component_id="system-1",
        checkpoint=True,
        wake_path=action is PowerAction.SHUTDOWN,
        wake_coordinator_id="wake-1" if action is PowerAction.SHUTDOWN else None,
    )

    snapshot = coordinator.propose(request)

    assert snapshot.status is PowerStatus.AWAITING_APPROVAL
    assert snapshot.approval_requirement == "explicit_user"


@pytest.mark.parametrize(
    "action",
    [PowerAction.SLEEP, PowerAction.HIBERNATE, PowerAction.SHUTDOWN],
)
def test_system_power_actions_without_checkpoint_are_refused(tmp_path, action):
    system = component(
        kind=ComponentKind.SYSTEM_POWER,
        component_id="system-1",
        actions=(action,),
        coordinator_id="wake-1",
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=system,
        capabilities=(action.value,),
    )

    snapshot = coordinator.propose(
        proposal(
            action,
            component_id="system-1",
            wake_path=True,
            wake_coordinator_id="wake-1",
        )
    )

    assert snapshot.status is PowerStatus.REFUSED
    assert "checkpoint" in snapshot.refusal_reason


def test_protected_work_blocks_power_transition(tmp_path):
    system = component(
        kind=ComponentKind.SYSTEM_POWER,
        component_id="system-1",
        actions=(PowerAction.SLEEP,),
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=system,
        capabilities=("sleep",),
    )

    snapshot = coordinator.propose(
        proposal(
            PowerAction.SLEEP,
            component_id="system-1",
            checkpoint=True,
            protected_work_safe=False,
        )
    )

    assert snapshot.status is PowerStatus.REFUSED
    assert "protected work" in snapshot.refusal_reason


def test_shutdown_without_verified_wake_path_is_refused(tmp_path):
    system = component(
        kind=ComponentKind.SYSTEM_POWER,
        component_id="system-1",
        actions=(PowerAction.SHUTDOWN,),
        coordinator_id="wake-1",
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=system,
        capabilities=("shutdown",),
    )

    snapshot = coordinator.propose(
        proposal(
            PowerAction.SHUTDOWN,
            component_id="system-1",
            checkpoint=True,
        )
    )

    assert snapshot.status is PowerStatus.REFUSED
    assert "wake path" in snapshot.refusal_reason


@pytest.mark.parametrize(
    ("capabilities", "coordinator_id"),
    [(("wake_on_lan",), None), ((), "wake-1")],
)
def test_wake_requires_advertised_capability_and_authorized_coordinator(
    tmp_path,
    capabilities,
    coordinator_id,
):
    wake = component(
        kind=ComponentKind.WAKE_COORDINATOR,
        component_id="wake-component-1",
        actions=(PowerAction.WAKE_ON_LAN,),
        coordinator_id="wake-1",
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=wake,
        state=LivenessState.OFFLINE,
        capabilities=capabilities,
    )

    snapshot = coordinator.propose(
        proposal(
            PowerAction.WAKE_ON_LAN,
            component_id="wake-component-1",
            wake_coordinator_id=coordinator_id,
            expires_at=NOW + timedelta(minutes=5),
        )
    )

    assert snapshot.status is PowerStatus.REFUSED


def test_wake_request_is_bounded_and_requires_explicit_approval(tmp_path):
    wake = component(
        kind=ComponentKind.WAKE_COORDINATOR,
        component_id="wake-component-1",
        actions=(PowerAction.WAKE_ON_LAN, PowerAction.SCHEDULED_WAKE),
        coordinator_id="wake-1",
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=wake,
        state=LivenessState.OFFLINE,
        capabilities=("wake_on_lan", "scheduled_wake"),
    )

    snapshot = coordinator.propose(
        proposal(
            PowerAction.SCHEDULED_WAKE,
            component_id="wake-component-1",
            wake_coordinator_id="wake-1",
            execute_after=NOW + timedelta(minutes=2),
            expires_at=NOW + timedelta(minutes=5),
        )
    )

    assert snapshot.status is PowerStatus.AWAITING_APPROVAL


def test_proposal_is_not_approval_and_expired_approval_cannot_authorize(tmp_path):
    system = component(
        kind=ComponentKind.SYSTEM_POWER,
        component_id="system-1",
        actions=(PowerAction.SLEEP,),
    )
    clock = MutableClock()
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=system,
        capabilities=("sleep",),
        clock=clock,
    )
    request = proposal(
        PowerAction.SLEEP,
        component_id="system-1",
        checkpoint=True,
    )
    snapshot = coordinator.propose(request)

    with pytest.raises(PowerRefusalError, match="approval"):
        coordinator.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=2))

    expired = approval(snapshot.proposal_id, expires=NOW + timedelta(seconds=1))
    coordinator.approve(expired)
    clock.value = NOW + timedelta(seconds=2)
    with pytest.raises(PowerRefusalError, match="expired"):
        coordinator.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=2))


def test_approval_cannot_bypass_local_refusal(tmp_path):
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=component(actions=(PowerAction.DISPLAY_ON,)),
    )
    snapshot = coordinator.propose(proposal())

    with pytest.raises(PowerRefusalError, match="refused"):
        coordinator.approve(
            PowerApproval(
                proposal_id=snapshot.proposal_id,
                worker_id="worker-1",
                component_id="display-1",
                action=PowerAction.DISPLAY_OFF,
                approval_type=ApprovalType.EXPLICIT_USER,
                actor="user-1",
                approved_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
        )


def test_local_final_refusal_is_durable_and_auditable(tmp_path):
    system = component(
        kind=ComponentKind.SYSTEM_POWER,
        component_id="system-1",
        actions=(PowerAction.SLEEP,),
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=system,
        capabilities=("sleep",),
    )
    snapshot = coordinator.propose(
        proposal(PowerAction.SLEEP, component_id="system-1", checkpoint=True)
    )
    coordinator.approve(approval(snapshot.proposal_id))
    coordinator.heartbeat_registry.lease.state = LivenessState.OFFLINE

    refused = coordinator.authorize(
        snapshot.proposal_id,
        expires_at=NOW + timedelta(minutes=2),
    )

    assert refused.status is PowerStatus.REFUSED
    assert "local worker state" in refused.refusal_reason
    restarted, _ = make_coordinator(
        tmp_path,
        governed_component=system,
        state=LivenessState.OFFLINE,
        capabilities=("sleep",),
    )
    assert restarted.inspect(snapshot.proposal_id) == refused


def test_correct_key_restart_restores_state_and_wrong_key_fails_closed(tmp_path):
    coordinator, adapter = make_coordinator(tmp_path)
    request = proposal()
    expected = coordinator.propose(request)

    restarted, _ = make_coordinator(tmp_path, adapter=adapter)
    assert restarted.inspect(expected.proposal_id) == expected

    with pytest.raises(PowerCorruptionError, match="authentication"):
        make_coordinator(tmp_path, adapter=adapter, key=WRONG_KEY)


@pytest.mark.parametrize("attack", ["alter", "ordinary_sha256", "truncate", "reorder"])
def test_corrupt_evidence_is_rejected_without_modification(tmp_path, attack):
    coordinator, adapter = make_coordinator(tmp_path)
    first = coordinator.propose(proposal())
    coordinator.authorize(first.proposal_id, expires_at=NOW + timedelta(minutes=5))
    path = tmp_path / "power.evidence"
    lines = path.read_bytes().splitlines(keepends=True)
    if attack == "alter":
        lines[-1] = lines[-1].replace(b"execution_authorized", b"execution_authorizeX")
    elif attack == "ordinary_sha256":
        record = json.loads(lines[-1])
        record["authentication_tag"] = hashlib.sha256(
            json.dumps(record, sort_keys=True).encode()
        ).hexdigest()
        lines[-1] = (
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
    elif attack == "truncate":
        lines[-1] = lines[-1][:-1]
    else:
        lines[-2], lines[-1] = lines[-1], lines[-2]
    attacked = b"".join(lines)
    path.write_bytes(attacked)

    with pytest.raises(PowerCorruptionError):
        PowerCoordinator(
            path,
            node_registry=coordinator.node_registry,
            heartbeat_registry=coordinator.heartbeat_registry,
            controller_authority="controller-1",
            integrity_authority="integrity-key-1",
            integrity_key=KEY,
            adapters={adapter.adapter_id: adapter},
            clock=MutableClock(),
        )

    assert path.read_bytes() == attacked


def test_read_only_inspection_creates_nothing(tmp_path):
    path = tmp_path / "missing" / "power.evidence"
    nodes = NodeRegistry()

    assert PowerCoordinator.inspect_store(
        path,
        integrity_key=KEY,
        controller_authority="controller-1",
        integrity_authority="integrity-key-1",
    ) == ()
    assert not path.parent.exists()


def test_concurrent_approval_attempts_produce_one_result(tmp_path):
    system = component(
        kind=ComponentKind.SYSTEM_POWER,
        component_id="system-1",
        actions=(PowerAction.SLEEP,),
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=system,
        capabilities=("sleep",),
    )
    snapshot = coordinator.propose(
        proposal(PowerAction.SLEEP, component_id="system-1", checkpoint=True)
    )
    evidence = approval(snapshot.proposal_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: coordinator.approve(evidence), range(8)))

    assert {result.status for result in results} == {PowerStatus.APPROVED}
    assert [e.event_type for e in coordinator.audit_history()].count("approved") == 1


def test_concurrent_terminal_results_have_first_terminal_wins(tmp_path):
    coordinator, adapter = make_coordinator(tmp_path)
    snapshot = coordinator.propose(proposal())
    coordinator.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=5))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: coordinator.execute(snapshot.proposal_id), range(8)))

    assert {result.status for result in results} == {PowerStatus.SUCCEEDED}
    assert adapter.attempts == ((PowerAction.DISPLAY_OFF, "worker-1", "display-1"),)
    assert [e.event_type for e in coordinator.audit_history()].count("succeeded") == 1


def test_authorized_proposal_can_be_cancelled_idempotently(tmp_path):
    coordinator, _ = make_coordinator(tmp_path)
    snapshot = coordinator.propose(proposal())
    coordinator.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=5))

    first = coordinator.cancel(
        snapshot.proposal_id,
        actor="user-1",
        reason="User kept the display active",
    )
    before = (tmp_path / "power.evidence").read_bytes()
    second = coordinator.cancel(
        snapshot.proposal_id,
        actor="user-1",
        reason="User kept the display active",
    )

    assert first.status is PowerStatus.CANCELLED
    assert second == first
    assert (tmp_path / "power.evidence").read_bytes() == before


def test_uncertain_adapter_result_requires_reconciliation(tmp_path):
    adapter = ReconciliationAdapter()
    governed = replace(component(), adapter_id=adapter.adapter_id)
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=governed,
        adapter=adapter,
    )
    snapshot = coordinator.propose(proposal())
    coordinator.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=5))

    uncertain = coordinator.execute(snapshot.proposal_id)
    resolved = coordinator.reconcile(
        snapshot.proposal_id,
        succeeded=True,
        reason="Simulated state matches requested display state",
    )

    assert uncertain.status is PowerStatus.RECONCILIATION_REQUIRED
    assert resolved.status is PowerStatus.SUCCEEDED
    assert coordinator.reconcile(
        snapshot.proposal_id,
        succeeded=True,
        reason="Simulated state matches requested display state",
    ) == resolved


def test_execution_time_local_refusal_is_audited_as_failure(tmp_path):
    coordinator, _ = make_coordinator(tmp_path)
    snapshot = coordinator.propose(proposal())
    coordinator.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=5))
    coordinator.heartbeat_registry.lease.state = LivenessState.OFFLINE

    failed = coordinator.execute(snapshot.proposal_id)

    assert failed.status is PowerStatus.FAILED
    assert failed.latest_result == "local_refusal"
    assert "local worker state" in failed.refusal_reason


def test_conflicting_display_actions_serialize_deterministically(tmp_path):
    coordinator, _ = make_coordinator(tmp_path)
    first = coordinator.propose(proposal())

    second = coordinator.propose(proposal(PowerAction.DISPLAY_ON, sequence=2))

    assert first.status is PowerStatus.AUTO_APPROVED
    assert second.status is PowerStatus.REFUSED
    assert "transition" in second.refusal_reason


def test_repeated_execution_authorization_is_idempotent(tmp_path):
    coordinator, _ = make_coordinator(tmp_path)
    snapshot = coordinator.propose(proposal())

    first = coordinator.authorize(
        snapshot.proposal_id,
        expires_at=NOW + timedelta(minutes=5),
    )
    before = (tmp_path / "power.evidence").read_bytes()
    second = coordinator.authorize(
        snapshot.proposal_id,
        expires_at=NOW + timedelta(minutes=5),
    )

    assert second == first
    assert (tmp_path / "power.evidence").read_bytes() == before


def test_adapter_receives_only_typed_actions_and_no_command_surface(tmp_path):
    coordinator, adapter = make_coordinator(tmp_path)
    snapshot = coordinator.propose(proposal())
    coordinator.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=5))
    coordinator.execute(snapshot.proposal_id)

    assert adapter.attempts == ((PowerAction.DISPLAY_OFF, "worker-1", "display-1"),)
    with pytest.raises((TypeError, ValueError)):
        PowerProposal.create(
            worker_id="worker-1",
            component_id="display-1",
            action="rm -rf /",
            sequence=2,
            controller_authority="controller-1",
            integrity_authority="integrity-key-1",
            policy_version="policy-v1",
            requested_at=NOW,
            integrity_key=KEY,
        )
    assert not hasattr(PowerProposal, "command")


@pytest.mark.parametrize(
    "adapter_type",
    [DisabledFedoraPowerAdapter, DisabledWindowsPowerAdapter],
)
def test_production_adapters_are_disabled_by_default(adapter_type):
    adapter = adapter_type("production-disabled")

    assert adapter.enabled is False
    with pytest.raises(PowerRefusalError, match="disabled"):
        adapter.attempt(PowerAction.DISPLAY_OFF, "worker-1", "display-1")


def test_shutdown_wake_loop_prevention_and_wake_replay_protection(tmp_path):
    wake = component(
        kind=ComponentKind.WAKE_COORDINATOR,
        component_id="wake-component-1",
        actions=(PowerAction.WAKE_ON_LAN,),
        coordinator_id="wake-1",
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=wake,
        state=LivenessState.OFFLINE,
        capabilities=("wake_on_lan",),
    )
    request = proposal(
        PowerAction.WAKE_ON_LAN,
        component_id="wake-component-1",
        wake_coordinator_id="wake-1",
        expires_at=NOW + timedelta(minutes=5),
    )

    first = coordinator.propose(request)
    assert coordinator.propose(request) == first
    with pytest.raises(PowerConflictError):
        coordinator.propose(
            proposal(
                PowerAction.WAKE_ON_LAN,
                component_id="wake-component-1",
                wake_coordinator_id="wake-1",
                expires_at=NOW + timedelta(minutes=6),
            )
        )


def test_wake_loop_prevention_is_rechecked_before_authorization(tmp_path):
    wake = component(
        kind=ComponentKind.WAKE_COORDINATOR,
        component_id="wake-component-1",
        actions=(PowerAction.WAKE_ON_LAN,),
        coordinator_id="wake-1",
    )
    coordinator, _ = make_coordinator(
        tmp_path,
        governed_component=wake,
        state=LivenessState.OFFLINE,
        capabilities=("wake_on_lan",),
    )
    request = proposal(
        PowerAction.WAKE_ON_LAN,
        component_id="wake-component-1",
        wake_coordinator_id="wake-1",
        expires_at=NOW + timedelta(minutes=5),
    )
    snapshot = coordinator.propose(request)
    coordinator.approve(
        PowerApproval(
            proposal_id=snapshot.proposal_id,
            worker_id="worker-1",
            component_id="wake-component-1",
            action=PowerAction.WAKE_ON_LAN,
            approval_type=ApprovalType.EXPLICIT_USER,
            actor="user-1",
            approved_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
    )
    coordinator.heartbeat_registry.lease.state = LivenessState.ONLINE

    refused = coordinator.authorize(
        snapshot.proposal_id,
        expires_at=NOW + timedelta(minutes=2),
    )

    assert refused.status is PowerStatus.REFUSED
    assert "wake loop" in refused.refusal_reason


def test_component_and_records_are_immutable_and_operations_view_is_complete(tmp_path):
    governed = component()
    with pytest.raises(FrozenInstanceError):
        governed.component_id = "changed"
    coordinator, _ = make_coordinator(tmp_path, governed_component=governed)
    snapshot = coordinator.propose(proposal())

    view = coordinator.operations_view("worker-1", "display-1")

    assert view.worker_id == "worker-1"
    assert view.component_id == "display-1"
    assert view.supported_actions == ("display_off", "display_on")
    assert view.current_governed_state == PowerStatus.AUTO_APPROVED.value
    assert view.pending_proposal == snapshot.proposal_id
    assert view.audit_sequence >= 3


def test_display_control_does_not_mutate_federation_evidence(tmp_path):
    coordinator, _ = make_coordinator(tmp_path)
    node = coordinator.node_registry.get("worker-1")
    before = (
        node.status,
        node.last_seen,
        frozenset(node.capabilities),
        coordinator.heartbeat_registry.lease,
    )

    coordinator.propose(proposal())

    after = (
        node.status,
        node.last_seen,
        frozenset(node.capabilities),
        coordinator.heartbeat_registry.lease,
    )
    assert after == before


@pytest.mark.parametrize("bad_key", [None, "not-bytes", b"short"])
def test_integrity_key_has_no_default_or_fallback(tmp_path, bad_key):
    with pytest.raises((TypeError, ValueError)):
        PowerCoordinator.inspect_store(
            tmp_path / "power.evidence",
            integrity_key=bad_key,
            controller_authority="controller-1",
            integrity_authority="integrity-key-1",
        )


def test_proposal_requires_valid_controller_authentication(tmp_path):
    coordinator, _ = make_coordinator(tmp_path)
    request = proposal()

    with pytest.raises(PowerRefusalError, match="authentication"):
        coordinator.propose(replace(request, authentication_tag="0" * 64))
