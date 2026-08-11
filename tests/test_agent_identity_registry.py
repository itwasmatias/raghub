"""Adversarial tests for the durable agent identity registry."""

from __future__ import annotations

import json
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation.agent_identity import AgentIdentity, AgentIdentityLifecycle
from federation.agent_identity_registry import (
    AgentIdentityConflictError,
    AgentIdentityCorruptionError,
    AgentIdentityDomainError,
    AgentIdentityLifecycleError,
    AgentIdentityNotFoundError,
    DurableAgentIdentityRegistry,
)
from federation.control_domain import ControlDomain, DomainLifecycle
from federation.control_domain_registry import DurableControlDomainRegistry


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
KEY = b"agent-identity-v0.1-test-integrity-key-0001"


class MutableClock:
    def __init__(self, current=NOW):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, delta):
        self.current += delta


@pytest.fixture
def domain_registry_path(tmp_path):
    return tmp_path / "control-domains.jsonl"


@pytest.fixture
def identity_registry_path(tmp_path):
    return tmp_path / "agent-identities.jsonl"


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
    return DurableAgentIdentityRegistry(
        identity_registry_path,
        domain_registry=control_domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )


def _agent(domain_id: str = "domain-a", *, agent_id: str = "agent-1", name: str = "Agent One"):
    return AgentIdentity(
        agent_id=agent_id,
        domain_id=domain_id,
        name=name,
        created_at=NOW,
    )


def test_register_and_get_identity(identity_registry, identity_registry_path):
    state = identity_registry.register(_agent())

    assert state.agent_id == "agent-1"
    assert state.domain_id == "domain-a"
    assert state.name == "Agent One"
    assert state.lifecycle is AgentIdentityLifecycle.ACTIVE
    assert len(state.domain_fingerprint) == 64
    assert len(state.identity_fingerprint) == 64
    assert state.last_transition_at == NOW

    retrieved = identity_registry.get("agent-1", domain_id="domain-a")
    assert retrieved == state
    assert identity_registry_path.read_text().endswith("\n")


def test_list_identities_is_sorted_and_domain_filterable(identity_registry):
    identity_registry.register(_agent(domain_id="domain-b", agent_id="agent-2", name="Agent Two"))
    identity_registry.register(_agent(domain_id="domain-a", agent_id="agent-1", name="Agent One"))

    all_identities = identity_registry.list_identities()
    assert [item.domain_id for item in all_identities] == ["domain-a", "domain-b"]

    domain_a = identity_registry.list_identities(domain_id="domain-a")
    assert [item.agent_id for item in domain_a] == ["agent-1"]

    active_only = identity_registry.list_identities(status_filter=AgentIdentityLifecycle.ACTIVE)
    assert len(active_only) == 2


def test_exact_duplicate_registration_is_idempotent(identity_registry):
    identity = _agent()

    first = identity_registry.register(identity)
    second = identity_registry.register(identity)

    assert first == second
    assert len(identity_registry.list_identities()) == 1


def test_conflicting_duplicate_registration_fails_closed(identity_registry):
    identity_registry.register(_agent())

    with pytest.raises(AgentIdentityConflictError, match="already exists"):
        identity_registry.register(_agent(name="Different Name"))


def test_missing_domain_rejected(identity_registry):
    with pytest.raises(AgentIdentityDomainError, match="not registered"):
        identity_registry.register(_agent(domain_id="missing-domain"))


def test_inactive_domain_rejected(control_domain_registry, identity_registry_path):
    control_domain_registry.update_lifecycle("domain-b", DomainLifecycle.ARCHIVED)
    registry = DurableAgentIdentityRegistry(
        identity_registry_path,
        domain_registry=control_domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )

    with pytest.raises(AgentIdentityDomainError, match="not active"):
        registry.register(_agent(domain_id="domain-b", agent_id="agent-9"))


def test_ambiguous_lookup_requires_domain_context(identity_registry):
    identity_registry.register(_agent(domain_id="domain-a", agent_id="shared-agent", name="Agent A"))
    identity_registry.register(_agent(domain_id="domain-b", agent_id="shared-agent", name="Agent B"))

    with pytest.raises(AgentIdentityConflictError, match="ambiguous"):
        identity_registry.get("shared-agent")

    assert identity_registry.get("shared-agent", domain_id="domain-a").name == "Agent A"
    assert identity_registry.get("shared-agent", domain_id="domain-b").name == "Agent B"


