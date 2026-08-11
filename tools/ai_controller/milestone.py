from __future__ import annotations

"""Milestone Controller for isolated feature development workflows.

This module provides the MilestoneController, which orchestrates the creation,
validation, and evidence generation for milestone-based development. A milestone
represents an isolated feature implementation with explicit boundaries, validation
phases, and human review gates.

## Key Design Principles

1. **Base Safety**: All milestone bases must descend from the canonical base branch.
   Symbolic refs are rejected in dirty repositories to prevent ambiguity.

2. **Changed-File Boundary**: Complete tracking of committed, staged, unstaged, and
   untracked files relative to the milestone base.

3. **7-Phase Validation**: Focused tests → Adjacent tests → Full suite → Compileall
   → Git diff check → Staged diff check → Final git status.

4. **Human Review Gates**: The controller never commits, merges, pushes, or rebases.
   All integration requires explicit human approval after evidence generation.

5. **State Persistence**: Milestone state is persisted atomically with file locking
   to support resumability and concurrent access safety.

## Heuristic Test Discovery

**IMPORTANT**: Adjacent test discovery is heuristic and may not find all relevant
tests. It uses stem-based matching to discover tests that reference changed files,
but this is not guaranteed to be complete. Always provide explicit `focused_tests`
for critical test coverage. Adjacent test phases are marked with `kind="heuristic"`
to clearly indicate their approximate nature.

## Validation Lock Behavior

The validation lock (`.validation.lock`) prevents concurrent validation runs that
could interfere with each other. The lock currently uses indefinite blocking via
FileLock. In the rare case of an orphaned lock file (e.g., process killed), manual
recovery is required: remove the `.validation.lock` file from the controller root
directory and retry validation.

For production deployments requiring automatic timeout handling, consider enhancing
FileLock with explicit timeout support.
"""

import hashlib
import json
import os
import re
import shlex
import site
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ._locking import FileLock
from .changes import capture_changes
from .config import ControllerConfig
from .models import Task, utc_now
from .queue import atomic_json
from .remote import LocalRunner, ProcessResult
from .worktrees import WorktreeInfo, WorktreeManager


