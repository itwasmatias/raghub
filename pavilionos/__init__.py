"""PavilionOS Canonical Gateway Binding v0.1

Repository-contained integration binding PavilionOS local-shell provider
to the canonical MissionaryX Governed Effect Gateway.

This package provides:
- Canonical coordinator for Pavilion actions
- Provider adapter with mandatory permit consumption
- Authorization envelope for secure adapter invocation

Critical invariant:
NO PAVILIONOS PROVIDER EFFECT MAY BEGIN UNLESS THE MATCHING CANONICAL
MISSIONARYX GATEWAY PERMIT HAS ALREADY BEEN SUCCESSFULLY CONSUMED AND
THE CANONICAL CLAIM HAS DURABLY ENTERED HANDOFF_STARTED.
"""

__all__ = [
    "canonical_coordinator",
    "canonical_adapter",
    "authorization_envelope",
]
