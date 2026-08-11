"""Generic effect boundary facade layered over the existing power coordinator.

M4 deliberately wraps the current power attempt pipeline instead of creating a
second authority plane. The current implementation uses the power subsystem as
the first concrete adapter for the generic request -> decision -> attempt ->
outcome -> evidence flow.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from federation.agent_identity import AuthoritativeAgentIdentity
from federation.agent_identity_registry import (
    AgentIdentityCorruptionError,
    AgentIdentityDomainError,
    AgentIdentityError,
    AgentIdentityNotFoundError,
    DurableAgentIdentityRegistry,
)
from federation.control_domain_registry import (
    AuthoritativeDomain,
    ControlDomainRegistryError,
    DomainCorruptionError,
    DomainLifecycleError,
    DomainNotFoundError,
    DurableControlDomainRegistry,
)
from federation.delegation_grant import AuthoritativeDelegationGrant, DelegationGrantStatus
from federation.delegation_grant_registry import (
    DelegationGrantCorruptionError,
    DelegationGrantError,
    DelegationGrantIdentityError,
    DelegationGrantLifecycleError,
    DelegationGrantNotFoundError,
    DelegationGrantRegistry,
)
from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.power_action import PowerAction, PowerAuditEvent, PowerProposal, PowerSnapshot, PowerStatus, require_text, timestamp
from federation.power_adapter import PowerRefusalError
from federation.power_coordinator import PowerCoordinator


_SCHEMA_VERSION = 1
_REQUEST_AUTH_DOMAIN = b"raghub.effect-request.v1"


class EffectBoundaryError(Exception):
    """Base error for effect-boundary request handling."""


class EffectRequestError(EffectBoundaryError):
    """Raised when an effect request envelope cannot be trusted."""


class EffectAuthorityError(EffectBoundaryError):
    """Raised when identity, domain, or grant binding fails closed."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EffectRequestError("effect request evidence has duplicate JSON keys")
        result[key] = value
    return result


def _domain_record(domain: AuthoritativeDomain) -> dict[str, Any]:
    return {
        "domain_id": domain.domain_id,
        "domain_fingerprint": domain.domain_fingerprint,
        "name": domain.name,
        "owner": domain.owner,
        "lifecycle": domain.lifecycle.value,
        "created_at": domain.created_at.astimezone(timezone.utc).isoformat(),
        "last_transition_at": domain.last_transition_at.astimezone(timezone.utc).isoformat(),
    }


def _audit_event_record(event: PowerAuditEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "event_type": event.event_type,
        "proposal_id": event.proposal_id,
        "worker_id": event.worker_id,
        "component_id": event.component_id,
        "action": event.action,
        "controller_timestamp": timestamp(event.controller_timestamp),
        "reason": event.reason,
        "authentication_tag": event.authentication_tag,
    }


def _snapshot_record(snapshot: PowerSnapshot) -> dict[str, Any]:
    return {
        "proposal_id": snapshot.proposal_id,
        "worker_id": snapshot.worker_id,
        "component_id": snapshot.component_id,
        "action": snapshot.action.value,
        "status": snapshot.status.value,
        "approval_requirement": snapshot.approval_requirement,
        "refusal_reason": snapshot.refusal_reason,
        "checkpoint_status": snapshot.checkpoint_status,
        "wake_path_status": snapshot.wake_path_status,
        "authorization_expiration": (
            None
            if snapshot.authorization_expiration is None
            else timestamp(snapshot.authorization_expiration)
        ),
        "latest_result": snapshot.latest_result,
        "audit_sequence": snapshot.audit_sequence,
    }


def _require_effect_text(value: Any, field: str) -> str:
    return require_text(value, field)


