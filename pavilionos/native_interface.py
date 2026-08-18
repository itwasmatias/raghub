"""PavilionOS Native Interface v0.1 — read-only presentation of MissionaryX truth.

GOVERNING RULE:

    PAVILIONOS PRESENTS MISSIONARYX TRUTH.
    PAVILIONOS DOES NOT RECREATE MISSIONARYX TRUTH.

This module provides a read-only presentation boundary for MissionaryX
mission/effect state through the accepted Mission Observability API.

This is a PRESENTATION layer only. It does NOT:
- Own mission lifecycle truth
- Own effect outcome truth
- Own evidence truth
- Execute effectful operations
- Query authoritative stores directly

All consequential Pavilion actions MUST continue through:
    CanonicalPavilionCoordinator.coordinate(...)

v0.1 Limitations:
- Read-only presentation only
- No consequential action execution
- No reconciliation
- No provider invocation
- No direct store access
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from federation.mission_observability import (
    EffectObservation,
    MissionObservability,
    MissionObservabilityError,
    MissionObservation,
    MissionTimelineEvent,
    ProjectedEffectStatus,
    UnresolvedEffectObservation,
)
from federation.mission_state import MissionLifecycle


class PavilionNativeInterfaceError(Exception):
    """Base error for Pavilion Native Interface presentation failures."""


class PavilionNativeInterfaceSourceError(PavilionNativeInterfaceError):
    """Raised when the source MissionObservability fails with integrity error."""


class _MissionObservabilityReader(Protocol):
    """Protocol for reading mission observations."""

    def observe(self, control_domain: str, mission_id: str) -> MissionObservation: ...

    def timeline(
        self, control_domain: str, mission_id: str
    ) -> tuple[MissionTimelineEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class PavilionEffectSummary:
    """Immutable effect summary for presentation.

    Preserves exact effect status semantics:
    - UNKNOWN remains UNKNOWN (not converted to nothing_landed/something_landed)
    - INDETERMINATE remains INDETERMINATE (not converted to failed)
    - reconciliation_required is explicitly visible
    """

    effect_intent_id: str
    effect_dispatch_id: str | None
    gateway_claim_id: str | None
    projected_status: ProjectedEffectStatus
    status_basis: str
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class PavilionUnresolvedEffectSummary:
    """Immutable unresolved effect summary for presentation."""

    effect_intent_id: str
    effect_dispatch_id: str | None
    gateway_claim_id: str | None
    projected_status: ProjectedEffectStatus
    reason: str
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class PavilionTimelineItem:
    """Immutable timeline event for presentation.

    Preserves source identity and does not claim causal ordering.
    Timeline ordering comes from MissionObservation, not independent reconstruction.
    """

    source_type: str
    source_id: str
    recorded_at: datetime
    source_fingerprint: str
    control_domain: str
    mission_id: str


@dataclass(frozen=True, slots=True)
class PavilionMissionView:
    """Immutable mission view for PavilionOS native presentation.

    All values are derived from MissionObservation. This view does NOT
    add new truth-bearing state.

    Effect status semantics:
    - UNKNOWN: public reads do not expose unambiguous effect outcome
    - INDETERMINATE: authoritative gateway claim is indeterminate
    - Mission FAILED does NOT imply effects failed (nothing_landed)
    - Mission COMPLETED does NOT imply effects succeeded (something_landed)
    """

    control_domain: str
    mission_id: str
    objective: str
    owner_identity: str
    agent_identity: str | None
    lifecycle: MissionLifecycle
    revision: int
    updated_at: datetime
    created_at: datetime
    effect_count: int
    unresolved_effect_count: int
    evidence_available: bool
    evidence_count: int
    unbound_evidence_count: int
    effects: tuple[PavilionEffectSummary, ...]
    unresolved_effects: tuple[PavilionUnresolvedEffectSummary, ...]
    timeline_summary: tuple[PavilionTimelineItem, ...]
    projection_fingerprint: str
    generated_at: datetime


class PavilionNativeInterface:
    """Read-only native presentation interface for MissionaryX mission/effect state.

    IMPORTANT: This interface is READ-ONLY. It does NOT execute effectful operations.

    Future consequential Pavilion actions MUST use:
        CanonicalPavilionCoordinator.coordinate(...)

    This class does NOT:
    - Call CanonicalPavilionCoordinator.coordinate()
    - Call CanonicalPavilionAdapter.dispatch()
    - Query SQLite directly
    - Read MissionRuntimeStore/DurableEffectStore directly
    - Issue permits or consume credentials
    - Execute reconciliation
    - Invoke providers
    - Execute subprocesses
    - Make network requests

    It ONLY presents MissionaryX truth through MissionObservability.
    """

    def __init__(self, observability: _MissionObservabilityReader) -> None:
        """Initialize Pavilion Native Interface with observability dependency.

        Args:
            observability: MissionObservability-compatible reader for mission state.
                Must NOT be a raw SQLite path or authoritative store.
        """
        self._observability = observability

    def mission_view(
        self, control_domain: str, mission_id: str
    ) -> PavilionMissionView:
        """Build immutable mission view for presentation.

        Args:
            control_domain: Control domain scope for mission
            mission_id: Mission identifier

        Returns:
            Immutable PavilionMissionView derived from MissionObservation

        Raises:
            PavilionNativeInterfaceSourceError: Source observation integrity failure
            PavilionNativeInterfaceError: Mission not found or other presentation error
        """
        try:
            observation = self._observability.observe(control_domain, mission_id)
        except MissionObservabilityError as exc:
            # Distinguish integrity failures from not-found
            if "integrity" in str(exc).lower() or "contradict" in str(exc).lower():
                raise PavilionNativeInterfaceSourceError(
                    f"Mission observation integrity failure: {exc}"
                ) from exc
            raise PavilionNativeInterfaceError(str(exc)) from exc

        # Build immutable effect summaries
        effects = tuple(
            PavilionEffectSummary(
                effect_intent_id=effect.reference.effect_intent_id,
                effect_dispatch_id=effect.reference.effect_dispatch_id,
                gateway_claim_id=effect.reference.gateway_claim_id,
                projected_status=effect.projected_status,
                status_basis=effect.status_basis,
                reconciliation_required=effect.reconciliation_required,
            )
            for effect in observation.effects
        )

        # Build immutable unresolved effect summaries
        unresolved = tuple(
            PavilionUnresolvedEffectSummary(
                effect_intent_id=unresolved.effect_intent_id,
                effect_dispatch_id=unresolved.effect_dispatch_id,
                gateway_claim_id=unresolved.gateway_claim_id,
                projected_status=unresolved.projected_status,
                reason=unresolved.reason,
                reconciliation_required=unresolved.reconciliation_required,
            )
            for unresolved in observation.unresolved_effects
        )

        # Build immutable timeline summary
        timeline = tuple(
            PavilionTimelineItem(
                source_type=event.source_type,
                source_id=event.source_id,
                recorded_at=event.recorded_at,
                source_fingerprint=event.source_fingerprint,
                control_domain=event.control_domain,
                mission_id=event.mission_id,
            )
            for event in observation.timeline
        )

        return PavilionMissionView(
            control_domain=observation.control_domain,
            mission_id=observation.mission_id,
            objective=observation.specification.objective,
            owner_identity=observation.specification.owner_identity,
            agent_identity=observation.specification.agent_identity,
            lifecycle=observation.lifecycle,
            revision=observation.revision,
            updated_at=observation.updated_at,
            created_at=observation.specification.created_at,
            effect_count=len(observation.effects),
            unresolved_effect_count=len(observation.unresolved_effects),
            evidence_available=observation.evidence_available,
            evidence_count=len(observation.evidence),
            unbound_evidence_count=observation.unbound_evidence_count,
            effects=effects,
            unresolved_effects=unresolved,
            timeline_summary=timeline,
            projection_fingerprint=observation.projection_fingerprint,
            generated_at=observation.generated_at,
        )

    def timeline_view(
        self, control_domain: str, mission_id: str
    ) -> tuple[PavilionTimelineItem, ...]:
        """Build immutable timeline view for presentation.

        Timeline uses the accepted MissionObservation timeline identity.
        Ordering is NOT independently reconstructed from authoritative stores.

        Args:
            control_domain: Control domain scope for mission
            mission_id: Mission identifier

        Returns:
            Immutable tuple of timeline items

        Raises:
            PavilionNativeInterfaceSourceError: Source observation integrity failure
            PavilionNativeInterfaceError: Mission not found or other presentation error
        """
        try:
            timeline = self._observability.timeline(control_domain, mission_id)
        except MissionObservabilityError as exc:
            if "integrity" in str(exc).lower() or "contradict" in str(exc).lower():
                raise PavilionNativeInterfaceSourceError(
                    f"Mission observation integrity failure: {exc}"
                ) from exc
            raise PavilionNativeInterfaceError(str(exc)) from exc

        return tuple(
            PavilionTimelineItem(
                source_type=event.source_type,
                source_id=event.source_id,
                recorded_at=event.recorded_at,
                source_fingerprint=event.source_fingerprint,
                control_domain=event.control_domain,
                mission_id=event.mission_id,
            )
            for event in timeline
        )

    def render_mission(self, control_domain: str, mission_id: str) -> str:
        """Render mission state as deterministic text suitable for native display.

        Output is deterministic for unchanged observation and does not use ANSI escapes.

        Effect semantics:
        - UNKNOWN remains "UNKNOWN" (not nothing_landed/something_landed)
        - INDETERMINATE shows "INDETERMINATE — reconciliation required"
        - Mission FAILED does NOT rewrite effect outcomes
        - Mission COMPLETED does NOT rewrite effect outcomes

        Args:
            control_domain: Control domain scope for mission
            mission_id: Mission identifier

        Returns:
            Deterministic text rendering of mission state

        Raises:
            PavilionNativeInterfaceSourceError: Source observation integrity failure
            PavilionNativeInterfaceError: Mission not found or other presentation error
        """
        view = self.mission_view(control_domain, mission_id)

        lines = [
            "MissionaryX Mission",
            "-------------------",
            f"Mission: {view.mission_id}",
            f"Domain: {view.control_domain}",
            f"State: {view.lifecycle.value.upper()}",
            f"Revision: {view.revision}",
            f"Owner: {view.owner_identity}",
        ]

        if view.agent_identity is not None:
            lines.append(f"Agent: {view.agent_identity}")

        lines.extend(
            [
                "",
                "Objective",
                "---------",
                view.objective,
                "",
                "Status",
                "------",
                f"Created: {view.created_at.isoformat()}",
                f"Updated: {view.updated_at.isoformat()}",
                f"Effects: {view.effect_count}",
                f"Unresolved effects: {view.unresolved_effect_count}",
                f"Evidence: {'available' if view.evidence_available else 'unavailable'}",
                f"Evidence count: {view.evidence_count}",
            ]
        )

        if view.unbound_evidence_count > 0:
            lines.append(f"Unbound evidence: {view.unbound_evidence_count}")

        lines.append(f"Projection: {view.projection_fingerprint[:16]}...")

        if view.unresolved_effect_count > 0:
            lines.extend(["", "Unresolved Effects", "------------------"])
            for unresolved in view.unresolved_effects:
                status_display = self._format_effect_status(
                    unresolved.projected_status, unresolved.reconciliation_required
                )
                lines.append(f"{unresolved.effect_intent_id[:32]}...  {status_display}")

        if len(view.timeline_summary) > 0:
            # Show recent timeline (last 5 events)
            lines.extend(["", "Recent Timeline", "---------------"])
            recent = view.timeline_summary[-5:]
            for item in recent:
                lines.append(
                    f"{item.recorded_at.isoformat()} [{item.source_type}] {item.source_id[:40]}"
                )

        lines.extend(
            [
                "",
                f"Generated: {view.generated_at.isoformat()}",
                "",
            ]
        )

        return "\n".join(lines)

    @staticmethod
    def _format_effect_status(
        status: ProjectedEffectStatus, reconciliation_required: bool
    ) -> str:
        """Format effect status for display, preserving exact semantics.

        UNKNOWN remains UNKNOWN (not converted to nothing_landed/something_landed).
        INDETERMINATE shows reconciliation requirement explicitly.

        Args:
            status: Projected effect status
            reconciliation_required: Whether reconciliation is required

        Returns:
            Human-readable status string preserving exact semantics
        """
        if status is ProjectedEffectStatus.UNKNOWN:
            return "UNKNOWN"
        elif status is ProjectedEffectStatus.INDETERMINATE:
            if reconciliation_required:
                return "INDETERMINATE — reconciliation required"
            return "INDETERMINATE"
        else:
            return status.value.upper()


__all__ = [
    "PavilionNativeInterfaceError",
    "PavilionNativeInterfaceSourceError",
    "PavilionEffectSummary",
    "PavilionUnresolvedEffectSummary",
    "PavilionTimelineItem",
    "PavilionMissionView",
    "PavilionNativeInterface",
]
