"""Execution prompt compiler.

M6 Execution Prompt Engine v0.1 — Compiler

Compiles deterministic execution prompts from milestone + dev-status + policy.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from tools.execution_prompt.models import (
    ExecutionPrompt,
    MilestoneSpec,
    DevStatusHandoff,
    PolicyProfile,
    PromptKind,
)
from tools.execution_prompt.validation import (
    validate_dev_status,
    validate_milestone_spec,
)


class PromptCompilationError(Exception):
    """Raised when prompt compilation fails."""

    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


def _canonical(value: Any) -> bytes:
    """Canonical JSON serialization for fingerprinting."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    """SHA-256 fingerprint of canonical representation."""
    return hashlib.sha256(_canonical(value)).hexdigest()


class ExecutionPromptCompiler:
    """Deterministic execution prompt compiler."""

    REQUIRED_SECTIONS = [
        "Mission",
        "Authoritative live state",
        "Governing constraints",
        "Scope",
        "Non-goals",
        "Preserve / do-not-touch constraints",
        "Preflight",
        "Implementation contract",
        "Effect and authority ceiling",
        "Credit contract",
        "Validation",
        "Trial-and-error rule",
        "Effect-state reporting",
        "Reconciliation obligation",
        "Mission-effect fold and lifecycle",
        "Stop conditions",
        "Completion response schema",
    ]

    def compile_new_milestone_implementation(
        self,
        milestone: MilestoneSpec,
        dev_status: DevStatusHandoff,
        policy: PolicyProfile,
        *,
        reference_time: datetime | None = None,
    ) -> ExecutionPrompt:
        """Compile a new_milestone_implementation prompt.

        Args:
            milestone: Milestone specification
            dev_status: Development status handoff
            policy: Policy profile
            reference_time: Optional reference time for dev-status staleness check
        """
        # Validate inputs
        if milestone is None or dev_status is None or policy is None:
            raise PromptCompilationError(
                "milestone, dev_status, and policy are all required",
                "MISSING_REQUIRED_INPUT",
            )

        validate_milestone_spec(milestone.to_dict())
        validate_dev_status(dev_status.to_dict(), reference_time=reference_time)

        # Build canonical input for fingerprinting
        canonical_input = {
            "kind": PromptKind.NEW_MILESTONE_IMPLEMENTATION.value,
            "milestone": milestone.to_dict(),
            "dev_status": dev_status.to_dict(),
            "policy": policy.to_dict(),
        }

        # Render prompt sections
        sections = self._render_sections(milestone, dev_status, policy)
        rendered_text = "\n\n".join(sections)

        # Compute fingerprint
        prompt_fingerprint = _fingerprint(canonical_input)

        return ExecutionPrompt(
            kind=PromptKind.NEW_MILESTONE_IMPLEMENTATION,
            milestone_spec=milestone,
            dev_status=dev_status,
            policy=policy,
            rendered_text=rendered_text,
            prompt_fingerprint=prompt_fingerprint,
            compiled_at=datetime.now(timezone.utc),
        )

    def _render_sections(
        self,
        milestone: MilestoneSpec,
        dev_status: DevStatusHandoff,
        policy: PolicyProfile,
    ) -> list[str]:
        """Render all 17 required sections in order."""
        return [
            self._section_mission(milestone),
            self._section_authoritative_live_state(dev_status),
            self._section_governing_constraints(policy),
            self._section_scope(milestone),
            self._section_non_goals(),
            self._section_preserve_constraints(dev_status),
            self._section_preflight(dev_status),
            self._section_implementation_contract(milestone),
            self._section_effect_authority_ceiling(policy),
            self._section_credit_contract(),
            self._section_validation(milestone),
            self._section_trial_and_error_rule(),
            self._section_effect_state_reporting(),
            self._section_reconciliation_obligation(),
            self._section_mission_effect_fold_lifecycle(),
            self._section_stop_conditions(),
            self._section_completion_response_schema(),
        ]

    def _section_mission(self, milestone: MilestoneSpec) -> str:
        return f"""# Mission

Implement {milestone.title} ({milestone.milestone_id})

{milestone.description}

Requirements:
{chr(10).join(f"- {req}" for req in milestone.requirements)}"""

    def _section_authoritative_live_state(self, dev_status: DevStatusHandoff) -> str:
        return f"""# Authoritative live state

Repository: {dev_status.repo}
Branch: {dev_status.branch}
HEAD: {dev_status.head}
Status: {dev_status.status}
Timestamp: {dev_status.timestamp.isoformat()}"""

    def _section_governing_constraints(self, policy: PolicyProfile) -> str:
        return f"""# Governing constraints

Policy version: {policy.version}
Paid budget: ${policy.paid_budget:.2f}
Allowed effects: {", ".join(policy.allowed_effects) if policy.allowed_effects else "none"}

All external effects must go through the effect boundary.
No effects may execute without authority reservation.
Indeterminate effects must create reconciliation obligations."""

    def _section_scope(self, milestone: MilestoneSpec) -> str:
        return f"""# Scope

This milestone implements: {milestone.title}

