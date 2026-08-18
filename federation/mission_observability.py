"""Read-only correlated Mission, Effect, and Evidence projection.

Mission Observability reads individually authoritative public records.  It does
not own mission lifecycle, effect outcome, evidence, or reconciliation truth.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, cast

from federation.control_domain import validate_domain_id
from federation.effect_safety import (
    AuthorityDisposition,
    AuthorityReservation,
    EffectDispatch,
    EffectIntent,
)
from federation.mission_runtime_store import MissionNotFoundError
from federation.mission_state import (
    EffectReference,
    MissionCheckpoint,
    MissionLifecycle,
    MissionSpecification,
    MissionTransition,
)
from research_mission.evidence_spine import (
    EvidencePointer,
    EvidenceRecord,
    EvidenceSpine,
)


SCHEMA_VERSION = "missionaryx.mission-observability.v0.1"


class MissionObservabilityError(Exception):
    """Base error for Mission Observability reads."""


class MissionObservabilityNotFoundError(MissionObservabilityError):
    """Raised when the requested mission does not exist in the requested domain."""


class MissionObservabilityIntegrityError(MissionObservabilityError):
    """Raised when authoritative public records form a broken or contradictory join."""


class MissionObservabilityConcurrentChangeError(MissionObservabilityError):
    """Raised when mission revision changes while a projection is being built."""


class ProjectedEffectStatus(str, Enum):
    """Conservative effect posture available to the v0.1 projection.

    UNKNOWN means public reads do not expose an unambiguous effect outcome.
    INDETERMINATE mirrors the authoritative gateway claim state directly.
    """

    UNKNOWN = "unknown"
    INDETERMINATE = "indeterminate"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_value(value: Any) -> Any:
    """Return a fresh deterministic JSON-compatible representation."""
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("projection contains a non-finite float")
        return value
    if isinstance(value, datetime):
        return _require_timestamp(value, "projection timestamp").isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if type(key) is not str:
                raise TypeError("projection mapping keys must be strings")
            normalized[key] = _json_value(value[key])
        return normalized
    raise TypeError(f"unsupported projection value: {type(value).__name__}")


def _fingerprint(value: Any) -> str:
    encoded = _canonical_json(_json_value(value)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_text(value: Any, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise MissionObservabilityIntegrityError(
            f"Authoritative {field_name} must be a non-empty string"
        )
    if value != value.strip():
        raise MissionObservabilityIntegrityError(
            f"Authoritative {field_name} must not contain surrounding whitespace"
        )
    return value


def _require_timestamp(value: Any, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise MissionObservabilityIntegrityError(
            f"Authoritative {field_name} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _claim_timestamp(value: Any, field_name: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if type(value) is datetime:
        return _require_timestamp(value, field_name)
    if type(value) is not str or not value.strip():
        raise MissionObservabilityIntegrityError(
            f"Gateway claim {field_name} must be an ISO timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MissionObservabilityIntegrityError(
            f"Gateway claim {field_name} is not a valid ISO timestamp"
        ) from exc
    return _require_timestamp(parsed, f"gateway claim {field_name}")


class _ProjectionModel:
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_value(self))

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class MissionSpecificationObservation(_ProjectionModel):
    mission_id: str
    control_domain: str
    objective: str
    owner_identity: str
    agent_identity: str | None
    success_criteria: str | None
    constraints: str | None
    deadline: datetime | None
    created_at: datetime
    specification_fingerprint: str


@dataclass(frozen=True, slots=True)
class MissionTransitionObservation(_ProjectionModel):
    transition_id: str
    mission_id: str
    control_domain: str
    from_state: MissionLifecycle
    to_state: MissionLifecycle
    revision: int
    reason: str | None
    checkpoint_id: str | None
    transitioned_at: datetime


@dataclass(frozen=True, slots=True)
class MissionCheckpointObservation(_ProjectionModel):
    checkpoint_id: str
    mission_id: str
    control_domain: str
    sequence: int
    mission_state: MissionLifecycle
    progress_data_json: str
    reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EffectReferenceObservation(_ProjectionModel):
    effect_intent_id: str
    effect_dispatch_id: str | None
    gateway_claim_id: str | None
    referenced_at: datetime


@dataclass(frozen=True, slots=True)
class EffectIntentObservation(_ProjectionModel):
    effect_intent_id: str
    decision_id: str
    mission_id: str
    task_id: str | None
    attempt_id: str
    operation_digest: str
    idempotency_key: str
    provider_scope: str
    authority_reservation_id: str
    compensation_strategy: str | None
    evidence_reference: str
    state: str
    created_at: datetime
    control_domain: str


@dataclass(frozen=True, slots=True)
class EffectDispatchObservation(_ProjectionModel):
    dispatch_id: str
    effect_intent_id: str
    attempt_id: str
    idempotency_key: str
    provider_adapter: str
    capability_profile_version: str
    transport_digest: str
    posture: str
    provider_operation_id: str | None
    evidence_reference: str
    dispatched_at: datetime
    control_domain: str


@dataclass(frozen=True, slots=True)
class GatewayClaimObservation(_ProjectionModel):
    gateway_claim_id: str
    control_domain: str
    request_fingerprint: str
    effect_intent_id: str
    effect_dispatch_id: str
    authority_reservation_id: str
    delegation_grant_id: str
    delegation_grant_fingerprint: str
    requested_capability: str
    idempotency_key: str
    operation_digest: str
    provider_id: str
    adapter_id: str
    owner_identity: str
    state: str
    claimed_at: datetime
    expires_at: datetime
    handoff_started_at: datetime | None
    receipt_recorded_at: datetime | None
    terminal_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthorityReservationObservation(_ProjectionModel):
    reservation_id: str
    effect_intent_id: str
    capability_type: str
    amount: float
    disposition: AuthorityDisposition
    reserved_at: datetime
    disposition_at: datetime | None
    disposition_evidence_reference_fingerprint: str | None
    disposition_evidence_record_fingerprint: str | None
    control_domain: str


@dataclass(frozen=True, slots=True)
class EffectObservation(_ProjectionModel):
    reference: EffectReferenceObservation
    intent: EffectIntentObservation
    dispatch: EffectDispatchObservation | None
    gateway_claim: GatewayClaimObservation | None
    reservation: AuthorityReservationObservation
    projected_status: ProjectedEffectStatus
    status_basis: str
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class UnresolvedEffectObservation(_ProjectionModel):
    effect_intent_id: str
    effect_dispatch_id: str | None
    gateway_claim_id: str | None
    projected_status: ProjectedEffectStatus
    reason: str
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class EvidenceObservation(_ProjectionModel):
    control_domain: str
    mission_id: str
    source: str
    record_id: str
    task_id: str | None
    source_revision: str
    reference_fingerprint: str
    record_fingerprint: str
    observed_at: datetime
    summary: str
    reference: str | None


@dataclass(frozen=True, slots=True)
class MissionTimelineEvent(_ProjectionModel):
    source_type: str
    source_id: str
    recorded_at: datetime
    source_fingerprint: str
    control_domain: str
    mission_id: str


@dataclass(frozen=True, slots=True)
class MissionObservation(_ProjectionModel):
    schema_version: str
    control_domain: str
    mission_id: str
    specification: MissionSpecificationObservation
    lifecycle: MissionLifecycle
    revision: int
    updated_at: datetime
    transitions: tuple[MissionTransitionObservation, ...]
    checkpoints: tuple[MissionCheckpointObservation, ...]
    effects: tuple[EffectObservation, ...]
    evidence: tuple[EvidenceObservation, ...]
    unresolved_effects: tuple[UnresolvedEffectObservation, ...]
    evidence_available: bool
    unbound_evidence_count: int
    timeline: tuple[MissionTimelineEvent, ...]
    generated_at: datetime
    projection_fingerprint: str


class _MissionRuntimeReader(Protocol):
    def get_mission(
        self, control_domain: str, mission_id: str
    ) -> tuple[MissionSpecification, MissionLifecycle, int, datetime]: ...

    def list_transitions(
        self, control_domain: str, mission_id: str
    ) -> list[MissionTransition]: ...

    def list_checkpoints(
        self, control_domain: str, mission_id: str
    ) -> list[MissionCheckpoint]: ...

    def list_effect_references(
        self, control_domain: str, mission_id: str
    ) -> list[EffectReference]: ...


class _EffectStoreReader(Protocol):
    def get_intent(self, effect_intent_id: str, control_domain: str) -> EffectIntent | None: ...

    def get_dispatch(self, dispatch_id: str, control_domain: str) -> EffectDispatch | None: ...

    def get_gateway_claim(
        self, gateway_claim_id: str, control_domain: str
    ) -> Mapping[str, Any] | None: ...

    def get_reservation(
        self,
        reservation_id: str,
        control_domain: str,
        evidence_spine: EvidenceSpine | None = None,
    ) -> AuthorityReservation | None: ...


class MissionObservability:
    """Build immutable correlated projections exclusively through public reads.

    A mission read is performed before and after correlation.  A change in the
    mission specification, lifecycle, revision, or authoritative ``updated_at``
    fails with ``MissionObservabilityConcurrentChangeError``.  There is no lock
    or cross-store atomic transaction.
    """

    def __init__(
        self,
        mission_runtime: _MissionRuntimeReader,
        effect_store: _EffectStoreReader,
        *,
        evidence_spine: EvidenceSpine | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._mission_runtime = mission_runtime
        self._effect_store = effect_store
        self._evidence_spine = evidence_spine
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def observe(self, control_domain: str, mission_id: str) -> MissionObservation:
        domain = validate_domain_id(control_domain, "control_domain")
        requested_mission_id = _require_text(mission_id, "mission_id")

        initial = self._read_mission(domain, requested_mission_id)
        specification, lifecycle, revision, updated_at = initial
        specification_observation = self._project_specification(specification)

        transitions = self._read_transitions(domain, requested_mission_id)
        checkpoints = self._read_checkpoints(domain, requested_mission_id)
        references = self._read_effect_references(domain, requested_mission_id)
        effects = tuple(
            self._observe_effect(domain, requested_mission_id, reference)
            for reference in references
        )
        evidence, unbound_evidence_count = self._read_evidence(
            domain,
            requested_mission_id,
        )
        unresolved = tuple(self._unresolved_effect(effect) for effect in effects)
        timeline = self._build_timeline(
            domain,
            requested_mission_id,
            transitions,
            checkpoints,
            effects,
            evidence,
        )

        final = self._read_mission(domain, requested_mission_id)
        if self._mission_read_identity(initial) != self._mission_read_identity(final):
            raise MissionObservabilityConcurrentChangeError(
                f"Mission {requested_mission_id!r} in domain {domain!r} changed "
                "while its observability projection was being built"
            )
        self._validate_transition_history(transitions, revision, lifecycle)

        generated_at = _require_timestamp(self._clock(), "generated_at")
        content = {
            "schema_version": SCHEMA_VERSION,
            "control_domain": domain,
            "mission_id": requested_mission_id,
            "specification": specification_observation,
            "lifecycle": lifecycle,
            "revision": revision,
            "updated_at": updated_at,
            "transitions": transitions,
            "checkpoints": checkpoints,
            "effects": effects,
            "evidence": evidence,
            "unresolved_effects": unresolved,
            "evidence_available": self._evidence_spine is not None,
            "unbound_evidence_count": unbound_evidence_count,
            "timeline": timeline,
        }
        projection_fingerprint = _fingerprint(content)
        return MissionObservation(
            schema_version=SCHEMA_VERSION,
            control_domain=domain,
            mission_id=requested_mission_id,
            specification=specification_observation,
            lifecycle=lifecycle,
            revision=revision,
            updated_at=updated_at,
            transitions=transitions,
            checkpoints=checkpoints,
            effects=effects,
            evidence=evidence,
            unresolved_effects=unresolved,
            evidence_available=self._evidence_spine is not None,
            unbound_evidence_count=unbound_evidence_count,
            timeline=timeline,
            generated_at=generated_at,
            projection_fingerprint=projection_fingerprint,
        )

    def timeline(
        self, control_domain: str, mission_id: str
    ) -> tuple[MissionTimelineEvent, ...]:
        return self.observe(control_domain, mission_id).timeline

    def _read_mission(
        self,
        control_domain: str,
        mission_id: str,
    ) -> tuple[MissionSpecification, MissionLifecycle, int, datetime]:
        try:
            result = self._mission_runtime.get_mission(control_domain, mission_id)
        except MissionNotFoundError as exc:
            raise MissionObservabilityNotFoundError(
                f"Mission {mission_id!r} was not found in domain {control_domain!r}"
            ) from exc
        if type(result) is not tuple or len(result) != 4:
            raise MissionObservabilityIntegrityError(
                "Mission Runtime get_mission returned an invalid public read shape"
            )
        specification, lifecycle, revision, updated_at = result
        if type(specification) is not MissionSpecification:
            raise MissionObservabilityIntegrityError(
                "Mission Runtime returned an invalid mission specification"
            )
        if specification.control_domain != control_domain:
            raise MissionObservabilityIntegrityError(
                f"Mission specification domain mismatch: requested {control_domain!r}, "
                f"found {specification.control_domain!r}"
            )
        if specification.mission_id != mission_id:
            raise MissionObservabilityIntegrityError(
                f"Mission specification ID mismatch: requested {mission_id!r}, "
                f"found {specification.mission_id!r}"
            )
        if type(lifecycle) is not MissionLifecycle:
            raise MissionObservabilityIntegrityError(
                "Mission Runtime returned an invalid MissionLifecycle"
            )
        if type(revision) is not int or revision < 1:
            raise MissionObservabilityIntegrityError(
                "Mission Runtime returned an invalid mission revision"
            )
        return specification, lifecycle, revision, _require_timestamp(updated_at, "updated_at")

    @staticmethod
    def _mission_read_identity(
        read: tuple[MissionSpecification, MissionLifecycle, int, datetime],
    ) -> tuple[str, MissionLifecycle, int, datetime]:
        specification, lifecycle, revision, updated_at = read
        return (
            specification.specification_fingerprint(),
            lifecycle,
            revision,
            updated_at,
        )

    @staticmethod
    def _project_specification(
        specification: MissionSpecification,
    ) -> MissionSpecificationObservation:
        return MissionSpecificationObservation(
            mission_id=specification.mission_id,
            control_domain=specification.control_domain,
            objective=specification.objective,
            owner_identity=specification.owner_identity,
            agent_identity=specification.agent_identity,
            success_criteria=specification.success_criteria,
            constraints=specification.constraints,
            deadline=specification.deadline,
            created_at=specification.created_at,
            specification_fingerprint=specification.specification_fingerprint(),
        )

    def _read_transitions(
        self,
        control_domain: str,
        mission_id: str,
    ) -> tuple[MissionTransitionObservation, ...]:
        source = self._mission_runtime.list_transitions(control_domain, mission_id)
        if not isinstance(source, (list, tuple)):
            raise MissionObservabilityIntegrityError(
                "Mission Runtime list_transitions returned an invalid public read shape"
            )
        projected: list[MissionTransitionObservation] = []
        seen_ids: set[str] = set()
        seen_revisions: set[int] = set()
        for transition in source:
            if type(transition) is not MissionTransition:
                raise MissionObservabilityIntegrityError(
                    "Mission Runtime returned an invalid mission transition"
                )
            self._require_mission_binding(
                transition.control_domain,
                transition.mission_id,
                control_domain,
                mission_id,
                f"transition {transition.transition_id!r}",
            )
            if transition.transition_id in seen_ids:
                raise MissionObservabilityIntegrityError(
                    f"Duplicate mission transition {transition.transition_id!r}"
                )
            if transition.revision in seen_revisions:
                raise MissionObservabilityIntegrityError(
                    f"Duplicate mission transition revision {transition.revision}"
                )
            seen_ids.add(transition.transition_id)
            seen_revisions.add(transition.revision)
            projected.append(
                MissionTransitionObservation(
                    transition_id=transition.transition_id,
                    mission_id=transition.mission_id,
                    control_domain=transition.control_domain,
                    from_state=transition.from_state,
                    to_state=transition.to_state,
                    revision=transition.revision,
                    reason=transition.reason,
                    checkpoint_id=transition.checkpoint_id,
                    transitioned_at=transition.transitioned_at,
                )
            )
        return tuple(projected)

    @staticmethod
    def _is_legal_mission_transition(from_state: MissionLifecycle, to_state: MissionLifecycle) -> bool:
        """Validate transition legality per authoritative Mission Runtime semantics.

        This mirrors Mission Runtime transition rules for fail-closed read validation only.
        It does not become a state-transition authority.

        Terminal states: COMPLETED, FAILED, CANCELLED cannot transition to anything.
        Non-terminal states allow idempotent transitions and specific non-idempotent transitions.
        """
        # Terminal states cannot transition
        if from_state in {MissionLifecycle.COMPLETED, MissionLifecycle.FAILED, MissionLifecycle.CANCELLED}:
            return False

        # Idempotent transitions are always legal for non-terminal states
        if from_state == to_state:
            return True

        # Non-idempotent transitions from CREATED
        if from_state == MissionLifecycle.CREATED:
            return to_state in {MissionLifecycle.RUNNING, MissionLifecycle.CANCELLED}

        # Non-idempotent transitions from RUNNING
        if from_state == MissionLifecycle.RUNNING:
            return to_state in {
                MissionLifecycle.PAUSED,
                MissionLifecycle.COMPLETED,
                MissionLifecycle.FAILED,
                MissionLifecycle.CANCELLED,
            }

        # Non-idempotent transitions from PAUSED
        if from_state == MissionLifecycle.PAUSED:
            return to_state in {
                MissionLifecycle.RUNNING,
                MissionLifecycle.COMPLETED,
                MissionLifecycle.FAILED,
                MissionLifecycle.CANCELLED,
            }

        return False

    @staticmethod
    def _validate_transition_history(
        transitions: tuple[MissionTransitionObservation, ...],
        mission_revision: int,
        lifecycle: MissionLifecycle,
    ) -> None:
        expected_revisions = tuple(range(1, mission_revision + 1))
        actual_revisions = tuple(item.revision for item in transitions)
        if actual_revisions != expected_revisions:
            raise MissionObservabilityIntegrityError(
                f"Mission transition revisions contradict mission revision {mission_revision}: "
                f"found {actual_revisions!r}"
            )
        if not transitions or transitions[-1].to_state is not lifecycle:
            raise MissionObservabilityIntegrityError(
                "Mission transition history contradicts current lifecycle"
            )

        # Validate first transition is exactly the initial CREATED -> CREATED at revision 1
        if len(transitions) > 0:
            first = transitions[0]
            if first.revision != 1:
                raise MissionObservabilityIntegrityError(
                    f"Mission first transition must be revision 1, found {first.revision}"
                )
            if first.from_state != MissionLifecycle.CREATED:
                raise MissionObservabilityIntegrityError(
                    f"Mission first transition must be from CREATED, found {first.from_state.value!r}"
                )
            if first.to_state != MissionLifecycle.CREATED:
                raise MissionObservabilityIntegrityError(
                    f"Mission first transition must be to CREATED, found {first.to_state.value!r}"
                )

        # Validate state chain continuity and transition legality for subsequent transitions
        for i, transition in enumerate(transitions):
            if i == 0:
                # First transition already validated above
                continue
            previous = transitions[i - 1]

            # Check continuity: from_state must equal previous to_state
            if transition.from_state != previous.to_state:
                raise MissionObservabilityIntegrityError(
                    f"Mission transition chain broken at revision {transition.revision}: "
                    f"transition from {transition.from_state.value!r} but previous ended at "
                    f"{previous.to_state.value!r}"
                )

            # Check transition legality per Mission Runtime rules
            if not MissionObservability._is_legal_mission_transition(
                transition.from_state, transition.to_state
            ):
                raise MissionObservabilityIntegrityError(
                    f"Illegal mission transition at revision {transition.revision}: "
                    f"{transition.from_state.value!r} -> {transition.to_state.value!r}"
                )

        # Validate timestamp ordering: transitions must not regress in time
        for i in range(1, len(transitions)):
            previous = transitions[i - 1]
            current = transitions[i]
            if current.transitioned_at < previous.transitioned_at:
                raise MissionObservabilityIntegrityError(
                    f"Mission transition timestamp regression at revision {current.revision}: "
                    f"timestamp {current.transitioned_at.isoformat()} precedes previous "
                    f"transition timestamp {previous.transitioned_at.isoformat()}"
                )

    def _read_checkpoints(
        self,
        control_domain: str,
        mission_id: str,
    ) -> tuple[MissionCheckpointObservation, ...]:
        source = self._mission_runtime.list_checkpoints(control_domain, mission_id)
        if not isinstance(source, (list, tuple)):
            raise MissionObservabilityIntegrityError(
                "Mission Runtime list_checkpoints returned an invalid public read shape"
            )
        projected: list[MissionCheckpointObservation] = []
        seen_ids: set[str] = set()
        seen_sequences: set[int] = set()
        for checkpoint in source:
            if type(checkpoint) is not MissionCheckpoint:
                raise MissionObservabilityIntegrityError(
                    "Mission Runtime returned an invalid mission checkpoint"
                )
            self._require_mission_binding(
                checkpoint.control_domain,
                checkpoint.mission_id,
                control_domain,
                mission_id,
                f"checkpoint {checkpoint.checkpoint_id!r}",
            )
            if checkpoint.checkpoint_id in seen_ids:
                raise MissionObservabilityIntegrityError(
                    f"Duplicate mission checkpoint {checkpoint.checkpoint_id!r}"
                )
            if checkpoint.sequence in seen_sequences:
                raise MissionObservabilityIntegrityError(
                    f"Duplicate mission checkpoint sequence {checkpoint.sequence}"
                )
            seen_ids.add(checkpoint.checkpoint_id)
            seen_sequences.add(checkpoint.sequence)
            projected.append(
                MissionCheckpointObservation(
                    checkpoint_id=checkpoint.checkpoint_id,
                    mission_id=checkpoint.mission_id,
                    control_domain=checkpoint.control_domain,
                    sequence=checkpoint.sequence,
                    mission_state=checkpoint.mission_state,
                    progress_data_json=_canonical_json(dict(checkpoint.progress_data)),
                    reason=checkpoint.reason,
                    created_at=checkpoint.created_at,
                )
            )
        projected.sort(key=lambda item: (item.sequence, item.created_at, item.checkpoint_id))
        return tuple(projected)

    def _read_effect_references(
        self,
        control_domain: str,
        mission_id: str,
    ) -> tuple[EffectReference, ...]:
        source = self._mission_runtime.list_effect_references(control_domain, mission_id)
        if not isinstance(source, (list, tuple)):
            raise MissionObservabilityIntegrityError(
                "Mission Runtime list_effect_references returned an invalid public read shape"
            )
        references: list[EffectReference] = []
        seen_intents: set[str] = set()
        for reference in source:
            if type(reference) is not EffectReference:
                raise MissionObservabilityIntegrityError(
                    "Mission Runtime returned an invalid effect reference"
                )
            if reference.effect_intent_id in seen_intents:
                raise MissionObservabilityIntegrityError(
                    f"Duplicate effect reference for intent {reference.effect_intent_id!r}"
                )
            seen_intents.add(reference.effect_intent_id)
            references.append(reference)
        references.sort(
            key=lambda item: (
                item.referenced_at,
                item.effect_intent_id,
                item.effect_dispatch_id or "",
                item.gateway_claim_id or "",
            )
        )
        return tuple(references)

    def _observe_effect(
        self,
        control_domain: str,
        mission_id: str,
        reference: EffectReference,
    ) -> EffectObservation:
        intent = self._effect_store.get_intent(reference.effect_intent_id, control_domain)
        if intent is None:
            raise MissionObservabilityIntegrityError(
                f"Referenced effect intent {reference.effect_intent_id!r} does not exist "
                f"in domain {control_domain!r}"
            )
        if type(intent) is not EffectIntent:
            raise MissionObservabilityIntegrityError(
                f"Referenced effect intent {reference.effect_intent_id!r} has an invalid read shape"
            )
        if intent.effect_intent_id != reference.effect_intent_id:
            raise MissionObservabilityIntegrityError(
                f"Effect intent ID mismatch: requested {reference.effect_intent_id!r}, "
                f"found {intent.effect_intent_id!r}"
            )
        self._require_mission_binding(
            intent.control_domain,
            intent.mission_id,
            control_domain,
            mission_id,
            f"effect intent {intent.effect_intent_id!r}",
        )

        dispatch: EffectDispatch | None = None
        if reference.effect_dispatch_id is not None:
            dispatch = self._read_dispatch(
                reference.effect_dispatch_id,
                control_domain,
                intent,
            )

        claim: GatewayClaimObservation | None = None
        if reference.gateway_claim_id is not None:
            raw_claim = self._effect_store.get_gateway_claim(
                reference.gateway_claim_id,
                control_domain,
            )
            if raw_claim is None:
                raise MissionObservabilityIntegrityError(
                    f"Referenced gateway claim {reference.gateway_claim_id!r} does not exist "
                    f"in domain {control_domain!r}"
                )
            claim = self._project_claim(raw_claim, reference.gateway_claim_id, control_domain)
            if claim.effect_intent_id != intent.effect_intent_id:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim.gateway_claim_id!r} binds intent "
                    f"{claim.effect_intent_id!r}, expected {intent.effect_intent_id!r}"
                )
            if claim.authority_reservation_id != intent.authority_reservation_id:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim.gateway_claim_id!r} binds reservation "
                    f"{claim.authority_reservation_id!r}, expected "
                    f"{intent.authority_reservation_id!r}"
                )
            if claim.idempotency_key != intent.idempotency_key:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim.gateway_claim_id!r} idempotency_key contradicts intent"
                )
            if claim.operation_digest != intent.operation_digest:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim.gateway_claim_id!r} operation_digest contradicts intent"
                )
            if reference.effect_dispatch_id is not None:
                if claim.effect_dispatch_id != reference.effect_dispatch_id:
                    raise MissionObservabilityIntegrityError(
                        f"Gateway claim {claim.gateway_claim_id!r} binds dispatch "
                        f"{claim.effect_dispatch_id!r}, expected {reference.effect_dispatch_id!r}"
                    )
                # Validate exact adapter correlation between claim and dispatch
                if dispatch is not None:
                    if claim.adapter_id != dispatch.provider_adapter:
                        raise MissionObservabilityIntegrityError(
                            f"Gateway claim {claim.gateway_claim_id!r} adapter_id "
                            f"{claim.adapter_id!r} contradicts dispatch provider_adapter "
                            f"{dispatch.provider_adapter!r}"
                        )
            else:
                dispatch = self._read_dispatch(
                    claim.effect_dispatch_id,
                    control_domain,
                    intent,
                )
                # Validate exact adapter correlation between claim and dispatch
                if claim.adapter_id != dispatch.provider_adapter:
                    raise MissionObservabilityIntegrityError(
                        f"Gateway claim {claim.gateway_claim_id!r} adapter_id "
                        f"{claim.adapter_id!r} contradicts dispatch provider_adapter "
                        f"{dispatch.provider_adapter!r}"
                    )

        reservation = self._effect_store.get_reservation(
            intent.authority_reservation_id,
            control_domain,
            self._evidence_spine,
        )
        if reservation is None:
            raise MissionObservabilityIntegrityError(
                f"Authoritative reservation {intent.authority_reservation_id!r} for effect "
                f"intent {intent.effect_intent_id!r} does not exist in domain {control_domain!r}"
            )
        if type(reservation) is not AuthorityReservation:
            raise MissionObservabilityIntegrityError(
                f"Authority reservation {intent.authority_reservation_id!r} has an invalid read shape"
            )
        if reservation.reservation_id != intent.authority_reservation_id:
            raise MissionObservabilityIntegrityError(
                f"Authority reservation ID mismatch: requested {intent.authority_reservation_id!r}, "
                f"found {reservation.reservation_id!r}"
            )
        if reservation.control_domain != control_domain:
            raise MissionObservabilityIntegrityError(
                f"Authority reservation {reservation.reservation_id!r} domain mismatch: "
                f"expected {control_domain!r}, found {reservation.control_domain!r}"
            )
        if reservation.effect_intent_id != intent.effect_intent_id:
            raise MissionObservabilityIntegrityError(
                f"Authority reservation {reservation.reservation_id!r} binds intent "
                f"{reservation.effect_intent_id!r}, expected {intent.effect_intent_id!r}"
            )

        intent_observation = EffectIntentObservation(
            effect_intent_id=intent.effect_intent_id,
            decision_id=intent.decision_id,
            mission_id=intent.mission_id,
            task_id=intent.task_id,
            attempt_id=intent.attempt_id,
            operation_digest=intent.operation_digest,
            idempotency_key=intent.idempotency_key,
            provider_scope=intent.provider_scope,
            authority_reservation_id=intent.authority_reservation_id,
            compensation_strategy=intent.compensation_strategy,
            evidence_reference=intent.evidence_reference,
            state=intent.state,
            created_at=intent.created_at,
            control_domain=intent.control_domain,
        )
        dispatch_observation = (
            None if dispatch is None else self._project_dispatch(dispatch)
        )
        reservation_observation = self._project_reservation(reservation, control_domain)
        status = (
            ProjectedEffectStatus.INDETERMINATE
            if claim is not None and claim.state == "indeterminate"
            else ProjectedEffectStatus.UNKNOWN
        )
        status_basis = (
            "authoritative_gateway_claim_state"
            if status is ProjectedEffectStatus.INDETERMINATE
            else "effect_outcome_unavailable_from_public_reads"
        )
        return EffectObservation(
            reference=EffectReferenceObservation(
                effect_intent_id=reference.effect_intent_id,
                effect_dispatch_id=reference.effect_dispatch_id,
                gateway_claim_id=reference.gateway_claim_id,
                referenced_at=reference.referenced_at,
            ),
            intent=intent_observation,
            dispatch=dispatch_observation,
            gateway_claim=claim,
            reservation=reservation_observation,
            projected_status=status,
            status_basis=status_basis,
            reconciliation_required=status is ProjectedEffectStatus.INDETERMINATE,
        )

    def _read_dispatch(
        self,
        dispatch_id: str,
        control_domain: str,
        intent: EffectIntent,
    ) -> EffectDispatch:
        dispatch = self._effect_store.get_dispatch(dispatch_id, control_domain)
        if dispatch is None:
            raise MissionObservabilityIntegrityError(
                f"Referenced effect dispatch {dispatch_id!r} does not exist "
                f"in domain {control_domain!r}"
            )
        if type(dispatch) is not EffectDispatch:
            raise MissionObservabilityIntegrityError(
                f"Referenced effect dispatch {dispatch_id!r} has an invalid read shape"
            )
        if dispatch.dispatch_id != dispatch_id:
            raise MissionObservabilityIntegrityError(
                f"Effect dispatch ID mismatch: requested {dispatch_id!r}, "
                f"found {dispatch.dispatch_id!r}"
            )
        if dispatch.control_domain != control_domain:
            raise MissionObservabilityIntegrityError(
                f"Effect dispatch {dispatch.dispatch_id!r} domain mismatch: expected "
                f"{control_domain!r}, found {dispatch.control_domain!r}"
            )
        if dispatch.effect_intent_id != intent.effect_intent_id:
            raise MissionObservabilityIntegrityError(
                f"Effect dispatch {dispatch.dispatch_id!r} binds intent "
                f"{dispatch.effect_intent_id!r}, expected {intent.effect_intent_id!r}"
            )
        if dispatch.attempt_id != intent.attempt_id:
            raise MissionObservabilityIntegrityError(
                f"Effect dispatch {dispatch.dispatch_id!r} attempt_id contradicts intent"
            )
        if dispatch.idempotency_key != intent.idempotency_key:
            raise MissionObservabilityIntegrityError(
                f"Effect dispatch {dispatch.dispatch_id!r} idempotency_key contradicts intent"
            )
        return dispatch

    @staticmethod
    def _project_dispatch(dispatch: EffectDispatch) -> EffectDispatchObservation:
        return EffectDispatchObservation(
            dispatch_id=dispatch.dispatch_id,
            effect_intent_id=dispatch.effect_intent_id,
            attempt_id=dispatch.attempt_id,
            idempotency_key=dispatch.idempotency_key,
            provider_adapter=dispatch.provider_adapter,
            capability_profile_version=dispatch.capability_profile_version,
            transport_digest=dispatch.transport_digest,
            posture=dispatch.posture,
            provider_operation_id=dispatch.provider_operation_id,
            evidence_reference=dispatch.evidence_reference,
            dispatched_at=dispatch.dispatched_at,
            control_domain=dispatch.control_domain,
        )

    @staticmethod
    def _project_claim(
        source: Mapping[str, Any],
        expected_claim_id: str,
        control_domain: str,
    ) -> GatewayClaimObservation:
        if not isinstance(source, Mapping):
            raise MissionObservabilityIntegrityError(
                f"Gateway claim {expected_claim_id!r} has an invalid read shape"
            )
        if "control_domain" in source and source["control_domain"] != control_domain:
            raise MissionObservabilityIntegrityError(
                f"Gateway claim {expected_claim_id!r} domain mismatch: expected "
                f"{control_domain!r}, found {source['control_domain']!r}"
            )

        def required(name: str) -> str:
            if name not in source:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {expected_claim_id!r} is missing {name}"
                )
            return _require_text(source[name], f"gateway claim {name}")

        claim_id = required("gateway_claim_id")
        if claim_id != expected_claim_id:
            raise MissionObservabilityIntegrityError(
                f"Gateway claim ID mismatch: requested {expected_claim_id!r}, found {claim_id!r}"
            )
        state = required("state")
        allowed_states = {
            "prepared",
            "claimed",
            "handoff_started",
            "receipt_recorded",
            "terminal",
            "indeterminate",
        }
        if state not in allowed_states:
            raise MissionObservabilityIntegrityError(
                f"Gateway claim {claim_id!r} has invalid state {state!r}"
            )
        claimed_at = cast(datetime, _claim_timestamp(source.get("claimed_at"), "claimed_at"))
        expires_at = cast(datetime, _claim_timestamp(source.get("expires_at"), "expires_at"))
        handoff_started_at = _claim_timestamp(
            source.get("handoff_started_at"),
            "handoff_started_at",
            optional=True,
        )
        receipt_recorded_at = _claim_timestamp(
            source.get("receipt_recorded_at"),
            "receipt_recorded_at",
            optional=True,
        )
        terminal_at = _claim_timestamp(
            source.get("terminal_at"),
            "terminal_at",
            optional=True,
        )
        if expires_at <= claimed_at:
            raise MissionObservabilityIntegrityError(
                f"Gateway claim {claim_id!r} expires_at must follow claimed_at"
            )

        # Validate required phase timestamps for each state
        if state in {"prepared", "claimed"}:
            if handoff_started_at is not None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} must not have handoff_started_at"
                )
            if receipt_recorded_at is not None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} must not have receipt_recorded_at"
                )
            if terminal_at is not None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} must not have terminal_at"
                )

        if state == "handoff_started":
            if handoff_started_at is None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} requires handoff_started_at"
                )
            if receipt_recorded_at is not None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} must not have receipt_recorded_at"
                )
            if terminal_at is not None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} must not have terminal_at"
                )

        if state == "receipt_recorded":
            if handoff_started_at is None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} requires handoff_started_at"
                )
            if receipt_recorded_at is None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} requires receipt_recorded_at"
                )
            if terminal_at is not None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} must not have terminal_at"
                )

        if state == "terminal":
            if handoff_started_at is None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} requires handoff_started_at"
                )
            if receipt_recorded_at is None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} requires receipt_recorded_at"
                )
            if terminal_at is None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} requires terminal_at"
                )

        if state == "indeterminate":
            if handoff_started_at is None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} requires handoff_started_at"
                )
            if receipt_recorded_at is not None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} indeterminate state must not have receipt_recorded_at"
                )
            if terminal_at is None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} state {state!r} requires terminal_at"
                )

        # Validate chronological ordering of phase timestamps
        if handoff_started_at is not None:
            if handoff_started_at < claimed_at:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} handoff_started_at must follow claimed_at"
                )

        if receipt_recorded_at is not None:
            if handoff_started_at is None:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} receipt_recorded_at requires handoff_started_at"
                )
            if receipt_recorded_at < handoff_started_at:
                raise MissionObservabilityIntegrityError(
                    f"Gateway claim {claim_id!r} receipt_recorded_at must follow handoff_started_at"
                )

        if terminal_at is not None:
            if state == "terminal":
                # Terminal path requires full handoff -> receipt -> terminal progression
                if receipt_recorded_at is None:
                    raise MissionObservabilityIntegrityError(
                        f"Gateway claim {claim_id!r} terminal_at requires receipt_recorded_at"
                    )
                if terminal_at < receipt_recorded_at:
                    raise MissionObservabilityIntegrityError(
                        f"Gateway claim {claim_id!r} terminal_at must follow receipt_recorded_at"
                    )
            elif state == "indeterminate":
                # Indeterminate path: handoff -> indeterminate (no receipt)
                if handoff_started_at is None:
                    raise MissionObservabilityIntegrityError(
                        f"Gateway claim {claim_id!r} terminal_at requires handoff_started_at"
                    )
                if terminal_at < handoff_started_at:
                    raise MissionObservabilityIntegrityError(
                        f"Gateway claim {claim_id!r} terminal_at must follow handoff_started_at"
                    )

        return GatewayClaimObservation(
            gateway_claim_id=claim_id,
            control_domain=control_domain,
            request_fingerprint=required("request_fingerprint"),
            effect_intent_id=required("effect_intent_id"),
            effect_dispatch_id=required("effect_dispatch_id"),
            authority_reservation_id=required("authority_reservation_id"),
            delegation_grant_id=required("delegation_grant_id"),
            delegation_grant_fingerprint=required("delegation_grant_fingerprint"),
            requested_capability=required("requested_capability"),
            idempotency_key=required("idempotency_key"),
            operation_digest=required("operation_digest"),
            provider_id=required("provider_id"),
            adapter_id=required("adapter_id"),
            owner_identity=required("owner_identity"),
            state=state,
            claimed_at=claimed_at,
            expires_at=expires_at,
            handoff_started_at=handoff_started_at,
            receipt_recorded_at=receipt_recorded_at,
            terminal_at=terminal_at,
        )

    @staticmethod
    def _project_reservation(
        reservation: AuthorityReservation,
        control_domain: str,
    ) -> AuthorityReservationObservation:
        pointer = reservation.disposition_evidence
        reference_fingerprint = None
        record_fingerprint = None
        if pointer is not None:
            if type(pointer) is not EvidencePointer:
                raise MissionObservabilityIntegrityError(
                    f"Authority reservation {reservation.reservation_id!r} has invalid evidence"
                )
            if pointer.key.domain_id is not None and pointer.key.domain_id != control_domain:
                raise MissionObservabilityIntegrityError(
                    f"Authority reservation {reservation.reservation_id!r} evidence domain mismatch"
                )
            reference_fingerprint = pointer.reference_fingerprint
            record_fingerprint = pointer.record_fingerprint
        return AuthorityReservationObservation(
            reservation_id=reservation.reservation_id,
            effect_intent_id=reservation.effect_intent_id,
            capability_type=reservation.capability_type,
            amount=float(reservation.amount),
            disposition=reservation.disposition,
            reserved_at=reservation.reserved_at,
            disposition_at=reservation.disposition_at,
            disposition_evidence_reference_fingerprint=reference_fingerprint,
            disposition_evidence_record_fingerprint=record_fingerprint,
            control_domain=reservation.control_domain,
        )

    def _read_evidence(
        self,
        control_domain: str,
        mission_id: str,
    ) -> tuple[tuple[EvidenceObservation, ...], int]:
        if self._evidence_spine is None:
            return (), 0
        records = self._evidence_spine.records_for_mission(mission_id)
        if not isinstance(records, tuple):
            raise MissionObservabilityIntegrityError(
                "EvidenceSpine records_for_mission returned an invalid public read shape"
            )
        projected: list[EvidenceObservation] = []
        unbound_count = 0
        seen: set[tuple[str, str, str]] = set()
        for record in records:
            if type(record) is not EvidenceRecord:
                raise MissionObservabilityIntegrityError(
                    "EvidenceSpine returned an invalid evidence record"
                )
            if record.key.mission_id != mission_id:
                raise MissionObservabilityIntegrityError(
                    "EvidenceSpine records_for_mission returned a different mission ID"
                )
            if record.key.domain_id is None:
                unbound_count += 1
                continue
            if record.key.domain_id != control_domain:
                # Cross-domain records are neither projected nor counted.  They must
                # not affect this domain's content fingerprint.
                continue
            pointer = EvidencePointer.from_record(record)
            verified = self._evidence_spine.verify_evidence(pointer)
            if type(verified) is not EvidenceRecord:
                raise MissionObservabilityIntegrityError(
                    "EvidenceSpine verify_evidence returned an invalid record"
                )
            if verified.record_fingerprint != record.record_fingerprint or verified.key != record.key:
                raise MissionObservabilityIntegrityError(
                    f"Evidence verification contradicted record {record.key.record_id!r}"
                )
            identity = (
                record.key.source,
                record.key.record_id,
                record.record_fingerprint,
            )
            if identity in seen:
                raise MissionObservabilityIntegrityError(
                    f"Duplicate verified evidence record {record.key.record_id!r}"
                )
            seen.add(identity)
            projected.append(
                EvidenceObservation(
                    control_domain=control_domain,
                    mission_id=mission_id,
                    source=record.key.source,
                    record_id=record.key.record_id,
                    task_id=record.key.task_id,
                    source_revision=record.reference.source_revision,
                    reference_fingerprint=record.reference.fingerprint,
                    record_fingerprint=record.record_fingerprint,
                    observed_at=record.reference.observed_at,
                    summary=record.reference.summary,
                    reference=record.reference.reference,
                )
            )
        projected.sort(
            key=lambda item: (
                item.observed_at,
                item.source,
                item.record_id,
                item.record_fingerprint,
            )
        )
        return tuple(projected), unbound_count

    @staticmethod
    def _unresolved_effect(effect: EffectObservation) -> UnresolvedEffectObservation:
        reason = (
            "indeterminate_gateway_claim"
            if effect.projected_status is ProjectedEffectStatus.INDETERMINATE
            else "effect_outcome_unavailable"
        )
        return UnresolvedEffectObservation(
            effect_intent_id=effect.reference.effect_intent_id,
            effect_dispatch_id=effect.reference.effect_dispatch_id,
            gateway_claim_id=effect.reference.gateway_claim_id,
            projected_status=effect.projected_status,
            reason=reason,
            reconciliation_required=effect.reconciliation_required,
        )

    @classmethod
    def _build_timeline(
        cls,
        control_domain: str,
        mission_id: str,
        transitions: tuple[MissionTransitionObservation, ...],
        checkpoints: tuple[MissionCheckpointObservation, ...],
        effects: tuple[EffectObservation, ...],
        evidence: tuple[EvidenceObservation, ...],
    ) -> tuple[MissionTimelineEvent, ...]:
        events: list[MissionTimelineEvent] = []
        for transition in transitions:
            events.append(
                cls._timeline_event(
                    "mission_transition",
                    transition.transition_id,
                    transition.transitioned_at,
                    transition,
                    control_domain,
                    mission_id,
                )
            )
        for checkpoint in checkpoints:
            events.append(
                cls._timeline_event(
                    "mission_checkpoint",
                    checkpoint.checkpoint_id,
                    checkpoint.created_at,
                    checkpoint,
                    control_domain,
                    mission_id,
                )
            )
        for effect in effects:
            events.append(
                cls._timeline_event(
                    "effect_reference",
                    effect.reference.effect_intent_id,
                    effect.reference.referenced_at,
                    effect.reference,
                    control_domain,
                    mission_id,
                )
            )
            events.append(
                cls._timeline_event(
                    "effect_intent",
                    effect.intent.effect_intent_id,
                    effect.intent.created_at,
                    effect.intent,
                    control_domain,
                    mission_id,
                )
            )
            if effect.dispatch is not None:
                events.append(
                    cls._timeline_event(
                        "effect_dispatch",
                        effect.dispatch.dispatch_id,
                        effect.dispatch.dispatched_at,
                        effect.dispatch,
                        control_domain,
                        mission_id,
                    )
                )
            events.append(
                cls._timeline_event(
                    "authority_reservation",
                    effect.reservation.reservation_id,
                    effect.reservation.reserved_at,
                    effect.reservation,
                    control_domain,
                    mission_id,
                )
            )
            if effect.reservation.disposition_at is not None:
                events.append(
                    cls._timeline_event(
                        "authority_reservation_disposition",
                        effect.reservation.reservation_id,
                        effect.reservation.disposition_at,
                        effect.reservation,
                        control_domain,
                        mission_id,
                    )
                )
            claim = effect.gateway_claim
            if claim is not None:
                events.append(
                    cls._timeline_event(
                        "gateway_claim",
                        claim.gateway_claim_id,
                        claim.claimed_at,
                        claim,
                        control_domain,
                        mission_id,
                    )
                )
                if claim.handoff_started_at is not None:
                    events.append(
                        cls._timeline_event(
                            "gateway_handoff_started",
                            claim.gateway_claim_id,
                            claim.handoff_started_at,
                            claim,
                            control_domain,
                            mission_id,
                        )
                    )
                if claim.receipt_recorded_at is not None:
                    events.append(
                        cls._timeline_event(
                            "gateway_receipt_recorded",
                            claim.gateway_claim_id,
                            claim.receipt_recorded_at,
                            claim,
                            control_domain,
                            mission_id,
                        )
                    )
                if claim.terminal_at is not None:
                    source_type = (
                        "gateway_indeterminate"
                        if claim.state == "indeterminate"
                        else "gateway_terminal"
                    )
                    events.append(
                        cls._timeline_event(
                            source_type,
                            claim.gateway_claim_id,
                            claim.terminal_at,
                            claim,
                            control_domain,
                            mission_id,
                        )
                    )
        for item in evidence:
            events.append(
                MissionTimelineEvent(
                    source_type="evidence",
                    source_id=f"{item.source}:{item.record_id}",
                    recorded_at=item.observed_at,
                    source_fingerprint=item.record_fingerprint,
                    control_domain=control_domain,
                    mission_id=mission_id,
                )
            )
        events.sort(
            key=lambda item: (
                item.recorded_at,
                item.source_type,
                item.source_id,
                item.source_fingerprint,
            )
        )
        return tuple(events)

    @staticmethod
    def _timeline_event(
        source_type: str,
        source_id: str,
        recorded_at: datetime,
        source: _ProjectionModel,
        control_domain: str,
        mission_id: str,
    ) -> MissionTimelineEvent:
        source_fingerprint = _fingerprint(
            {
                "source_type": source_type,
                "source": source,
            }
        )
        return MissionTimelineEvent(
            source_type=source_type,
            source_id=source_id,
            recorded_at=recorded_at,
            source_fingerprint=source_fingerprint,
            control_domain=control_domain,
            mission_id=mission_id,
        )

    @staticmethod
    def _require_mission_binding(
        actual_domain: str,
        actual_mission_id: str,
        expected_domain: str,
        expected_mission_id: str,
        source_name: str,
    ) -> None:
        if actual_domain != expected_domain:
            raise MissionObservabilityIntegrityError(
                f"{source_name} domain mismatch: expected {expected_domain!r}, "
                f"found {actual_domain!r}"
            )
        if actual_mission_id != expected_mission_id:
            raise MissionObservabilityIntegrityError(
                f"{source_name} mission mismatch: expected {expected_mission_id!r}, "
                f"found {actual_mission_id!r}"
            )


__all__ = [
    "SCHEMA_VERSION",
    "MissionObservabilityError",
    "MissionObservabilityNotFoundError",
    "MissionObservabilityIntegrityError",
    "MissionObservabilityConcurrentChangeError",
    "ProjectedEffectStatus",
    "MissionSpecificationObservation",
    "MissionTransitionObservation",
    "MissionCheckpointObservation",
    "EffectReferenceObservation",
    "EffectIntentObservation",
    "EffectDispatchObservation",
    "GatewayClaimObservation",
    "AuthorityReservationObservation",
    "EffectObservation",
    "UnresolvedEffectObservation",
    "EvidenceObservation",
    "MissionTimelineEvent",
    "MissionObservation",
    "MissionObservability",
]