def test_revoke_and_archive_non_widening(identity_registry):
    identity_registry.register(_agent())

    revoked = identity_registry.revoke("agent-1", domain_id="domain-a", reason="compromised")
    assert revoked.lifecycle is AgentIdentityLifecycle.REVOKED
    assert revoked.revoked_at == NOW
    assert revoked.revocation_reason == "compromised"

    archived = identity_registry.archive("agent-1", domain_id="domain-a", reason="retired")
    assert archived.lifecycle is AgentIdentityLifecycle.ARCHIVED
    assert archived.archived_at == NOW
    assert archived.revoked_at == NOW
    assert archived.revocation_reason == "compromised"
    assert archived.archive_reason == "retired"

    with pytest.raises(AgentIdentityLifecycleError, match="non-widening"):
        identity_registry.update_lifecycle(
            "agent-1",
            AgentIdentityLifecycle.ACTIVE,
            domain_id="domain-a",
        )


def test_idempotent_revoke_and_archive_keep_existing_evidence(identity_registry):
    identity_registry.register(_agent())
    revoked = identity_registry.revoke("agent-1", domain_id="domain-a", reason="compromised")
    repeat = identity_registry.revoke("agent-1", domain_id="domain-a", reason="compromised")
    assert repeat == revoked

    archived = identity_registry.archive("agent-1", domain_id="domain-a", reason="retired")
    repeat_archive = identity_registry.archive("agent-1", domain_id="domain-a", reason="retired")
    assert repeat_archive == archived


def test_reactivation_by_ordinary_mutation_is_rejected(identity_registry):
    identity_registry.register(_agent())
    identity_registry.revoke("agent-1", domain_id="domain-a")

    with pytest.raises(AgentIdentityLifecycleError, match="non-widening"):
        identity_registry.update_lifecycle(
            "agent-1",
            AgentIdentityLifecycle.ACTIVE,
            domain_id="domain-a",
        )


def test_registry_reconstructs_deterministically_after_restart(identity_registry_path, control_domain_registry):
    registry = DurableAgentIdentityRegistry(
        identity_registry_path,
        domain_registry=control_domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    registry.register(_agent())
    registry.revoke("agent-1", domain_id="domain-a", reason="compromised")

    restarted = DurableAgentIdentityRegistry(
        identity_registry_path,
        domain_registry=control_domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    assert restarted.get("agent-1", domain_id="domain-a").lifecycle is AgentIdentityLifecycle.REVOKED
    assert restarted.snapshot() == registry.snapshot()


def test_tampered_history_is_rejected(identity_registry, identity_registry_path):
    identity_registry.register(_agent())
    text = identity_registry_path.read_text()
    identity_registry_path.write_text(text.replace("Agent One", "Tampered Agent"), encoding="utf-8")

    with pytest.raises(AgentIdentityCorruptionError):
        identity_registry.snapshot()


def _register_process(root: str, domain_root: str, queue) -> None:
    domain_registry = DurableControlDomainRegistry(
        Path(domain_root),
        integrity_key=KEY,
    )
    registry = DurableAgentIdentityRegistry(
        Path(root),
        domain_registry=domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    try:
        state = registry.register(_agent())
        queue.put(("ok", state.identity_fingerprint))
    except BaseException as exc:  # pragma: no cover - propagated to parent
        queue.put(("error", type(exc).__name__, str(exc)))


def test_cross_process_duplicate_registration_is_idempotent(tmp_path):
    domain_path = tmp_path / "domains.jsonl"
    identity_path = tmp_path / "agents.jsonl"
    domain_registry = DurableControlDomainRegistry(domain_path, integrity_key=KEY)
    domain_registry.register(
        ControlDomain(
            domain_id="domain-a",
            name="Domain A",
            owner="owner-a",
            created_at=NOW,
        )
    )

    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_register_process, args=(str(identity_path), str(domain_path), queue))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)

    assert all(process.exitcode == 0 for process in processes)
    results = [queue.get(timeout=5) for _ in processes]
    assert all(item[0] == "ok" for item in results)
    assert len({item[1] for item in results}) == 1

    registry = DurableAgentIdentityRegistry(
        identity_path,
        domain_registry=domain_registry,
        integrity_key=KEY,
        clock=MutableClock(),
    )
    assert len(registry.list_identities()) == 1