@dataclass(frozen=True, slots=True)
class EffectRequest:
    """Authenticated request envelope for a bounded effect attempt."""

    effect_request_id: str
    domain_id: str
    mission_id: str
    agent_id: str
    grant_id: str
    proposal: PowerProposal
    authentication_tag: str

    def __post_init__(self) -> None:
        for field_name in ("effect_request_id", "domain_id", "mission_id", "agent_id", "grant_id"):
            object.__setattr__(
                self,
                field_name,
                _require_effect_text(getattr(self, field_name), field_name),
            )
        if type(self.proposal) is not PowerProposal:
            raise TypeError("proposal must be a PowerProposal")
        if not isinstance(self.authentication_tag, str):
            raise TypeError("authentication_tag must be a string")
        if len(self.authentication_tag) != 64:
            raise ValueError("authentication_tag must be a SHA-256 HMAC tag")
        try:
            bytes.fromhex(self.authentication_tag)
        except ValueError as exc:
            raise ValueError("authentication_tag must be hexadecimal") from exc
        expected = self.deterministic_id(self.unsigned_record())
        if self.effect_request_id != expected:
            raise ValueError("effect_request_id does not match request content")

    @property
    def effect_id(self) -> str:
        return self.effect_request_id

    @staticmethod
    def deterministic_id(record: dict[str, Any]) -> str:
        return hashlib.sha256(
            b"raghub.effect-request.v1\0" + _canonical(record),
        ).hexdigest()

    def unsigned_record(self) -> dict[str, Any]:
        return {
            "domain_id": self.domain_id,
            "mission_id": self.mission_id,
            "agent_id": self.agent_id,
            "grant_id": self.grant_id,
            "proposal": self.proposal.record(),
        }

    def authenticated_payload(self) -> dict[str, Any]:
        return self.unsigned_record()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "effect_request_id": self.effect_request_id,
            **self.unsigned_record(),
            "authentication_tag": self.authentication_tag,
        }

    record = to_dict

    @classmethod
    def create(
        cls,
        *,
        domain_id: str,
        mission_id: str,
        agent_id: str,
        grant_id: str,
        proposal: PowerProposal,
        integrity_key: bytes,
    ) -> "EffectRequest":
        if type(proposal) is not PowerProposal:
            raise TypeError("proposal must be a PowerProposal")
        integrity_key = require_integrity_key(integrity_key)
        values = {
            "domain_id": _require_effect_text(domain_id, "domain_id"),
            "mission_id": _require_effect_text(mission_id, "mission_id"),
            "agent_id": _require_effect_text(agent_id, "agent_id"),
            "grant_id": _require_effect_text(grant_id, "grant_id"),
            "proposal": proposal,
        }
        serialized = {**values, "proposal": proposal.record()}
        return cls(
            effect_request_id=cls.deterministic_id(serialized),
            authentication_tag=authentication_tag(
                integrity_key,
                _REQUEST_AUTH_DOMAIN,
                _canonical(serialized),
            ),
            **values,
        )

    @classmethod
    def from_record(
        cls,
        record: dict[str, Any],
        *,
        integrity_key: bytes | None = None,
    ) -> "EffectRequest":
        if not isinstance(record, dict):
            raise EffectRequestError("effect request record must be an object")
        required = {
            "schema_version",
            "effect_request_id",
            "domain_id",
            "mission_id",
            "agent_id",
            "grant_id",
            "proposal",
            "authentication_tag",
        }
        if set(record) != required or record["schema_version"] != _SCHEMA_VERSION:
            raise EffectRequestError("effect request schema is invalid")
        if not isinstance(record["proposal"], dict):
            raise EffectRequestError("effect request proposal is invalid")
        if integrity_key is not None:
            key = require_integrity_key(integrity_key)
            unsigned = {
                "domain_id": record["domain_id"],
                "mission_id": record["mission_id"],
                "agent_id": record["agent_id"],
                "grant_id": record["grant_id"],
                "proposal": record["proposal"],
            }
            if not authenticates(
                key,
                _REQUEST_AUTH_DOMAIN,
                _canonical(unsigned),
                record["authentication_tag"],
            ):
                raise EffectRequestError("effect request authentication failed")
        proposal = PowerProposal.from_record(record["proposal"])
        request = cls(
            effect_request_id=record["effect_request_id"],
            domain_id=record["domain_id"],
            mission_id=record["mission_id"],
            agent_id=record["agent_id"],
            grant_id=record["grant_id"],
            proposal=proposal,
            authentication_tag=record["authentication_tag"],
        )
        return request


@dataclass(frozen=True, slots=True)
class EffectDecision:
    """Outcome of the request-stage domain, identity, and grant binding."""

    request: EffectRequest
    domain: AuthoritativeDomain
    identity: AuthoritativeAgentIdentity
    grant: AuthoritativeDelegationGrant
    snapshot: PowerSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "domain": _domain_record(self.domain),
            "identity": self.identity.to_dict(),
            "grant": self.grant.to_dict(),
            "snapshot": _snapshot_record(self.snapshot),
        }


