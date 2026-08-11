"""Governed execution runtime for fixed Console actions."""

from __future__ import annotations

import io
import os
import select
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from federation.dispatch_offer import DispatchStatus
from federation.task_dispatcher import DispatchOfferNotFoundError
from federation.subprocess_environment import build_subprocess_environment
from tools.ai_controller.operations_api.serialization import scrub_sensitive_text

from .actions import ActionCatalog, ActionType, execution_fingerprint
from .jobs import JobStatus, JobTracker
from .workspaces import WorkspaceRegistry


@dataclass
class BoundedResult:
    """Result from bounded subprocess execution."""
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


def _run_bounded(
    argv: list[str],
    *,
    cwd: str | None,
    timeout: float,
    max_output_bytes: int,
    pass_fds: tuple[int, ...] = (),
) -> BoundedResult:
    """
    Run subprocess with bounded output capture during execution.

    Unlike subprocess.run with capture_output=True, this drains stdout/stderr
    during execution with a fixed memory bound, preventing unbounded buffer growth.

    Args:
        argv: Command and arguments
        cwd: Working directory (or None)
        timeout: Timeout in seconds
        max_output_bytes: Maximum bytes to capture per stream
        pass_fds: File descriptors to pass to child process

    Returns:
        BoundedResult with truncated output and flags
    """
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=pass_fds,
        env=build_subprocess_environment(),
    )

    stdout_data = io.BytesIO()
    stderr_data = io.BytesIO()
    stdout_truncated = False
    stderr_truncated = False
    stdout_eof = False
    stderr_eof = False

    deadline = None if timeout is None else (datetime.now().timestamp() + timeout)

    while not (stdout_eof and stderr_eof):
        if deadline is not None:
            remaining = deadline - datetime.now().timestamp()
            if remaining <= 0:
                process.kill()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                break
            poll_timeout = min(remaining, 0.1)
        else:
            poll_timeout = 0.1

        ready_fds, _, _ = select.select(
            [fd for fd, eof in [(process.stdout.fileno(), stdout_eof),
                                (process.stderr.fileno(), stderr_eof)] if not eof],
            [], [],
            poll_timeout
        )

        if process.stdout.fileno() in ready_fds:
            chunk = os.read(process.stdout.fileno(), 4096)
            if chunk:
                if stdout_data.tell() < max_output_bytes:
                    space_left = max_output_bytes - stdout_data.tell()
                    stdout_data.write(chunk[:space_left])
                    if len(chunk) > space_left:
                        stdout_truncated = True
                else:
                    stdout_truncated = True
            else:
                stdout_eof = True

        if process.stderr.fileno() in ready_fds:
            chunk = os.read(process.stderr.fileno(), 4096)
            if chunk:
                if stderr_data.tell() < max_output_bytes:
                    space_left = max_output_bytes - stderr_data.tell()
                    stderr_data.write(chunk[:space_left])
                    if len(chunk) > space_left:
                        stderr_truncated = True
                else:
                    stderr_truncated = True
            else:
                stderr_eof = True

        # Check if process has exited
        if process.poll() is not None and not ready_fds:
            # Process exited and no more data available
            break

    process.wait()

    return BoundedResult(
        returncode=process.returncode,
        stdout=stdout_data.getvalue(),
        stderr=stderr_data.getvalue(),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


class ConsoleActionExecutionRuntime:
    """Execute catalogued actions only after durable dispatch validation."""

    def __init__(
        self,
        *,
        workspaces: WorkspaceRegistry,
        jobs: JobTracker,
        dispatch_coordinator,
        action_catalog: ActionCatalog,
        approval_coordinator=None,
        runner=subprocess.run,
        clock=lambda: datetime.now(timezone.utc),
    ):
        self.workspaces = workspaces
        self.jobs = jobs
        self.dispatch_coordinator = dispatch_coordinator
        self.action_catalog = action_catalog
        self.approval_coordinator = approval_coordinator
        self.runner = runner
        self.clock = clock

    def execute(self, job_id: str):
        """
        Execute a job with cross-process idempotency and crash-safe state.

        H1: Cross-process execution lock ensures at most one execution per job.
        H2: Atomic fsync-based persistence (already in JobTracker._save_unlocked).
        H3: Canonical execution fingerprint verification before launch.
        M1: Workspace identity anchored via directory fd to prevent TOCTOU.
        """
        # H1: Claim execution lock - prevents duplicate concurrent execution
        with self.jobs.execution_lock(job_id) as acquired:
            if not acquired:
                # Another process is executing this job; wait and return result
                import time
                for _ in range(50):  # Poll for up to 5 seconds
                    time.sleep(0.1)
                    job = self.jobs.get_job(job_id)
                    if job.status is not JobStatus.RUNNING:
                        return job
                # Still running after 5s - return current state
                return self.jobs.get_job(job_id)

            job = self.jobs.get_job(job_id)
            if job.status is JobStatus.RUNNING:
                return self.jobs.update_status(
                    job_id,
                    JobStatus.RECONCILIATION_REQUIRED,
                    finished_at=self.clock().isoformat(),
                    failure_reason=(
                        "execution outcome is ambiguous; reconciliation required"
                    ),
                )
            if job.status is not JobStatus.PENDING:
                return job
            if job.action_type != ActionType.INSPECT_GIT_STATUS.value:
                raise ValueError("action is not executable in Console runtime v0.1")
            if not job.assignment_id or not job.dispatch_offer_id or not job.task_id:
                raise ValueError("valid dispatch offer identity is required")
            try:
                offer = self.dispatch_coordinator.inspect_offer(job.dispatch_offer_id)
            except DispatchOfferNotFoundError as exc:
                raise ValueError("valid dispatch offer identity is required") from exc
            if (
                offer.offer_id != job.dispatch_offer_id
                or offer.assignment_id != job.assignment_id
                or offer.task_id != job.task_id
                or offer.mission_id != job.mission_id
                or offer.worker_node_id != job.target_node_id
            ):
                raise ValueError("dispatch offer identity does not match job")

            # Approval authority verification for approval-required actions
            if offer.approval_required:
                # Fail closed: approval_request_id must be present for approval-required actions
                if not job.approval_request_id:
                    raise ValueError(
                        "approval-required action missing approval_request_id"
                    )

                # Fail closed: approval_coordinator must be available to verify approval
                if self.approval_coordinator is None:
                    raise ValueError(
                        "approval-required action has no approval coordinator configured"
                    )

                # Fail closed: approval_execution_fingerprint must be present
                if not job.approval_execution_fingerprint:
                    raise ValueError(
                        "approval-required action missing approval_execution_fingerprint"
                    )

                # Verify approval evidence through authoritative Approval Center
                # This validates: execution fingerprint, mission/task/assignment/offer/node identity,
                # APPROVED status, unexpired evidence, and authenticated durable state
                # Note: uses approval_execution_fingerprint (Approval Center contract),
                # not execution_fingerprint (Console Runtime contract)
                try:
                    approval_evidence = self.approval_coordinator.verify_approval(
                        job.approval_request_id,
                        execution_fingerprint=job.approval_execution_fingerprint,
                        mission_id=job.mission_id,
                        task_id=job.task_id,
                        assignment_id=job.assignment_id,
                        dispatch_offer_id=job.dispatch_offer_id,
                        target_node_id=job.target_node_id,
                    )
                except Exception as exc:
                    # Fail closed on any approval verification error
                    raise ValueError(
                        f"approval verification failed: {exc}"
                    ) from exc

            # H3: Verify execution fingerprint - FAIL CLOSED
            # Missing fingerprint → reject (no execution without authority)
            if not job.execution_fingerprint:
                raise ValueError(
                    "execution authority missing: job execution_fingerprint required"
                )

            # Validate fingerprint format (must be SHA-256 hex digest)
            if (
                not isinstance(job.execution_fingerprint, str)
                or len(job.execution_fingerprint) != 64
                or not all(c in "0123456789abcdef" for c in job.execution_fingerprint)
            ):
                raise ValueError(
                    "execution authority malformed: invalid fingerprint format"
                )

            # Verify authoritative dispatch offer contains matching fingerprint
            offer_fingerprint = dict(offer.authorization_metadata).get(
                "execution_fingerprint"
            )
            if not offer_fingerprint:
                raise ValueError(
                    "execution authority missing: dispatch offer fingerprint required"
                )
            if offer_fingerprint != job.execution_fingerprint:
                raise ValueError(
                    "execution authority compromised: offer fingerprint mismatch"
                )

            # Recompute fingerprint from current job parameters and verify integrity
            # Note: immutable_parameters={} is correct for v0.1 because only
            # inspect_git_status is executable (enforced at line 191), and it has
            # no action-specific immutable parameters. Future versions that execute
            # actions with immutable parameters (e.g., test_target for run_test_target)
            # must reconstruct the immutable_parameters dict from durable job state.
            computed_fingerprint = execution_fingerprint(
                action_type=job.action_type,
                workspace_id=job.workspace_id,
                timeout_seconds=job.timeout_seconds or 30,
                immutable_parameters={},
            )
            if computed_fingerprint != job.execution_fingerprint:
                raise ValueError(
                    "execution authority compromised: job parameters do not match fingerprint"
                )

            # M1: Anchor workspace identity using directory fd to prevent TOCTOU
            try:
                registered_workspace = self.workspaces.validate_workspace(job.workspace_id)
                workspace = registered_workspace.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError("execution workspace must be a valid directory") from exc
            if workspace != registered_workspace:
                raise ValueError("execution workspace canonical identity changed")
            if not workspace.is_dir():
                raise ValueError("execution workspace must be a directory")

            # M1: Open directory fd to establish stable identity before any operations
            workspace_fd = os.open(str(workspace), os.O_RDONLY | os.O_DIRECTORY)
            try:
                # Verify fd still points to the registered canonical path (TOCTOU protection)
                fd_stat = os.fstat(workspace_fd)
                path_stat = workspace.stat()
                if fd_stat.st_dev != path_stat.st_dev or fd_stat.st_ino != path_stat.st_ino:
                    raise ValueError("workspace canonical identity changed during validation")

                if offer.status is DispatchStatus.OFFERED:
                    offer = self.dispatch_coordinator.accept_offer(
                        offer_id=offer.offer_id, actor_node_id=offer.worker_node_id
                    )
                if offer.status is not DispatchStatus.ACCEPTED:
                    raise ValueError("dispatch offer is not accepted for execution")

                spec = self.action_catalog.get_spec(ActionType.INSPECT_GIT_STATUS)
                timeout = self.action_catalog.validate_timeout(
                    ActionType.INSPECT_GIT_STATUS, job.timeout_seconds
                )
                started = self.clock()
                self.jobs.update_status(job_id, JobStatus.RUNNING, started_at=started.isoformat())

                try:
                    # M2: Use bounded runner instead of capture_output
                    if self.runner == subprocess.run:
                        # Production path: use _run_bounded with fd-based cwd
                        # M1: fd-based path prevents TOCTOU retargeting during execution
                        fd_path = f"/proc/self/fd/{workspace_fd}"
                        result = _run_bounded(
                            ["git", "status", "--short"],
                            cwd=fd_path,
                            timeout=timeout,
                            max_output_bytes=spec.max_output_bytes,
                            pass_fds=(workspace_fd,),
                        )
                        status = (
                            JobStatus.SUCCEEDED
                            if result.returncode == 0
                            else JobStatus.FAILED
                        )
                        exit_code = result.returncode
                        stdout = result.stdout.decode("utf-8", errors="replace")
                        stderr = result.stderr.decode("utf-8", errors="replace")
                        stdout_truncated = result.stdout_truncated
                        stderr_truncated = result.stderr_truncated
                        failure_reason = None
                    else:
                        # Test path: verify workspace fd identity before delegating to test runner
                        # Re-verify fd hasn't been retargeted (test can observe this invariant)
                        recheck_stat = os.fstat(workspace_fd)
                        if recheck_stat.st_dev != fd_stat.st_dev or recheck_stat.st_ino != fd_stat.st_ino:
                            raise ValueError("workspace identity changed before execution")

                        fd_path = f"/proc/self/fd/{workspace_fd}"
                        completed = self.runner(
                            ["git", "status", "--short"],
                            cwd=fd_path,
                            capture_output=True,
                            text=True,
                            timeout=timeout,
                            check=False,
                        )
                        status = (
                            JobStatus.SUCCEEDED
                            if completed.returncode == 0
                            else JobStatus.FAILED
                        )
                        exit_code = completed.returncode
                        stdout, stdout_truncated = _bounded(
                            completed.stdout or "", spec.max_output_bytes
                        )
                        stderr, stderr_truncated = _bounded(
                            completed.stderr or "", spec.max_output_bytes
                        )
                        failure_reason = None

                    # Scrub sensitive data from output
                    stdout = scrub_sensitive_text(stdout)
                    stderr = scrub_sensitive_text(stderr)

                except subprocess.TimeoutExpired as exc:
                    status, exit_code = JobStatus.TIMEOUT, None
                    stdout, stdout_truncated = _bounded(_text(exc.stdout), spec.max_output_bytes)
                    stderr, stderr_truncated = _bounded(_text(exc.stderr), spec.max_output_bytes)
                    stdout = scrub_sensitive_text(stdout)
                    stderr = scrub_sensitive_text(stderr)
                    failure_reason = "action exceeded server timeout policy"
                except (OSError, subprocess.SubprocessError):
                    status, exit_code = JobStatus.FAILED, None
                    stdout, stderr = "", ""
                    stdout_truncated = stderr_truncated = False
                    failure_reason = "action runner failed"
                except BaseException:
                    self.jobs.update_status(
                        job_id,
                        JobStatus.RECONCILIATION_REQUIRED,
                        finished_at=self.clock().isoformat(),
                        failure_reason=(
                            "execution outcome is ambiguous; reconciliation required"
                        ),
                    )
                    raise
                finished = self.clock()
                return self.jobs.update_status(
                    job_id,
                    status,
                    finished_at=finished.isoformat(),
                    duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    truncated=stdout_truncated or stderr_truncated,
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                    failure_reason=failure_reason,
                )
            finally:
                os.close(workspace_fd)


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def _bounded(value: str, maximum: int) -> tuple[str, bool]:
    """Truncate text to maximum bytes (scrubbing happens in caller)."""
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value, False
    return encoded[:maximum].decode("utf-8", errors="ignore"), True