CANONICAL_BASE_BRANCH = "integration/canonical-control-plane-baseline-v0-1"
MILESTONE_ROOT_DIR = "milestones"
STATE_FILENAME = "state.json"
EVIDENCE_FILENAME = "evidence.json"
REPORT_FILENAME = "report.md"
HANDOFF_FILENAME = "handoff.md"
MAX_CAPTURED_TEXT = 100_000
FULL_VALIDATION_PHASES = (
    "focused_tests",
    "adjacent_tests",
    "full_suite",
    "compileall",
    "git_diff_check",
    "staged_diff_check",
    "final_git_status",
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class MilestoneError(ValueError):
    """Base error for milestone controller failures."""


class MilestoneBaseError(MilestoneError):
    """Raised when the requested base cannot be proven safe."""


class MilestoneStateError(MilestoneError):
    """Raised when the persisted milestone state is missing or inconsistent."""


class MilestoneValidationError(MilestoneError):
    """Raised when validation cannot proceed or a required command fails."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-._")
    if not candidate:
        raise MilestoneError("milestone name must contain at least one path-safe character")
    return candidate[:128]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _shorten(text: str | None, limit: int = MAX_CAPTURED_TEXT) -> tuple[str, bool]:
    if not text:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _duration_ms(result: ProcessResult) -> int:
    try:
        started = datetime.fromisoformat(result.started_at)
        finished = datetime.fromisoformat(result.finished_at)
    except ValueError:
        return 0
    return max(int((finished - started).total_seconds() * 1000), 0)


def _is_full_sha(value: str) -> bool:
    return bool(_SHA_RE.fullmatch(value))


def _parse_name_status_z(raw: str) -> tuple["ChangedFile", ...]:
    parts = raw.split("\0")
    items: list[ChangedFile] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            continue
        if index >= len(parts):
            break
        code = status[0]
        if code in {"R", "C"}:
            if index + 1 >= len(parts):
                break
            old_path = parts[index]
            new_path = parts[index + 1]
            index += 2
            items.append(
                ChangedFile(
                    kind="renamed" if code == "R" else "copied",
                    path=new_path,
                    old_path=old_path,
                )
            )
            continue
        path = parts[index]
        index += 1
        kind = {
            "A": "added",
            "D": "deleted",
            "M": "modified",
            "T": "type_changed",
            "U": "unmerged",
        }.get(code, "modified")
        items.append(ChangedFile(kind=kind, path=path))
    return tuple(items)


def _split_lines(raw: str) -> tuple[str, ...]:
    return tuple(line for line in (item.strip() for item in raw.splitlines()) if line)


def _boundary_paths(boundary: "ChangedFileBoundary") -> tuple[str, ...]:
    paths = {
        item.path for item in boundary.committed_changes + boundary.staged_changes + boundary.unstaged_changes
    }
    paths.update(boundary.untracked_files)
    return tuple(sorted(paths))


def _combine_messages(messages: Iterable[str]) -> str:
    return "; ".join(message for message in messages if message)


@dataclass(frozen=True, slots=True)
class ChangedFile:
    kind: str
    path: str
    old_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ChangedFile":
        return cls(
            kind=str(payload["kind"]),
            path=str(payload["path"]),
            old_path=payload.get("old_path"),
        )


@dataclass(frozen=True, slots=True)
class ChangedFileBoundary:
    base_ref: str
    base_commit: str
    head_commit: str
    committed_changes: tuple[ChangedFile, ...]
    staged_changes: tuple[ChangedFile, ...]
    unstaged_changes: tuple[ChangedFile, ...]
    untracked_files: tuple[str, ...]
    committed_diffstat: str
    staged_diffstat: str
    unstaged_diffstat: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_ref": self.base_ref,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "committed_changes": [item.to_dict() for item in self.committed_changes],
            "staged_changes": [item.to_dict() for item in self.staged_changes],
            "unstaged_changes": [item.to_dict() for item in self.unstaged_changes],
            "untracked_files": list(self.untracked_files),
            "committed_diffstat": self.committed_diffstat,
            "staged_diffstat": self.staged_diffstat,
            "unstaged_diffstat": self.unstaged_diffstat,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ChangedFileBoundary":
        return cls(
            base_ref=str(payload["base_ref"]),
            base_commit=str(payload["base_commit"]),
            head_commit=str(payload["head_commit"]),
            committed_changes=tuple(
                ChangedFile.from_dict(item) for item in payload.get("committed_changes", [])
            ),
            staged_changes=tuple(
                ChangedFile.from_dict(item) for item in payload.get("staged_changes", [])
            ),
            unstaged_changes=tuple(
                ChangedFile.from_dict(item) for item in payload.get("unstaged_changes", [])
            ),
            untracked_files=tuple(str(item) for item in payload.get("untracked_files", [])),
            committed_diffstat=str(payload.get("committed_diffstat", "")),
            staged_diffstat=str(payload.get("staged_diffstat", "")),
            unstaged_diffstat=str(payload.get("unstaged_diffstat", "")),
        )

    @property
    def all_paths(self) -> tuple[str, ...]:
        return _boundary_paths(self)


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    started_at: str
    finished_at: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_process(cls, argv: Iterable[str], cwd: Path | str, result: ProcessResult) -> "CommandResult":
        stdout, stdout_truncated = _shorten(result.stdout)
        stderr, stderr_truncated = _shorten(result.stderr)
        return cls(
            argv=tuple(argv),
            cwd=os.fspath(cwd),
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            timed_out=result.timed_out,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=_duration_ms(result),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CommandResult":
        return cls(
            argv=tuple(str(item) for item in payload.get("argv", [])),
            cwd=str(payload["cwd"]),
            exit_code=payload.get("exit_code"),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            stdout_truncated=bool(payload.get("stdout_truncated", False)),
            stderr_truncated=bool(payload.get("stderr_truncated", False)),
            timed_out=bool(payload.get("timed_out", False)),
            started_at=str(payload.get("started_at", "")),
            finished_at=str(payload.get("finished_at", "")),
            duration_ms=int(payload.get("duration_ms", 0)),
        )


@dataclass(frozen=True, slots=True)
class ValidationPhase:
    name: str
    kind: str
    status: str
    exit_code: int | None
    commands: tuple[CommandResult, ...]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "exit_code": self.exit_code,
            "commands": [item.to_dict() for item in self.commands],
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ValidationPhase":
        return cls(
            name=str(payload["name"]),
            kind=str(payload.get("kind", "required")),
            status=str(payload.get("status", "skipped")),
            exit_code=payload.get("exit_code"),
            commands=tuple(
                CommandResult.from_dict(item) for item in payload.get("commands", [])
            ),
            summary=str(payload.get("summary", "")),
        )

    @property
    def timed_out(self) -> bool:
        return any(command.timed_out for command in self.commands)


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    started_at: str
    finished_at: str
    overall_exit_code: int
    phases: tuple[ValidationPhase, ...]
    completed_phases: tuple[str, ...]
    remaining_phases: tuple[str, ...]
    failures: tuple[str, ...]
    blockers: tuple[str, ...]
    final_git_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "overall_exit_code": self.overall_exit_code,
            "phases": [item.to_dict() for item in self.phases],
            "completed_phases": list(self.completed_phases),
            "remaining_phases": list(self.remaining_phases),
            "failures": list(self.failures),
            "blockers": list(self.blockers),
            "final_git_status": self.final_git_status,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ValidationSummary":
        return cls(
            started_at=str(payload["started_at"]),
            finished_at=str(payload["finished_at"]),
            overall_exit_code=int(payload.get("overall_exit_code", 1)),
            phases=tuple(
                ValidationPhase.from_dict(item) for item in payload.get("phases", [])
            ),
            completed_phases=tuple(
                str(item) for item in payload.get("completed_phases", [])
            ),
            remaining_phases=tuple(
                str(item) for item in payload.get("remaining_phases", [])
            ),
            failures=tuple(str(item) for item in payload.get("failures", [])),
            blockers=tuple(str(item) for item in payload.get("blockers", [])),
            final_git_status=str(payload.get("final_git_status", "")),
        )

    @property
    def success(self) -> bool:
        return self.overall_exit_code == 0 and not self.failures and not self.blockers


@dataclass(frozen=True, slots=True)
class MilestoneState:
    milestone_id: str
    name: str
    implementation_area: str
    repository_root: str
    base_ref: str
    canonical_base_ref: str | None
    canonical_base_commit: str | None
    current_base: str | None
    current_head: str | None
    worktree: str | None
    branch: str | None
    status: str
    created_at: str
    updated_at: str
    focused_tests: tuple[tuple[str, ...], ...]
    invariants: tuple[str, ...]
    scope_paths: tuple[str, ...]
    what_changed: tuple[str, ...]
    validation_completed: tuple[str, ...]
    validation_remaining: tuple[str, ...]
    failures: tuple[str, ...]
    blockers: tuple[str, ...]
    untracked_files: tuple[str, ...]
    next_safe_action: str
    boundary: ChangedFileBoundary | None = None
    validation: ValidationSummary | None = None
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "name": self.name,
            "implementation_area": self.implementation_area,
            "repository_root": self.repository_root,
            "base_ref": self.base_ref,
            "canonical_base_ref": self.canonical_base_ref,
            "canonical_base_commit": self.canonical_base_commit,
            "current_base": self.current_base,
            "current_head": self.current_head,
            "worktree": self.worktree,
            "branch": self.branch,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "focused_tests": [list(item) for item in self.focused_tests],
            "invariants": list(self.invariants),
            "scope_paths": list(self.scope_paths),
            "what_changed": list(self.what_changed),
            "validation_completed": list(self.validation_completed),
            "validation_remaining": list(self.validation_remaining),
            "failures": list(self.failures),
            "blockers": list(self.blockers),
            "untracked_files": list(self.untracked_files),
            "next_safe_action": self.next_safe_action,
            "boundary": self.boundary.to_dict() if self.boundary else None,
            "validation": self.validation.to_dict() if self.validation else None,
            "artifacts": dict(self.artifacts),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MilestoneState":
        boundary_payload = payload.get("boundary")
        validation_payload = payload.get("validation")
        return cls(
            milestone_id=str(payload["milestone_id"]),
            name=str(payload.get("name", "")),
            implementation_area=str(payload.get("implementation_area", "")),
            repository_root=str(payload.get("repository_root", "")),
            base_ref=str(payload.get("base_ref", "HEAD")),
            canonical_base_ref=payload.get("canonical_base_ref"),
            canonical_base_commit=payload.get("canonical_base_commit"),
            current_base=payload.get("current_base"),
            current_head=payload.get("current_head"),
            worktree=payload.get("worktree"),
            branch=payload.get("branch"),
            status=str(payload.get("status", "created")),
            created_at=str(payload.get("created_at", _utcnow())),
            updated_at=str(payload.get("updated_at", _utcnow())),
            focused_tests=tuple(
                tuple(str(part) for part in item) for item in payload.get("focused_tests", [])
            ),
            invariants=tuple(str(item) for item in payload.get("invariants", [])),
            scope_paths=tuple(str(item) for item in payload.get("scope_paths", [])),
            what_changed=tuple(str(item) for item in payload.get("what_changed", [])),
            validation_completed=tuple(
                str(item) for item in payload.get("validation_completed", [])
            ),
            validation_remaining=tuple(
                str(item) for item in payload.get("validation_remaining", [])
            ),
            failures=tuple(str(item) for item in payload.get("failures", [])),
            blockers=tuple(str(item) for item in payload.get("blockers", [])),
            untracked_files=tuple(str(item) for item in payload.get("untracked_files", [])),
            next_safe_action=str(payload.get("next_safe_action", "run validation")),
            boundary=(
                ChangedFileBoundary.from_dict(boundary_payload)
                if isinstance(boundary_payload, dict)
                else None
            ),
            validation=(
                ValidationSummary.from_dict(validation_payload)
                if isinstance(validation_payload, dict)
                else None
            ),
            artifacts=dict(payload.get("artifacts", {})),
        )


@dataclass(frozen=True, slots=True)
class MilestoneRequest:
    milestone_id: str
    name: str
    base_ref: str
    implementation_area: str
    focused_tests: tuple[tuple[str, ...], ...] = ()
    invariants: tuple[str, ...] = ()
    scope_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "name": self.name,
            "base_ref": self.base_ref,
            "implementation_area": self.implementation_area,
            "focused_tests": [list(item) for item in self.focused_tests],
            "invariants": list(self.invariants),
            "scope_paths": list(self.scope_paths),
        }


def _normalize_focused_tests(values: Iterable[Iterable[str]] | None) -> tuple[tuple[str, ...], ...]:
    normalized: list[tuple[str, ...]] = []
    for value in values or ():
        command = tuple(str(part) for part in value if str(part))
        if command:
            normalized.append(command)
    return tuple(normalized)


def _normalize_paths(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(str(value) for value in values or () if str(value))


class MilestoneController:
    def __init__(
        self,
        *,
        config: ControllerConfig,
        runner: LocalRunner | None = None,
        worktrees: WorktreeManager | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or LocalRunner()
        self.worktrees = worktrees or WorktreeManager(
            self.runner,
            config.repository_path,
            config.worktree_root,
            lock_root=config.controller_root,
        )

    @classmethod
    def build_for_tests(
        cls,
        *,
        config: ControllerConfig,
        runner: LocalRunner | None = None,
    ) -> "MilestoneController":
        return cls(config=config, runner=runner)

    @property
    def repo_root(self) -> Path:
        return Path(self.config.repository_path).resolve()

    @property
    def milestones_root(self) -> Path:
        return self.config.controller_root / MILESTONE_ROOT_DIR

    @property
    def validation_lock_path(self) -> Path:
        return self.config.controller_root / ".validation.lock"

    def create(
        self,
        *,
        name: str,
        base_ref: str,
        implementation_area: str,
        milestone_id: str | None = None,
        focused_tests: Iterable[Iterable[str]] | None = None,
        invariants: Iterable[str] | None = None,
        scope_paths: Iterable[str] | None = None,
    ) -> MilestoneState:
        slug = milestone_id or _slugify(name)
        request = MilestoneRequest(
            milestone_id=slug,
            name=name,
            base_ref=base_ref,
            implementation_area=implementation_area,
            focused_tests=_normalize_focused_tests(focused_tests),
            invariants=_normalize_paths(invariants),
            scope_paths=_normalize_paths(scope_paths),
        )
        state_dir = self._milestone_dir(slug)
        state_path = state_dir / STATE_FILENAME
        state_dir.mkdir(parents=True, exist_ok=True)

        with FileLock(self._state_lock_path(slug)):
            existing = self._load_state_if_any(state_path)
            if existing is not None:
                if not self._request_matches_state(request, existing):
                    raise MilestoneStateError(
                        f"milestone {slug!r} already exists with different inputs"
                    )
                if existing.status != "creating" and existing.worktree and Path(existing.worktree).exists():
                    return existing

            provisional = MilestoneState(
                milestone_id=slug,
                name=name,
                implementation_area=implementation_area,
                repository_root=os.fspath(self.repo_root),
                base_ref=base_ref,
                canonical_base_ref=None,
                canonical_base_commit=None,
                current_base=None,
                current_head=None,
                worktree=None,
                branch=None,
                status="creating",
                created_at=_utcnow(),
                updated_at=_utcnow(),
                focused_tests=request.focused_tests,
                invariants=request.invariants,
                scope_paths=request.scope_paths,
                what_changed=(),
                validation_completed=(),
                validation_remaining=FULL_VALIDATION_PHASES,
                failures=(),
                blockers=(),
                untracked_files=(),
                next_safe_action="create isolated worktree",
                artifacts={},
            )
            self._save_state(slug, provisional)

        canonical = self.resolve_canonical_base()
        base_commit = self._resolve_base_commit(base_ref)
        self._verify_base_safety(base_ref=base_ref, base_commit=base_commit, canonical=canonical)
        task = Task(
            id=slug,
            title=name,
            prompt=implementation_area or name,
            base_ref=base_ref,
            tests=[],
            metadata={
                "milestone_id": slug,
                "implementation_area": implementation_area,
            },
        )
        info = self.worktrees.create(task)
        head_commit = self._git(
            ["rev-parse", "HEAD"],
            cwd=info.path,
            timeout_seconds=60,
        ).stdout
        boundary = self.capture_boundary(info.path, base_commit)
        state = MilestoneState(
            milestone_id=slug,
            name=name,
            implementation_area=implementation_area,
            repository_root=os.fspath(self.repo_root),
            base_ref=base_ref,
            canonical_base_ref=canonical["ref"] if canonical else None,
            canonical_base_commit=canonical["commit"] if canonical else None,
            current_base=base_commit,
            current_head=head_commit,
            worktree=os.fspath(info.path),
            branch=info.branch,
            status="created",
            created_at=provisional.created_at,
            updated_at=_utcnow(),
            focused_tests=request.focused_tests,
            invariants=request.invariants,
            scope_paths=request.scope_paths,
            what_changed=boundary.all_paths,
            validation_completed=(),
            validation_remaining=FULL_VALIDATION_PHASES,
            failures=(),
            blockers=(),
            untracked_files=boundary.untracked_files,
            next_safe_action="run milestone validate",
            boundary=boundary,
            artifacts={},
        )
        with FileLock(self._state_lock_path(slug)):
            self._save_state(slug, state)
        return state

    def status(self, milestone_id: str | None = None) -> dict[str, Any]:
        inventory = self._inventory()
        if milestone_id is None:
            milestones = [self._state_view(state) for state in self._all_states()]
            return {"inventory": inventory, "milestones": milestones}
        state = self.load_state(milestone_id)
        live_boundary = None
        if state.worktree and state.current_base and Path(state.worktree).exists():
            try:
                live_boundary = self.capture_boundary(Path(state.worktree), state.current_base)
            except MilestoneError:
                live_boundary = state.boundary
        milestone_view = self._state_view(state, live_boundary=live_boundary)
        ancestry = self._milestone_ancestry(state)
        return {
            "inventory": inventory,
            "milestone": milestone_view,
            "ancestry": ancestry,
        }

    def validate(self, milestone_id: str) -> ValidationSummary:
        state = self.load_state(milestone_id)
        if not state.worktree or not state.current_base:
            raise MilestoneStateError(f"milestone {milestone_id!r} does not have an active worktree")
        worktree = Path(state.worktree)
        if not worktree.exists():
            raise MilestoneStateError(f"milestone worktree does not exist: {worktree}")

        with FileLock(self.validation_lock_path):
            boundary = self.capture_boundary(worktree, state.current_base)
            adjacency = self.discover_adjacent_tests(boundary, state.scope_paths)
            phase_results: list[ValidationPhase] = []
            failures: list[str] = []
            blockers: list[str] = []
            completed_phases: list[str] = []
            remaining_phases: list[str] = list(FULL_VALIDATION_PHASES)
            overall_exit_code = 0
            started_at = _utcnow()
            current_validation: ValidationSummary | None = None
            current_state = replace(
                state,
                status="validating",
                updated_at=_utcnow(),
                boundary=boundary,
                what_changed=boundary.all_paths,
                untracked_files=boundary.untracked_files,
                validation_completed=tuple(completed_phases),
                validation_remaining=tuple(remaining_phases),
                next_safe_action="validation in progress",
            )
            self._save_state(milestone_id, current_state)

            phase_results.append(
                self._run_explicit_tests(
                    phase_name="focused_tests",
                    state=state,
                    worktree=worktree,
                    commands=state.focused_tests,
                    kind="required",
                )
            )
            phase_results.append(
                self._run_adjacency_tests(
                    worktree=worktree,
                    adjacency=adjacency,
                )
            )
            phase_results.append(self._run_full_suite(worktree=worktree))
            phase_results.append(self._run_compileall())
            phase_results.append(self._run_git_diff_check(worktree=worktree))
            phase_results.append(self._run_staged_diff_check(worktree=worktree))
            final_status_phase = self._run_final_git_status(worktree=worktree)
            phase_results.append(final_status_phase)

            for phase in phase_results:
                completed_phases.append(phase.name)
                remaining_phases = [name for name in FULL_VALIDATION_PHASES if name not in completed_phases]
                if phase.status == "failed":
                    message = _combine_messages(
                        [
                            f"{phase.kind} phase {phase.name} failed",
                            phase.summary,
                        ]
                    )
                    failures.append(message)
                    blockers.append(message)
                    if overall_exit_code == 0 and phase.exit_code not in (None, 0):
                        overall_exit_code = int(phase.exit_code)
                    elif overall_exit_code == 0 and phase.exit_code is None:
                        overall_exit_code = 124
                elif phase.status == "skipped" and phase.kind == "required":
                    blockers.append(f"required phase {phase.name} was not executed")
                if phase.exit_code not in (None, 0) and overall_exit_code == 0 and phase.status != "skipped":
                    overall_exit_code = int(phase.exit_code)

                current_validation = ValidationSummary(
                    started_at=started_at,
                    finished_at=_utcnow(),
                    overall_exit_code=overall_exit_code,
                    phases=tuple(phase_results[: len(completed_phases)]),
                    completed_phases=tuple(completed_phases),
                    remaining_phases=tuple(remaining_phases),
                    failures=tuple(failures),
                    blockers=tuple(blockers),
                    final_git_status=final_status_phase.commands[0].stdout if final_status_phase.commands else "",
                )
                current_state = replace(
                    state,
                    status="validating",
                    updated_at=_utcnow(),
                    boundary=boundary,
                    what_changed=boundary.all_paths,
                    untracked_files=boundary.untracked_files,
                    validation_completed=tuple(completed_phases),
                    validation_remaining=tuple(remaining_phases),
                    failures=tuple(failures),
                    blockers=tuple(blockers),
                    next_safe_action="inspect failures and rerun validation" if blockers else "continue to evidence and human review",
                    validation=current_validation,
                )
                self._save_state(milestone_id, current_state)

            if current_validation is None:
                raise MilestoneValidationError("validation did not run any phases")

            final_status = "ready_for_review" if not blockers else "blocked"
            final_next_action = (
                "stop at the human review / commit gate"
                if not blockers
                else "fix blockers in the worktree and rerun validation"
            )
            final_state = replace(
                current_state,
                status=final_status,
                updated_at=_utcnow(),
                next_safe_action=final_next_action,
                validation=current_validation,
            )
            self._save_state(milestone_id, final_state)
            return current_validation

    def evidence(self, milestone_id: str, output_dir: Path | None = None) -> dict[str, Any]:
        state = self.load_state(milestone_id)
        bundle_dir = output_dir or self._milestone_dir(milestone_id)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(self.validation_lock_path):
            inventory = self._inventory()
            live_boundary = None
            if state.worktree and state.current_base and Path(state.worktree).exists():
                live_boundary = self.capture_boundary(Path(state.worktree), state.current_base)
            validation = state.validation.to_dict() if state.validation else None
            completed_phases = list(state.validation_completed)
            remaining_phases = list(state.validation_remaining)
            not_validated = []
            if not state.focused_tests:
                not_validated.append("No explicit focused tests were supplied.")
            if not state.validation:
                not_validated.append("Validation has not been run yet.")
            elif state.validation.remaining_phases:
                not_validated.append(
                    "Validation did not reach the end of the phase list."
                )
            not_validated.extend(
                [
                    "No commit, merge, rebase, or push was performed by the controller.",
                    "No production or external deployment was validated.",
                ]
            )
            evidence_payload = {
                "timestamp": _utcnow(),
                "repository": inventory,
                "milestone": self._state_view(state, live_boundary=live_boundary),
                "boundary": live_boundary.to_dict() if live_boundary else None,
                "validation": validation,
                "commands_executed": self._commands_for_state(state),
                "failures": list(state.failures),
                "blockers": list(state.blockers),
                "validation_completed": completed_phases,
                "validation_remaining": remaining_phases,
                "not_validated": not_validated,
            }
            report_payload = dict(evidence_payload)
            report_milestone = dict(report_payload["milestone"])
            report_milestone["status"] = (
                "blocked" if report_milestone.get("blockers") else "ready_for_review"
            )
            report_milestone["next_safe_action"] = (
                "fix blockers in the worktree and rerun validation"
                if report_milestone.get("blockers")
                else "prepare human review packet and stop at the commit gate"
            )
            report_payload["milestone"] = report_milestone
            report_md = self._render_report_md(report_payload)
            report_path = bundle_dir / REPORT_FILENAME
            report_path.write_text(report_md, encoding="utf-8")
            report_sha256 = _sha256_text(report_md)
            evidence_payload["report_sha256"] = report_sha256
            evidence_path = bundle_dir / EVIDENCE_FILENAME
            atomic_json(evidence_path, evidence_payload)
            evidence_sha256 = _sha256_text(evidence_path.read_text(encoding="utf-8"))
            evidence_payload["evidence_sha256"] = evidence_sha256
            state = replace(
                state,
                status="evidence_ready" if state.validation else state.status,
                updated_at=_utcnow(),
                next_safe_action="prepare human review packet and stop at the commit gate",
                validation=state.validation,
                boundary=live_boundary or state.boundary,
                what_changed=(live_boundary or state.boundary).all_paths if (live_boundary or state.boundary) else state.what_changed,
                untracked_files=(live_boundary or state.boundary).untracked_files if (live_boundary or state.boundary) else state.untracked_files,
                artifacts={
                    **state.artifacts,
                    "report_sha256": report_sha256,
                    "evidence_sha256": evidence_sha256,
                    "evidence_json": os.fspath(evidence_path),
                    "report_md": os.fspath(report_path),
                },
            )
            self._save_state(milestone_id, state)
            return evidence_payload

    def handoff(self, milestone_id: str, output_path: Path | None = None) -> dict[str, Any]:
        evidence = self.evidence(milestone_id)
        state = self.load_state(milestone_id)
        packet = {
            "milestone_objective": state.name,
            "implementation_area": state.implementation_area,
            "invariants": list(state.invariants),
            "base": {
                "requested": state.base_ref,
                "resolved": state.current_base,
                "canonical": {
                    "ref": state.canonical_base_ref,
                    "commit": state.canonical_base_commit,
                },
            },
            "worktree": {
                "repository": state.repository_root,
                "worktree": state.worktree,
                "branch": state.branch,
                "head": state.current_head,
            },
            "boundary": evidence.get("boundary"),
            "implementation_summary": self._implementation_summary(state, evidence),
            "tests_executed": evidence.get("commands_executed", []),
            "known_risks": list(state.blockers),
            "prohibited_changes": [
                "No automatic commit",
                "No push",
                "No merge into canonical",
                "No rebase of accepted history",
                "No destructive git clean/reset/restore",
                "No widening of milestone scope without human approval",
            ],
            "unresolved_questions": self._unresolved_questions(state, evidence),
            "verification_commands": self._verification_commands(state),
            "next_safe_action": state.next_safe_action,
            "report_sha256": state.artifacts.get("report_sha256") or evidence.get("report_sha256"),
            "evidence_sha256": evidence.get("evidence_sha256"),
        }
        handoff_text = self._render_handoff_md(packet)
        if output_path is None:
            output_path = self._milestone_dir(milestone_id) / HANDOFF_FILENAME
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(handoff_text, encoding="utf-8")
        state = replace(
            state,
            status="handoff_ready",
            updated_at=_utcnow(),
            next_safe_action="stop at the human acceptance / commit gate",
            artifacts={**state.artifacts, "handoff_md": os.fspath(output_path)},
        )
        self._save_state(milestone_id, state)
        packet["handoff_path"] = os.fspath(output_path)
        packet["handoff_sha256"] = _sha256_text(handoff_text)
        return packet

    def load_state(self, milestone_id: str) -> MilestoneState:
        state_path = self._milestone_dir(milestone_id) / STATE_FILENAME
        data = self._load_json(state_path)
        return MilestoneState.from_dict(data)

    def resolve_canonical_base(self) -> dict[str, str] | None:
        result = self._git(
            ["rev-parse", f"{CANONICAL_BASE_BRANCH}^{{commit}}"],
            cwd=self.repo_root,
            timeout_seconds=60,
            allow_fail=True,
        )
        if result.exit_code != 0:
            return None
        commit = result.stdout.strip()
        if not commit:
            return None
        return {"ref": CANONICAL_BASE_BRANCH, "commit": commit}

    def capture_boundary(self, worktree: Path, base_commit: str) -> ChangedFileBoundary:
        head_commit = self._git(["rev-parse", "HEAD"], cwd=worktree, timeout_seconds=60).stdout
        committed = _parse_name_status_z(
            self._git(
                ["diff", "--name-status", "-z", "--find-renames", base_commit, "HEAD", "--"],
                cwd=worktree,
                timeout_seconds=120,
            ).stdout
        )
        staged = _parse_name_status_z(
            self._git(
                ["diff", "--cached", "--name-status", "-z", "--find-renames", "--"],
                cwd=worktree,
                timeout_seconds=120,
            ).stdout
        )
        unstaged = _parse_name_status_z(
            self._git(
                ["diff", "--name-status", "-z", "--find-renames", "--"],
                cwd=worktree,
                timeout_seconds=120,
            ).stdout
        )
        untracked_raw = self._git(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=worktree,
            timeout_seconds=120,
        ).stdout
        untracked_files = tuple(
            path for path in untracked_raw.split("\0") if path and path != WorktreeManager.MARKER
        )
        committed_diffstat = self._git(
            ["diff", "--stat", base_commit, "HEAD", "--"],
            cwd=worktree,
            timeout_seconds=120,
        ).stdout
        staged_diffstat = self._git(
            ["diff", "--stat", "--cached", "--"],
            cwd=worktree,
            timeout_seconds=120,
        ).stdout
        unstaged_diffstat = self._git(
            ["diff", "--stat", "--"],
            cwd=worktree,
            timeout_seconds=120,
        ).stdout
        return ChangedFileBoundary(
            base_ref=base_commit,
            base_commit=base_commit,
            head_commit=head_commit,
            committed_changes=committed,
            staged_changes=staged,
            unstaged_changes=unstaged,
            untracked_files=untracked_files,
            committed_diffstat=committed_diffstat,
            staged_diffstat=staged_diffstat,
            unstaged_diffstat=unstaged_diffstat,
        )

    def discover_adjacent_tests(
        self,
        boundary: ChangedFileBoundary,
        scope_paths: Iterable[str] | tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        repo = self.repo_root
        discovered: set[str] = set()
        candidate_test_files = self._candidate_test_files()
        changed_paths = list(boundary.all_paths)
        for path in changed_paths:
            candidate_stems = self._test_stems_for_path(path)
            if not candidate_stems:
                continue
            for test_path in candidate_test_files:
                test_text = test_path.read_text(encoding="utf-8", errors="replace")
                if any(stem in test_text for stem in candidate_stems):
                    discovered.add(os.fspath(test_path.relative_to(repo)))
                    continue
                if any(test_path.name.endswith(f"{stem}.py") for stem in candidate_stems):
                    discovered.add(os.fspath(test_path.relative_to(repo)))
        if scope_paths:
            scope_prefixes = tuple(str(item).strip().strip("/") for item in scope_paths if str(item).strip())
            if scope_prefixes:
                discovered = {
                    item
                    for item in discovered
                    if any(
                        item == prefix
                        or item.startswith(f"{prefix}/")
                        for prefix in scope_prefixes
                    )
                }
        return tuple(sorted(discovered))

    def _candidate_test_files(self) -> tuple[Path, ...]:
        files: list[Path] = []
        for root in (self.repo_root / "tests", self.repo_root / "tools" / "ai_controller" / "tests"):
            if root.exists():
                files.extend(sorted(root.rglob("test_*.py")))
                files.extend(sorted(root.rglob("*_test.py")))
        return tuple(dict.fromkeys(files))

    def _test_stems_for_path(self, path: str) -> tuple[str, ...]:
        path_obj = Path(path)
        if path_obj.name.startswith("test_") or path_obj.name.endswith("_test.py"):
            return ()
        stems = {path_obj.stem}
        if path_obj.suffix == ".py":
            stems.add(f"test_{path_obj.stem}")
            stems.add(path_obj.stem.replace("_", "-"))
        return tuple(sorted(stems))

    def _run_explicit_tests(
        self,
        *,
        phase_name: str,
        state: MilestoneState,
        worktree: Path,
        commands: Iterable[Iterable[str]],
        kind: str,
    ) -> ValidationPhase:
        command_results: list[CommandResult] = []
        summary_parts: list[str] = []
        exit_code: int | None = 0
        status = "succeeded"
        for command in commands:
            argv = tuple(str(part) for part in command)
            if not argv:
                continue
            result = self._run_command(argv, cwd=worktree, timeout_seconds=self.config.test_timeout_seconds)
            command_results.append(result)
            summary_parts.append(
                f"{' '.join(argv)} => exit={result.exit_code} timed_out={result.timed_out}"
            )
            if result.exit_code is None or result.exit_code != 0 or result.timed_out:
                status = "failed"
                exit_code = result.exit_code if result.exit_code is not None else 124
                break
        if not command_results:
            return ValidationPhase(
                name=phase_name,
                kind=kind,
                status="skipped",
                exit_code=None,
                commands=(),
                summary="no commands were configured",
            )
        if status == "succeeded":
            exit_code = 0
        return ValidationPhase(
            name=phase_name,
            kind=kind,
            status=status,
            exit_code=exit_code,
            commands=tuple(command_results),
            summary=_combine_messages(summary_parts),
        )

    def _run_adjacency_tests(self, *, worktree: Path, adjacency: tuple[str, ...]) -> ValidationPhase:
        if not adjacency:
            return ValidationPhase(
                name="adjacent_tests",
                kind="heuristic",
                status="skipped",
                exit_code=None,
                commands=(),
                summary="no adjacent tests discovered",
            )
        result = self._run_command(
            self._pytest_command("-q", *adjacency),
            cwd=worktree,
            timeout_seconds=self.config.test_timeout_seconds,
        )
        status = "succeeded" if result.exit_code == 0 and not result.timed_out else "failed"
        exit_code = result.exit_code if result.exit_code is not None else 124
        return ValidationPhase(
            name="adjacent_tests",
            kind="heuristic",
            status=status,
            exit_code=exit_code,
            commands=(result,),
            summary=f"pytest -q {' '.join(adjacency)} => exit={result.exit_code} timed_out={result.timed_out}",
        )

    def _run_full_suite(self, *, worktree: Path) -> ValidationPhase:
        result = self._run_command(
            self._pytest_command("-q"),
            cwd=worktree,
            timeout_seconds=self.config.test_timeout_seconds,
        )
        status = "succeeded" if result.exit_code == 0 and not result.timed_out else "failed"
        exit_code = result.exit_code if result.exit_code is not None else 124
        return ValidationPhase(
            name="full_suite",
            kind="required",
            status=status,
            exit_code=exit_code,
            commands=(result,),
            summary=f"pytest -q => exit={result.exit_code} timed_out={result.timed_out}",
        )

    def _run_compileall(self) -> ValidationPhase:
        paths = self._python_compile_targets()
        if not paths:
            return ValidationPhase(
                name="compileall",
                kind="maintenance",
                status="skipped",
                exit_code=None,
                commands=(),
                summary="no python compile targets found",
            )
        result = self._run_command(
            (self._python_executable(), "-m", "compileall", "-q", *paths),
            cwd=self.repo_root,
            timeout_seconds=max(self.config.test_timeout_seconds / 3.0, 30.0),
        )
        status = "succeeded" if result.exit_code == 0 and not result.timed_out else "failed"
        exit_code = result.exit_code if result.exit_code is not None else 124
        return ValidationPhase(
            name="compileall",
            kind="maintenance",
            status=status,
            exit_code=exit_code,
            commands=(result,),
            summary=f"python -m compileall -q {' '.join(paths)} => exit={result.exit_code} timed_out={result.timed_out}",
        )

    def _run_git_diff_check(self, *, worktree: Path) -> ValidationPhase:
        result = self._run_command(
            ("git", "diff", "--check"),
            cwd=worktree,
            timeout_seconds=120,
        )
        status = "succeeded" if result.exit_code == 0 and not result.timed_out else "failed"
        exit_code = result.exit_code if result.exit_code is not None else 124
        return ValidationPhase(
            name="git_diff_check",
            kind="maintenance",
            status=status,
            exit_code=exit_code,
            commands=(result,),
            summary=f"git diff --check => exit={result.exit_code} timed_out={result.timed_out}",
        )

    def _run_staged_diff_check(self, *, worktree: Path) -> ValidationPhase:
        staged = self._git(["diff", "--cached", "--name-only"], cwd=worktree, timeout_seconds=60).stdout
        if not staged.strip():
            return ValidationPhase(
                name="staged_diff_check",
                kind="maintenance",
                status="skipped",
                exit_code=None,
                commands=(),
                summary="no staged changes present",
            )
        result = self._run_command(
            ("git", "diff", "--check", "--cached"),
            cwd=worktree,
            timeout_seconds=120,
        )
        status = "succeeded" if result.exit_code == 0 and not result.timed_out else "failed"
        exit_code = result.exit_code if result.exit_code is not None else 124
        return ValidationPhase(
            name="staged_diff_check",
            kind="maintenance",
            status=status,
            exit_code=exit_code,
            commands=(result,),
            summary=f"git diff --check --cached => exit={result.exit_code} timed_out={result.timed_out}",
        )

    def _run_final_git_status(self, *, worktree: Path) -> ValidationPhase:
        result = self._run_command(
            ("git", "status", "--short", "--branch", "--untracked-files=all"),
            cwd=worktree,
            timeout_seconds=60,
        )
        status = "succeeded" if result.exit_code == 0 and not result.timed_out else "failed"
        exit_code = result.exit_code if result.exit_code is not None else 124
        return ValidationPhase(
            name="final_git_status",
            kind="maintenance",
            status=status,
            exit_code=exit_code,
            commands=(result,),
            summary="git status captured",
        )

    def _python_compile_targets(self) -> tuple[str, ...]:
        candidates = [
            "app.py",
            "api",
            "analytics",
            "federation",
            "ingest",
            "intelligence",
            "models",
            "research_mission",
            "raghub_worker",
            "scripts",
            "sports",
            "tests",
            "tools",
            "web_ui.py",
        ]
        return tuple(target for target in candidates if (self.repo_root / target).exists())

    def _python_executable(self) -> str:
        return getattr(self.runner, "remote_python", sys.executable)

    def _pytest_command(self, *pytest_args: str) -> tuple[str, ...]:
        user_site_packages = site.getusersitepackages()
        script = (
            "import site, sys\n"
            f"site.addsitedir({user_site_packages!r})\n"
            "import pytest\n"
            "sys.exit(pytest.main(sys.argv[1:]))\n"
        )
        return (self._python_executable(), "-c", script, *pytest_args)

    def _commands_for_state(self, state: MilestoneState) -> list[dict[str, Any]]:
        if not state.validation:
            return []
        commands: list[dict[str, Any]] = []
        for phase in state.validation.phases:
            for command in phase.commands:
                payload = command.to_dict()
                payload["phase"] = phase.name
                payload["phase_kind"] = phase.kind
                payload["phase_status"] = phase.status
                commands.append(payload)
        return commands

    def _verification_commands(self, state: MilestoneState) -> list[str]:
        worktree = shlex.quote(os.fspath(state.worktree or self.repo_root))
        commands = [
            f"cd {worktree} && git status --short --branch --untracked-files=all",
            f"cd {worktree} && git diff --check",
            f"cd {worktree} && git diff --check --cached",
        ]
        return commands

    def _implementation_summary(self, state: MilestoneState, evidence: dict[str, Any]) -> str:
        validation = evidence.get("validation") or {}
        if not validation:
            return "Validation not yet executed."
        completed = ", ".join(validation.get("completed_phases", []))
        blockers = validation.get("blockers", [])
        if blockers:
            return f"Validation ran {completed}; blockers remain."
        return f"Validation ran {completed}; ready for human review."

    def _unresolved_questions(self, state: MilestoneState, evidence: dict[str, Any]) -> list[str]:
        questions: list[str] = []
        if not state.focused_tests:
            questions.append("No explicit focused tests were supplied at create time.")
        if state.validation and state.validation.remaining_phases:
            questions.append(
                "Validation did not complete every configured phase before the review gate."
            )
        if state.blockers:
            questions.append("Blockers remain in the milestone state.")
        return questions

    def _render_report_md(self, evidence: dict[str, Any]) -> str:
        milestone = evidence["milestone"]
        inventory = evidence["repository"]
        boundary = evidence.get("boundary") or {}
        validation = evidence.get("validation") or {}
        lines = [
            f"# Milestone Evidence: {milestone['name']}",
            "",
            f"- Milestone ID: `{milestone['milestone_id']}`",
            f"- Implementation area: {milestone['implementation_area']}",
            f"- Repository: `{inventory['repository_root']}`",
            f"- Worktree: `{milestone.get('worktree')}`",
            f"- Branch: `{milestone.get('branch')}`",
            f"- Base: `{milestone.get('base_ref')}` -> `{milestone.get('current_base')}`",
            f"- HEAD: `{milestone.get('current_head')}`",
            f"- Status: `{milestone.get('status')}`",
            "",
            "## Required inputs",
            "",
            f"- Focused tests: {len(milestone.get('focused_tests', []))}",
            f"- Invariants: {len(milestone.get('invariants', []))}",
            f"- Scope paths: {', '.join(milestone.get('scope_paths', [])) or 'none'}",
            "",
            "## Boundary",
            "",
            f"- Committed changes: {len(boundary.get('committed_changes', []))}",
            f"- Staged changes: {len(boundary.get('staged_changes', []))}",
            f"- Unstaged changes: {len(boundary.get('unstaged_changes', []))}",
            f"- Untracked files: {len(boundary.get('untracked_files', []))}",
            "",
            "## Validation",
            "",
            f"- Completed phases: {', '.join(validation.get('completed_phases', [])) or 'none'}",
            f"- Remaining phases: {', '.join(validation.get('remaining_phases', [])) or 'none'}",
            f"- Overall exit code: {validation.get('overall_exit_code')}",
            "",
            "## Known blockers",
            "",
        ]
        blockers = evidence.get("blockers") or []
        if blockers:
            lines.extend(f"- {blocker}" for blocker in blockers)
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "## Not validated",
                "",
            ]
        )
        for item in evidence.get("not_validated", []) or []:
            lines.append(f"- {item}")
        lines.extend(
            [
                "",
                "## Next safe action",
                "",
                f"{milestone.get('next_safe_action')}",
                "",
            ]
        )
        return "\n".join(lines)

    def _render_handoff_md(self, packet: dict[str, Any]) -> str:
        boundary = packet.get("boundary") or {}
        lines = [
            f"# Review Handoff: {packet['milestone_objective']}",
            "",
            f"- Implementation area: {packet['implementation_area']}",
            f"- Base: {packet['base']['requested']} -> {packet['base']['resolved']}",
            f"- Canonical base: {packet['base']['canonical'].get('ref')} @ {packet['base']['canonical'].get('commit')}",
            f"- Worktree: {packet['worktree']['worktree']}",
            f"- Branch: {packet['worktree']['branch']}",
            f"- HEAD: {packet['worktree']['head']}",
            "",
            "## Diff boundary",
            "",
            f"- Committed changes: {len(boundary.get('committed_changes', []))}",
            f"- Staged changes: {len(boundary.get('staged_changes', []))}",
            f"- Unstaged changes: {len(boundary.get('unstaged_changes', []))}",
            f"- Untracked files: {len(boundary.get('untracked_files', []))}",
            "",
            "## Summary",
            "",
            packet["implementation_summary"],
            "",
            "## Tests",
            "",
        ]
        tests = packet.get("tests_executed", [])
        if tests:
            for item in tests:
                lines.append(f"- {item.get('phase', 'command')}: {item.get('summary', '')}")
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "## Risks",
                "",
            ]
        )
        risks = packet.get("known_risks", [])
        if risks:
            for item in risks:
                lines.append(f"- {item}")
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "## Prohibited changes",
                "",
            ]
        )
        for item in packet.get("prohibited_changes", []):
            lines.append(f"- {item}")
        lines.extend(
            [
                "",
                "## Verification",
                "",
            ]
        )
        for item in packet.get("verification_commands", []):
            lines.append(f"- {item}")
        lines.extend(
            [
                "",
                "## Next safe action",
                "",
                f"{packet.get('next_safe_action', '')}",
                "",
            ]
        )
        return "\n".join(lines)

    def _run_command(
        self,
        argv: Iterable[str],
        *,
        cwd: Path | str,
        timeout_seconds: float,
    ) -> CommandResult:
        result = self.runner.run(list(argv), cwd=cwd, timeout_seconds=timeout_seconds)
        return CommandResult.from_process(argv, cwd, result)

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path | str,
        timeout_seconds: float,
        allow_fail: bool = False,
    ) -> ProcessResult:
        result = self.runner.run(["git", *args], cwd=cwd, timeout_seconds=timeout_seconds)
        if not allow_fail and result.exit_code not in (0, None):
            raise MilestoneError(
                result.stderr or result.stdout or f"git {' '.join(args)} failed"
            )
        return result

    def _resolve_base_commit(self, base_ref: str) -> str:
        try:
            result = self._git(
                ["rev-parse", f"{base_ref}^{{commit}}"],
                cwd=self.repo_root,
                timeout_seconds=60,
            )
        except MilestoneError as exc:
            raise MilestoneBaseError(
                f"unable to resolve base ref {base_ref!r}: {exc}"
            ) from exc
        if result.exit_code not in (0, None):
            raise MilestoneBaseError(
                result.stderr or result.stdout or f"unable to resolve base ref {base_ref!r}"
            )
        commit = result.stdout.strip()
        if not commit:
            raise MilestoneBaseError(f"base ref {base_ref!r} resolved to an empty commit hash")
        return commit

    def _verify_base_safety(
        self,
        *,
        base_ref: str,
        base_commit: str,
        canonical: dict[str, str] | None,
    ) -> None:
        repo_dirty = self._repo_is_dirty()
        if repo_dirty and not _is_full_sha(base_ref):
            raise MilestoneBaseError(
                "dirty repository requires an exact commit base; symbolic refs are ambiguous"
            )
        if canonical is not None:
            ancestor_check = self._git(
                ["merge-base", "--is-ancestor", canonical["commit"], base_commit],
                cwd=self.repo_root,
                timeout_seconds=60,
                allow_fail=True,
            )
            if ancestor_check.exit_code != 0:
                detail = ancestor_check.stderr or ancestor_check.stdout or (
                    "timed out" if ancestor_check.timed_out else "unknown ancestry failure"
                )
                raise MilestoneBaseError(
                    "requested base "
                    f"{base_commit} is not descended from accepted canonical base "
                    f"{canonical['commit']}: {detail}"
                )

    def _repo_is_dirty(self) -> bool:
        result = self._git(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=self.repo_root,
            timeout_seconds=60,
        )
        return bool(result.stdout.strip())

    def _inventory(self) -> dict[str, Any]:
        worktrees = self._worktree_inventory()
        branches = self._branch_inventory()
        canonical = self.resolve_canonical_base()
        current = worktrees[0] if worktrees else None
        return {
            "repository_root": os.fspath(self.repo_root),
            "current_branch": self._git(
                ["branch", "--show-current"],
                cwd=self.repo_root,
                timeout_seconds=60,
            ).stdout.strip() or None,
            "current_head": self._git(
                ["rev-parse", "HEAD"],
                cwd=self.repo_root,
                timeout_seconds=60,
            ).stdout.strip(),
            "clean": not self._repo_is_dirty(),
            "canonical_base": canonical,
            "worktrees": worktrees,
            "branches": branches,
            "milestones": [self._state_view(state) for state in self._all_states()],
        }

    def _worktree_inventory(self) -> list[dict[str, Any]]:
        raw = self._git(
            ["worktree", "list", "--porcelain"],
            cwd=self.repo_root,
            timeout_seconds=120,
        ).stdout
        entries: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for line in raw.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                current["worktree"] = value.strip()
            elif key == "HEAD":
                current["head"] = value.strip()
            elif key == "branch":
                current["branch"] = value.strip().removeprefix("refs/heads/")
            elif key == "detached":
                current["branch"] = "detached"
        if current:
            entries.append(current)
        for entry in entries:
            path = Path(entry["worktree"])
            status = self._git(
                ["status", "--porcelain", "--untracked-files=all"],
                cwd=path,
                timeout_seconds=60,
            )
            entry["clean"] = not bool(status.stdout.strip())
            entry["status"] = status.stdout.strip().splitlines()
        return entries

    def _branch_inventory(self) -> list[dict[str, Any]]:
        result = self._git(
            ["show-ref", "--heads"],
            cwd=self.repo_root,
            timeout_seconds=60,
        )
        items: list[dict[str, Any]] = []
        for line in _split_lines(result.stdout):
            commit, ref = line.split(" ", 1)
            items.append(
                {
                    "branch": ref.removeprefix("refs/heads/"),
                    "commit": commit,
                }
            )
        return items

    def _all_states(self) -> list[MilestoneState]:
        root = self.milestones_root
        if not root.exists():
            return []
        states: list[MilestoneState] = []
        for state_path in sorted(root.glob(f"*/{STATE_FILENAME}")):
            try:
                states.append(MilestoneState.from_dict(self._load_json(state_path)))
            except Exception:
                continue
        return states

    def _state_view(
        self,
        state: MilestoneState,
        *,
        live_boundary: ChangedFileBoundary | None = None,
    ) -> dict[str, Any]:
        boundary = live_boundary or state.boundary
        ancestry = self._milestone_ancestry(state)
        return {
            "milestone_id": state.milestone_id,
            "name": state.name,
            "implementation_area": state.implementation_area,
            "base_ref": state.base_ref,
            "canonical_base_ref": state.canonical_base_ref,
            "canonical_base_commit": state.canonical_base_commit,
            "current_base": state.current_base,
            "current_head": state.current_head,
            "worktree": state.worktree,
            "branch": state.branch,
            "status": state.status,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "focused_tests": [list(item) for item in state.focused_tests],
            "invariants": list(state.invariants),
            "scope_paths": list(state.scope_paths),
            "what_changed": list(boundary.all_paths) if boundary else list(state.what_changed),
            "validation_completed": list(state.validation_completed),
            "validation_remaining": list(state.validation_remaining),
            "failures": list(state.failures),
            "blockers": list(state.blockers),
            "untracked_files": list(boundary.untracked_files) if boundary else list(state.untracked_files),
            "next_safe_action": state.next_safe_action,
            "artifacts": dict(state.artifacts),
            "ancestry": ancestry,
        }

    def _milestone_ancestry(self, state: MilestoneState) -> dict[str, Any]:
        canonical = state.canonical_base_commit
        current_base = state.current_base
        if not canonical or not current_base:
            return {
                "canonical_known": bool(canonical),
                "base_descends_from_canonical": None,
                "head_descends_from_base": None,
            }
        base_result = self._git(
            ["merge-base", "--is-ancestor", canonical, current_base],
            cwd=self.repo_root,
            timeout_seconds=60,
            allow_fail=True,
        )
        if base_result.exit_code == 0:
            base_descends: bool | None = True
        elif base_result.exit_code is None:
            base_descends = None
        else:
            base_descends = False
        head_descends: bool | None = True
        if state.current_head:
            head_result = self._git(
                ["merge-base", "--is-ancestor", current_base, state.current_head],
                cwd=self.repo_root,
                timeout_seconds=60,
                allow_fail=True,
            )
            if head_result.exit_code == 0:
                head_descends = True
            elif head_result.exit_code is None:
                head_descends = None
            else:
                head_descends = False
        return {
            "canonical_known": True,
            "base_descends_from_canonical": base_descends,
            "head_descends_from_base": head_descends,
        }

    def _milestone_dir(self, milestone_id: str) -> Path:
        return self.milestones_root / milestone_id

    def _state_lock_path(self, milestone_id: str) -> Path:
        return self._milestone_dir(milestone_id) / ".state.lock"

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise MilestoneStateError(f"missing milestone state: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_state_if_any(self, path: Path) -> MilestoneState | None:
        if not path.exists():
            return None
        return MilestoneState.from_dict(self._load_json(path))

    def _save_state(self, milestone_id: str, state: MilestoneState) -> None:
        state_path = self._milestone_dir(milestone_id) / STATE_FILENAME
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self._state_lock_path(milestone_id)):
            atomic_json(state_path, state.to_dict())

    def _request_matches_state(self, request: MilestoneRequest, state: MilestoneState) -> bool:
        return (
            request.milestone_id == state.milestone_id
            and request.name == state.name
            and request.base_ref == state.base_ref
            and request.implementation_area == state.implementation_area
            and request.focused_tests == state.focused_tests
            and request.invariants == state.invariants
            and request.scope_paths == state.scope_paths
        )
