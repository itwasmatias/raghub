"""Email normalization contract for MissionaryX Revenue Bridge v0.1.

This module normalizes inbound email evidence into the standard common AppEvent
contract, proving that both GitHub and Email share identical MissionaryX
governance, qualification, and approval flows.

Hard Safety Invariants:
- NO real mailbox connection or live email transmission.
- NO credential retrieval or storage.
- Deterministic ingestion of simulated/fixture email payloads.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from revenue_bridge.events import AppEvent, _canonical_bytes


SAMPLE_EMAIL_PYTHON_AUTOMATION_INQUIRY: dict[str, Any] = {
    "message_id": "<msg-20260831-001@client-corp.io>",
    "from": "lead-engineer@client-corp.io",
    "to": "info@missionaryx.ai",
    "subject": "Python automation bot failure & interrupted agent work",
    "body": (
        "Hi MissionaryX team,\n\n"
        "We are experiencing severe automation failures with our autonomous agent loop in production. "
        "Our Python automation scripts hang when running under Linux development tooling, and "
        "interrupted agent work leaves corrupt state. We need help verifying AI-generated code."
    ),
    "date": "2026-08-31T14:45:00Z",
}

SAMPLE_EMAIL_CODEX_VERIFICATION_INQUIRY: dict[str, Any] = {
    "message_id": "<msg-20260831-002@ai-startup.co>",
    "from": "cto@ai-startup.co",
    "to": "contact@missionaryx.ai",
    "subject": "Codex code verification and Gemini pipeline issues",
    "body": (
        "Hello,\n\n"
        "We need help setting up AI-generated-code verification for our Codex and Gemini pipelines. "
        "Agent loop crashes frequently without saving recovery checkpoints."
    ),
    "date": "2026-08-31T15:20:00Z",
}

SAMPLE_EMAIL_NEWSLETTER: dict[str, Any] = {
    "message_id": "<news-20260831-999@daily-updates.com>",
    "from": "digest@daily-updates.com",
    "to": "info@missionaryx.ai",
    "subject": "Weekly Tech Digest #401",
    "body": "Here are this week's top 10 articles on cloud computing and database indexing.",
    "date": "2026-08-31T09:00:00Z",
}


class EmailInboundNormalizer:
    """Normalizes raw email dictionaries into common AppEvents."""

    @staticmethod
    def normalize_email(payload: Mapping[str, Any]) -> AppEvent:
        """Normalize an email payload into an AppEvent."""
        msg_id = str(payload.get("message_id", "unknown_msg"))
        event_id = f"ev_em_{hashlib.sha256(msg_id.encode('utf-8')).hexdigest()[:12]}"

        actor = str(payload.get("from", "unknown_sender"))
        subject = str(payload.get("subject", "No Subject"))
        body = str(payload.get("body", ""))
        content = f"Subject: {subject}\n\n{body}".strip()

        thread_id = f"thread_{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:12]}"
        source_url = f"email://inbox/{msg_id.strip('<>')}"

        date_raw = payload.get("date")
        if isinstance(date_raw, str):
            observed_at = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
        elif isinstance(date_raw, datetime):
            observed_at = date_raw.astimezone(timezone.utc)
        else:
            observed_at = datetime.now(timezone.utc)

        raw_bytes = _canonical_bytes(dict(payload))
        raw_evidence_ref = f"raw_email_{hashlib.sha256(raw_bytes).hexdigest()[:16]}"

        capabilities = (
            "email.message.read",
            "email.reply.send",
        )

        uncertainty = (
            "actor_commercial_intent: preliminary",
            "budget: unknown",
            "willingness_to_pay: unknown",
            "verified_contact_channel: direct_email",
        )

        metadata = (
            ("platform", "email"),
            ("message_id", msg_id),
            ("subject", subject),
            ("recipient", str(payload.get("to", ""))),
        )

        return AppEvent(
            event_id=event_id,
            source_app="email",
            event_type="email.received",
            actor=actor,
            thread_id=thread_id,
            content=content,
            source_url=source_url,
            observed_at=observed_at,
            raw_evidence_ref=raw_evidence_ref,
            capabilities=capabilities,
            uncertainty=uncertainty,
            metadata=metadata,
        )

    @classmethod
    def load_fixture(cls, name: str) -> AppEvent:
        """Load deterministic email fixture by name."""
        fixtures = {
            "python_automation": SAMPLE_EMAIL_PYTHON_AUTOMATION_INQUIRY,
            "codex_verification": SAMPLE_EMAIL_CODEX_VERIFICATION_INQUIRY,
            "newsletter": SAMPLE_EMAIL_NEWSLETTER,
        }
        if name not in fixtures:
            raise KeyError(f"Unknown fixture: {name}. Available: {list(fixtures.keys())}")
        return cls.normalize_email(fixtures[name])
