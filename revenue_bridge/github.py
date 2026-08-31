"""GitHub inbound normalization slice for MissionaryX Revenue Bridge v0.1.

This module parses and normalizes GitHub issue and comment evidence into
the standard common AppEvent contract.

Hard Safety Invariants:
- NO live GitHub posting or commenting.
- NO authentication or credential storage.
- Deterministic ingestion of fixtures and unauthenticated payloads.
- Missing fields (e.g., budget, company) remain unknown; never fabricated.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from revenue_bridge.events import AppEvent, _canonical_bytes, _require_text


SAMPLE_GITHUB_CLAUDE_CODE_ISSUE: dict[str, Any] = {
    "id": 10928374,
    "html_url": "https://github.com/developer-tools/agent-runner/issues/104",
    "number": 104,
    "title": "Claude Code agent loop hang during large refactoring task",
    "body": (
        "We are running into frequent interrupted agent work when using Claude Code on our Linux development "
        "environment. The agent loop hangs midway through executing Python automation scripts and loses state. "
        "We need a reliable way to verify AI-generated code and ensure autonomous agent reliability."
    ),
    "user": {
        "login": "alexdev99",
        "id": 482019,
    },
    "created_at": "2026-08-31T14:30:00Z",
    "state": "open",
}

SAMPLE_GITHUB_GEMINI_AUTOMATION_ISSUE: dict[str, Any] = {
    "id": 10928399,
    "html_url": "https://github.com/infra-ops/workflow-engine/issues/42",
    "number": 42,
    "title": "Gemini automation failures in Python subprocess environment",
    "body": (
        "Encountering repeated automation failures when invoking Gemini workflows. "
        "Our Python automation bot crashes without saving checkpoints. Need governed execution."
    ),
    "user": {
        "login": "cloud-builder",
        "id": 993812,
    },
    "created_at": "2026-08-31T15:10:00Z",
    "state": "open",
}

SAMPLE_GITHUB_NON_RELEVANT_ISSUE: dict[str, Any] = {
    "id": 10928450,
    "html_url": "https://github.com/example/docs/issues/12",
    "number": 12,
    "title": "Fix typo in README header",
    "body": "There is a small spelling mistake on line 4 of README.md (recieve -> receive).",
    "user": {
        "login": "doc-editor",
        "id": 112233,
    },
    "created_at": "2026-08-31T15:45:00Z",
    "state": "open",
}


class GitHubInboundNormalizer:
    """Normalizes raw GitHub payloads into common AppEvents."""

    @staticmethod
    def normalize_issue(payload: Mapping[str, Any]) -> AppEvent:
        """Normalize a GitHub issue dictionary into an AppEvent."""
        issue_id = str(payload.get("id", payload.get("number", "unknown")))
        event_id = f"ev_gh_issue_{issue_id}"

        user = payload.get("user", {})
        actor = user.get("login", "unknown_user") if isinstance(user, dict) else str(user)

        title = payload.get("title", "")
        body = payload.get("body", "")
        content = f"{title}\n\n{body}".strip() if title else body.strip()

        source_url = payload.get("html_url") or payload.get("url")
        number = payload.get("number", issue_id)
        thread_id = f"issue-{number}"

        created_raw = payload.get("created_at")
        if isinstance(created_raw, str):
            observed_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        elif isinstance(created_raw, datetime):
            observed_at = created_raw.astimezone(timezone.utc)
        else:
            observed_at = datetime.now(timezone.utc)

        raw_bytes = _canonical_bytes(dict(payload))
        raw_evidence_ref = f"raw_gh_issue_{hashlib.sha256(raw_bytes).hexdigest()[:16]}"

        capabilities = (
            "github.issue.read",
            "github.issue_comment.read",
            "github.issue_comment.reply",
        )

        uncertainty = (
            "actor_commercial_intent: unknown",
            "budget: unknown",
            "willingness_to_pay: unknown",
            "verified_contact_channel: public_github_only",
        )

        metadata = (
            ("platform", "github"),
            ("resource_kind", "issue"),
            ("issue_number", str(number)),
            ("state", str(payload.get("state", "open"))),
        )

        return AppEvent(
            event_id=event_id,
            source_app="github",
            event_type="github.issue_opened",
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

    @staticmethod
    def normalize_comment(payload: Mapping[str, Any], issue_number: int | str = "unknown") -> AppEvent:
        """Normalize a GitHub comment dictionary into an AppEvent."""
        comment_id = str(payload.get("id", "unknown"))
        event_id = f"ev_gh_comment_{comment_id}"

        user = payload.get("user", {})
        actor = user.get("login", "unknown_user") if isinstance(user, dict) else str(user)
        body = payload.get("body", "")

        source_url = payload.get("html_url") or payload.get("url")
        thread_id = f"issue-{issue_number}"

        created_raw = payload.get("created_at")
        if isinstance(created_raw, str):
            observed_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        elif isinstance(created_raw, datetime):
            observed_at = created_raw.astimezone(timezone.utc)
        else:
            observed_at = datetime.now(timezone.utc)

        raw_bytes = _canonical_bytes(dict(payload))
        raw_evidence_ref = f"raw_gh_comment_{hashlib.sha256(raw_bytes).hexdigest()[:16]}"

        capabilities = (
            "github.issue.read",
            "github.issue_comment.read",
            "github.issue_comment.reply",
        )

        uncertainty = (
            "actor_commercial_intent: unknown",
            "budget: unknown",
            "willingness_to_pay: unknown",
            "verified_contact_channel: public_github_only",
        )

        metadata = (
            ("platform", "github"),
            ("resource_kind", "comment"),
            ("comment_id", str(comment_id)),
            ("issue_number", str(issue_number)),
        )

        return AppEvent(
            event_id=event_id,
            source_app="github",
            event_type="github.issue_comment",
            actor=actor,
            thread_id=thread_id,
            content=body,
            source_url=source_url,
            observed_at=observed_at,
            raw_evidence_ref=raw_evidence_ref,
            capabilities=capabilities,
            uncertainty=uncertainty,
            metadata=metadata,
        )

    @classmethod
    def load_fixture(cls, name: str) -> AppEvent:
        """Load deterministic test fixture by name."""
        fixtures = {
            "claude_code": SAMPLE_GITHUB_CLAUDE_CODE_ISSUE,
            "gemini_automation": SAMPLE_GITHUB_GEMINI_AUTOMATION_ISSUE,
            "non_relevant": SAMPLE_GITHUB_NON_RELEVANT_ISSUE,
        }
        if name not in fixtures:
            raise KeyError(f"Unknown fixture: {name}. Available: {list(fixtures.keys())}")
        return cls.normalize_issue(fixtures[name])