Work within the approved milestone specification only."""

    def _section_non_goals(self) -> str:
        return """# Non-goals

- Do not implement features outside this milestone
- Do not integrate with external paid services without explicit policy
- Do not modify protected worktrees or branches
- Do not weaken existing safety invariants"""

    def _section_preserve_constraints(self, dev_status: DevStatusHandoff) -> str:
        return f"""# Preserve / do-not-touch constraints

Protected worktree: {dev_status.repo}
Protected branch: {dev_status.branch}

Do not modify files outside the approved scope.
Do not reset, force-push, or delete worktrees."""

    def _section_preflight(self, dev_status: DevStatusHandoff) -> str:
        return f"""# Preflight

Verify repository state matches dev-status handoff:
- Branch: {dev_status.branch}
- HEAD: {dev_status.head}
- Status: {dev_status.status}

If state does not match, STOP with structured blocker."""

    def _section_implementation_contract(self, milestone: MilestoneSpec) -> str:
        reqs = "\n".join(f"{i+1}. {req}" for i, req in enumerate(milestone.requirements))
        return f"""# Implementation contract

Deliver:
{reqs}

All deliverables must pass validation."""

    def _section_effect_authority_ceiling(self, policy: PolicyProfile) -> str:
        return f"""# Effect and authority ceiling

Maximum paid budget: ${policy.paid_budget:.2f}
Allowed effect types: {", ".join(policy.allowed_effects) if policy.allowed_effects else "none"}

Effects exceeding this ceiling are REJECTED."""

    def _section_credit_contract(self) -> str:
        return """# Credit contract

All commits must include:

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>"""

    def _section_validation(self, milestone: MilestoneSpec) -> str:
        return f"""# Validation

Required validation:
- All focused tests pass
- Repository-standard static checks pass
- Compilation succeeds
- No uncommitted secrets

Validation failure is a STOP condition."""

    def _section_trial_and_error_rule(self) -> str:
        return """# Trial-and-error rule

On validation failure:
1. Diagnose using concrete evidence
2. Make ONE bounded corrective attempt
3. If second attempt fails, STOP with structured report

Do not expand scope or weaken safety."""

    def _section_effect_state_reporting(self) -> str:
        return """# Effect-state reporting

All external effects must report:
- Effect intent ID
- Dispatch posture (attempting, accepted, rejected, unknown)
- Effect state (nothing_landed, something_landed, indeterminate)
- Authority reservation disposition

Indeterminate effects create reconciliation obligations."""

    def _section_reconciliation_obligation(self) -> str:
        return """# Reconciliation obligation

When effect state is indeterminate:
1. Create durable reconciliation obligation
2. Hold authority reservation
3. Do NOT release reservation on timeout
4. Do NOT resolve indeterminate from task state

Only boundary reconciliation evidence resolves indeterminate."""

    def _section_mission_effect_fold_lifecycle(self) -> str:
        return """# Mission-effect fold and lifecycle

Mission posture folds over effect states:
- Any indeterminate effect → parked_reconciliation
- Failed task + uncompensated landed effect → dirty_compensation_required
- All failed with nothing_landed → clean_failed
- Successful with intended effects → clean_succeeded

Mission lifecycle:
- OPEN: Tasks may execute
- CLOSED: Task execution ended (effects may be unsettled)
- SEALED: All effects settled, evidence committed, immutable

Cannot seal with indeterminate effects or unsettled compensation."""

    def _section_stop_conditions(self) -> str:
        return """# Stop conditions

STOP with structured blocker if:
- Dev-status is stale, contradictory, or missing
- Required dependencies missing and creating them expands scope
- Substantive conflict with accepted architecture
- Authority ceiling would be exceeded
- Second corrective attempt fails
- Secret-bearing input detected"""

    def _section_completion_response_schema(self) -> str:
        return """# Completion response schema

Final response must include:
- Status: COMPLETE | BLOCKED | NEEDS_OPERATOR_DECISION
- Worktree and branch
- Starting and ending HEAD
- Commit hash (if created)
- Files changed and why
- REUSE / WRAP / EXTEND decisions
- Validation commands with exact outcomes
- Acceptance criteria demonstrated
- Remaining deferred work
- Exact dev-status handoff for next session
- Confirmation: no external effects, nothing pushed/merged"""


def compile_new_milestone_implementation_prompt(
    milestone: MilestoneSpec,
    dev_status: DevStatusHandoff,
    policy: PolicyProfile,
    *,
    reference_time: datetime | None = None,
) -> ExecutionPrompt:
    """Compile a new_milestone_implementation prompt.

    This is the public API for prompt compilation.

    Args:
        milestone: Milestone specification
        dev_status: Development status handoff
        policy: Policy profile
        reference_time: Optional reference time for dev-status staleness check (for testing)
    """
    compiler = ExecutionPromptCompiler()
    return compiler.compile_new_milestone_implementation(
        milestone,
        dev_status,
        policy,
        reference_time=reference_time,
    )


__all__ = [
    "ExecutionPromptCompiler",
    "PromptCompilationError",
    "compile_new_milestone_implementation_prompt",
]