@dataclass(frozen=True, slots=True)
class EffectAttempt:
    """Authorization granted to attempt a previously decided effect."""

    request: EffectRequest
    decision: EffectDecision
    authorization_expiration: datetime | None
    snapshot: PowerSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "decision": self.decision.to_dict(),
            "authorization_expiration": (
                None
                if self.authorization_expiration is None
                else timestamp(self.authorization_expiration)
            ),
            "snapshot": _snapshot_record(self.snapshot),
        }


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    """Current durable state for an effect attempt."""

    request: EffectRequest
    decision: EffectDecision
    attempt: EffectAttempt | None
    snapshot: PowerSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "decision": self.decision.to_dict(),
            "attempt": None if self.attempt is None else self.attempt.to_dict(),
            "snapshot": _snapshot_record(self.snapshot),
        }


@dataclass(frozen=True, slots=True)
class EffectEvidence:
    """Standardized evidence bundle for a bounded effect attempt."""

    request: EffectRequest
    decision: EffectDecision
    attempt: EffectAttempt | None
    outcome: EffectOutcome
    audit_history: tuple[PowerAuditEvent, ...]
    evidence_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_fingerprint", self._fingerprint())

    def _fingerprint(self) -> str:
        payload = {
            "request": self.request.to_dict(),
            "decision": self.decision.to_dict(),
            "attempt": None if self.attempt is None else self.attempt.to_dict(),
            "outcome": self.outcome.to_dict(),
            "audit_history": [_audit_event_record(item) for item in self.audit_history],
        }
        return hashlib.sha256(_canonical(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "decision": self.decision.to_dict(),
            "attempt": None if self.attempt is None else self.attempt.to_dict(),
            "outcome": self.outcome.to_dict(),
            "audit_history": [_audit_event_record(item) for item in self.audit_history],
            "evidence_fingerprint": self.evidence_fingerprint,
        }


class EffectBoundary:
    """Thin request/decision/attempt facade over the power coordinator."""

    def __init__(
        self,
        *,
        coordinator: PowerCoordinator,
        domain_registry: DurableControlDomainRegistry,
        identity_registry: DurableAgentIdentityRegistry,
        grant_registry: DelegationGrantRegistry,
        integrity_key: bytes,
    ) -> None:
        if type(coordinator) is not PowerCoordinator:
            raise TypeError("coordinator must be a PowerCoordinator")
        if type(domain_registry) is not DurableControlDomainRegistry:
            raise TypeError("domain_registry must be a DurableControlDomainRegistry")
        if type(identity_registry) is not DurableAgentIdentityRegistry:
            raise TypeError("identity_registry must be a DurableAgentIdentityRegistry")
        if type(grant_registry) is not DelegationGrantRegistry:
            raise TypeError("grant_registry must be a DelegationGrantRegistry")
        self.coordinator = coordinator
        self.domain_registry = domain_registry
        self.identity_registry = identity_registry
        self.grant_registry = grant_registry
        self._integrity_key = require_integrity_key(integrity_key)

    def _authenticate_request(self, request: EffectRequest) -> None:
        if type(request) is not EffectRequest:
            raise TypeError("request must be an EffectRequest")
        if not authenticates(
            self._integrity_key,
            _REQUEST_AUTH_DOMAIN,
            _canonical(request.unsigned_record()),
            request.authentication_tag,
        ):
            raise EffectRequestError("effect request authentication failed")

    def _resolve_context(
        self,
        request: EffectRequest,
    ) -> tuple[AuthoritativeDomain, AuthoritativeAgentIdentity, AuthoritativeDelegationGrant]:
        self._authenticate_request(request)
        try:
            domain = self.domain_registry.get(request.domain_id)
        except (ControlDomainRegistryError, DomainNotFoundError, DomainCorruptionError, DomainLifecycleError) as exc:
            raise EffectAuthorityError(str(exc)) from exc
        if domain.lifecycle.value != "active":
            raise EffectAuthorityError(
                f"control domain {request.domain_id!r} is not active",
            )
        try:
            identity = self.identity_registry.get(
                request.agent_id,
                domain_id=request.domain_id,
            )
        except (AgentIdentityError, AgentIdentityNotFoundError, AgentIdentityDomainError, AgentIdentityCorruptionError) as exc:
            raise EffectAuthorityError(str(exc)) from exc
        try:
            grant = self.grant_registry.get(
                request.grant_id,
                domain_id=request.domain_id,
                mission_id=request.mission_id,
            )
        except (DelegationGrantError, DelegationGrantNotFoundError, DelegationGrantIdentityError, DelegationGrantLifecycleError, DelegationGrantCorruptionError) as exc:
            raise EffectAuthorityError(str(exc)) from exc
        if grant.grantee_identity != request.agent_id:
            raise EffectAuthorityError(
                "delegation grant grantee does not match request agent",
            )
        if grant.status is not DelegationGrantStatus.ACTIVE:
            raise EffectAuthorityError("delegation grant is not active")
        if request.proposal.action.value not in grant.authority_scope:
            raise EffectAuthorityError("delegation grant scope does not cover requested effect")
        return domain, identity, grant

    @staticmethod
    def _decision(
        request: EffectRequest,
        domain: AuthoritativeDomain,
        identity: AuthoritativeAgentIdentity,
        grant: AuthoritativeDelegationGrant,
        snapshot: PowerSnapshot,
    ) -> EffectDecision:
        return EffectDecision(
            request=request,
            domain=domain,
            identity=identity,
            grant=grant,
            snapshot=snapshot,
        )

    def _attempt(
        self,
        request: EffectRequest,
        decision: EffectDecision,
        snapshot: PowerSnapshot,
        *,
        include_without_expiration: bool,
    ) -> EffectAttempt | None:
        if snapshot.authorization_expiration is None and not include_without_expiration:
            return None
        return EffectAttempt(
            request=request,
            decision=decision,
            authorization_expiration=snapshot.authorization_expiration,
            snapshot=snapshot,
        )

    def propose(self, request: EffectRequest) -> EffectDecision:
        domain, identity, grant = self._resolve_context(request)
        snapshot = self.coordinator.propose(request.proposal)
        return self._decision(request, domain, identity, grant, snapshot)

    def authorize(self, request: EffectRequest, *, expires_at: datetime) -> EffectAttempt:
        domain, identity, grant = self._resolve_context(request)
        current = self.coordinator.inspect(request.proposal.proposal_id)
        decision = self._decision(request, domain, identity, grant, current)
        snapshot = self.coordinator.authorize(
            request.proposal.proposal_id,
            expires_at=expires_at,
        )
        return self._attempt(
            request,
            decision,
            snapshot,
            include_without_expiration=True,
        )

    def execute(self, request: EffectRequest) -> EffectOutcome:
        domain, identity, grant = self._resolve_context(request)
        current = self.coordinator.inspect(request.proposal.proposal_id)
        decision = self._decision(request, domain, identity, grant, current)
        snapshot = self.coordinator.execute(request.proposal.proposal_id)
        attempt = self._attempt(
            request,
            decision,
            snapshot,
            include_without_expiration=False,
        )
        return EffectOutcome(
            request=request,
            decision=decision,
            attempt=attempt,
            snapshot=snapshot,
        )

    def inspect(self, request: EffectRequest) -> EffectEvidence:
        domain, identity, grant = self._resolve_context(request)
        snapshot = self.coordinator.inspect(request.proposal.proposal_id)
        decision = self._decision(request, domain, identity, grant, snapshot)
        attempt = self._attempt(
            request,
            decision,
            snapshot,
            include_without_expiration=False,
        )
        outcome = EffectOutcome(
            request=request,
            decision=decision,
            attempt=attempt,
            snapshot=snapshot,
        )
        audit_history = tuple(
            event
            for event in self.coordinator.audit_history()
            if event.proposal_id == request.proposal.proposal_id
        )
        return EffectEvidence(
            request=request,
            decision=decision,
            attempt=attempt,
            outcome=outcome,
            audit_history=audit_history,
        )


__all__ = [
    "EffectBoundary",
    "EffectBoundaryError",
    "EffectAuthorityError",
    "EffectAttempt",
    "EffectDecision",
    "EffectEvidence",
    "EffectOutcome",
    "EffectRequest",
    "EffectRequestError",
]
