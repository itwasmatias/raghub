"""Revenue qualification engine for MissionaryX Revenue Bridge v0.1.

This module qualifies inbound events against bounded first-customer pain domains:
- Claude Code
- Codex
- Gemini
- AI-agent reliability
- Interrupted agent work
- Automation failures
- Python automation
- Linux development/tooling
- AI-generated-code verification

Key Invariants:
- Explicit separation between VERIFIED evidence and UNKNOWN information.
- Zero fabrication of customer willingness to pay, budget, company, or intent.
- Default bounded offer: MissionaryX Reliability Check (~$50 for one clearly bounded problem).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from revenue_bridge.events import AppEvent, _canonical_bytes, _require_text, _require_timestamp


class RevenueFitDecision(str, Enum):
    """Qualification decision for an opportunity."""
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class BoundedOffer:
    """Standardized truthful first-customer offer."""
    name: str = "MissionaryX Reliability Check"
    price_usd: float = 50.0
    scope: str = "one clearly bounded problem"
    deliverable: str = "Root-cause diagnosis, safety fence, and governed verification evidence"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "price_usd": self.price_usd,
            "scope": self.scope,
            "deliverable": self.deliverable,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BoundedOffer:
        return cls(
            name=data.get("name", "MissionaryX Reliability Check"),
            price_usd=float(data.get("price_usd", 50.0)),
            scope=data.get("scope", "one clearly bounded problem"),
            deliverable=data.get("deliverable", "Root-cause diagnosis, safety fence, and governed verification evidence"),
        )


@dataclass(frozen=True, slots=True)
class RevenueQualification:
    """Outcome of revenue qualification for an AppEvent."""
    event_id: str
    fit_decision: RevenueFitDecision
    verified_evidence: tuple[str, ...]
    unknown_information: tuple[str, ...]
    pain_categories: tuple[str, ...]
    reason: str
    proposed_offer: BoundedOffer | None
    qualified_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _require_text(self.event_id, "event_id"))
        if not isinstance(self.fit_decision, RevenueFitDecision):
            raise TypeError("fit_decision must be a RevenueFitDecision")
        if not isinstance(self.verified_evidence, tuple):
            object.__setattr__(self, "verified_evidence", tuple(self.verified_evidence))
        if not isinstance(self.unknown_information, tuple):
            object.__setattr__(self, "unknown_information", tuple(self.unknown_information))
        if not isinstance(self.pain_categories, tuple):
            object.__setattr__(self, "pain_categories", tuple(self.pain_categories))
        object.__setattr__(self, "reason", _require_text(self.reason, "reason"))
        object.__setattr__(self, "qualified_at", _require_timestamp(self.qualified_at, "qualified_at"))

    @property
    def is_qualified(self) -> bool:
        return self.fit_decision == RevenueFitDecision.QUALIFIED

    @property
    def fingerprint(self) -> str:
        payload = {
            "event_id": self.event_id,
            "fit_decision": self.fit_decision.value,
            "verified_evidence": sorted(self.verified_evidence),
            "unknown_information": sorted(self.unknown_information),
            "pain_categories": sorted(self.pain_categories),
            "reason": self.reason,
            "proposed_offer": self.proposed_offer.to_dict() if self.proposed_offer else None,
            "qualified_at": self.qualified_at.isoformat(),
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "fit_decision": self.fit_decision.value,
            "verified_evidence": list(self.verified_evidence),
            "unknown_information": list(self.unknown_information),
            "pain_categories": list(self.pain_categories),
            "reason": self.reason,
            "proposed_offer": self.proposed_offer.to_dict() if self.proposed_offer else None,
            "qualified_at": self.qualified_at.isoformat(),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RevenueQualification:
        qualified_at = data["qualified_at"]
        if isinstance(qualified_at, str):
            qualified_at = datetime.fromisoformat(qualified_at.replace("Z", "+00:00"))

        offer_raw = data.get("proposed_offer")
        offer = BoundedOffer.from_dict(offer_raw) if offer_raw else None

        return cls(
            event_id=data["event_id"],
            fit_decision=RevenueFitDecision(data["fit_decision"]),
            verified_evidence=tuple(data.get("verified_evidence", ())),
            unknown_information=tuple(data.get("unknown_information", ())),
            pain_categories=tuple(data.get("pain_categories", ())),
            reason=data["reason"],
            proposed_offer=offer,
            qualified_at=qualified_at,
        )


PAIN_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "claude_code": [
        re.compile(r"\bclaude[\s_-]?code\b", re.IGNORECASE),
        re.compile(r"\bclaude\s+cli\b", re.IGNORECASE),
    ],
    "codex": [
        re.compile(r"\bcodex\b", re.IGNORECASE),
        re.compile(r"\bopenai\s+codex\b", re.IGNORECASE),
    ],
    "gemini": [
        re.compile(r"\bgemini\b", re.IGNORECASE),
        re.compile(r"\bgoogle[\s_-]?gemini\b", re.IGNORECASE),
    ],
    "ai_agent_reliability": [
        re.compile(r"\bagent\s+(?:loop|hang|crash|flakiness|unreliable|failure|stuck|timeout|hallucinat)", re.IGNORECASE),
        re.compile(r"\bai[- ]agent\s+reliab", re.IGNORECASE),
        re.compile(r"\bautonomous\s+agent\s+(?:broke|fail|error|issue)", re.IGNORECASE),
    ],
    "interrupted_agent_work": [
        re.compile(r"\binterrupted\s+(?:agent|run|task|work|job)\b", re.IGNORECASE),
        re.compile(r"\bagent\s+(?:lost\s+state|context\s+loss|crashed\s+midway|failed\s+to\s+resume|stopped\s+unexpectedly)\b", re.IGNORECASE),
        re.compile(r"\bcheckpoint\s+(?:corruption|loss|missing)\b", re.IGNORECASE),
    ],
    "automation_failures": [
        re.compile(r"\bautomation\s+(?:fail|error|broke|crash|exception|hang)\b", re.IGNORECASE),
        re.compile(r"\bflaky\s+(?:pipeline|workflow|test|script|action)\b", re.IGNORECASE),
        re.compile(r"\bworkflow\s+(?:timeout|failure|abort)\b", re.IGNORECASE),
    ],
    "python_automation": [
        re.compile(r"\bpython\s+(?:script|automation|tool|subprocess|bot)\s+(?:fail|error|hang|crash|broken)\b", re.IGNORECASE),
        re.compile(r"\bpython\s+(?:asyncio|threading|concurrency)\s+bug\b", re.IGNORECASE),
    ],
    "linux_development_tooling": [
        re.compile(r"\blinux\s+(?:tooling|environment|sandbox|process|daemon|systemd)\s+(?:error|issue|fail)\b", re.IGNORECASE),
        re.compile(r"\bbash\s+(?:script|command)\s+(?:hang|fail|stuck)\b", re.IGNORECASE),
    ],
    "ai_generated_code_verification": [
        re.compile(r"\b(?:verify|verifying|verification|validate|validating)\s+(?:ai|llm|generated)\s+code\b", re.IGNORECASE),
        re.compile(r"\b(?:ai|llm|generated)\s+code\s+(?:hallucination|regression|bug|syntax\s+error)\b", re.IGNORECASE),
        re.compile(r"\bgoverned\s+(?:execution|verification|code\s+audit)\b", re.IGNORECASE),
    ],
}


class RevenueQualifier:
    """Evaluates AppEvents for MissionaryX revenue fit without fabricating facts."""

    def __init__(self, default_offer: BoundedOffer | None = None) -> None:
        self.default_offer = default_offer or BoundedOffer()

    def qualify(self, event: AppEvent, qualified_at: datetime | None = None) -> RevenueQualification:
        if not isinstance(event, AppEvent):
            raise TypeError("event must be an AppEvent")

        now = qualified_at or datetime.now(timezone.utc)
        content = event.content

        # Split content into sentences/lines for verbatim snippet extraction
        lines_and_sentences = re.split(r"(?:\n+|\. )", content)

        matched_categories: list[str] = []
        verified_evidence: list[str] = []

        for category, patterns in PAIN_PATTERNS.items():
            category_matched = False
            for pattern in patterns:
                for segment in lines_and_sentences:
                    trimmed = segment.strip()
                    if not trimmed:
                        continue
                    if pattern.search(trimmed):
                        if trimmed not in verified_evidence:
                            verified_evidence.append(trimmed)
                        category_matched = True
            if category_matched:
                matched_categories.append(category)

        # Baseline unknowns that must never be fabricated
        unknowns: list[str] = [
            "willingness_to_pay: unknown (not explicitly declared by actor)",
            "customer_budget: unknown",
            "company_identity: unknown (only handle/address observed)",
            "commercial_intent: unverified (needs approval-gated initial outreach)",
            "contact_preferences: unknown",
        ]

        if matched_categories:
            fit_decision = RevenueFitDecision.QUALIFIED
            reason = (
                f"Identified explicit pain relevant to {len(matched_categories)} category/ies: "
                f"{', '.join(matched_categories)}. Eligible for {self.default_offer.name} proposal."
            )
            proposed_offer = self.default_offer
        else:
            fit_decision = RevenueFitDecision.UNQUALIFIED
            reason = "No explicit evidence of supported AI-agent, automation, or reliability pain points observed."
            proposed_offer = None

        return RevenueQualification(
            event_id=event.event_id,
            fit_decision=fit_decision,
            verified_evidence=tuple(verified_evidence),
            unknown_information=tuple(unknowns),
            pain_categories=tuple(matched_categories),
            reason=reason,
            proposed_offer=proposed_offer,
            qualified_at=now,
        )
