from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import time
import traceback

from . import __version__
from ._locking import FileLock
from .changes import capture_changes, detect_changes
from .config import ControllerConfig
from .experience import ExperienceLedger
from .logs import EventLog
from .models import Task, utc_now
from .providers.base import Provider
from .queue import DurableQueue
from .remote import LocalRunner
from .reports import ReportStore
from .worktrees import WorktreeInfo, WorktreeManager


class Controller:
    def __init__(
        self,
        *,
        config: ControllerConfig,
        queue: DurableQueue,
        runner: LocalRunner,
        worktrees: WorktreeManager,
        providers: list[tuple[Provider, int]],
        reports: ReportStore,
        experience: ExperienceLedger,
        events: EventLog | None = None,
    ) -> None:
        self.config = config
        self.queue = queue
        self.runner = runner
        self.worktrees = worktrees
        self.providers = providers
        self.reports = reports
        self.experience = experience
        self.events = events or EventLog(config.logs_root / "controller.jsonl")

    @classmethod
    def build_for_tests(
        cls,
        *,
        config: ControllerConfig,
        queue: DurableQueue,
        providers: list[tuple[Provider, int]],
    ) -> "Controller":
        runner = LocalRunner()
        return cls(
            config=config,
            queue=queue,
            runner=runner,
            worktrees=WorktreeManager(
                runner, config.repository_path, config.worktree_root, lock_root=config.controller_root
            ),
            providers=providers,
            reports=ReportStore(config.reports_root),
            experience=ExperienceLedger(config.experience_path),
            events=EventLog(config.logs_root / "controller.jsonl"),
        )

    def _log(self, event: str, **payload: object) -> None:
        try:
            self.events.append(
                {
                    "event": event,
                    "controller_version": __version__,
                    "timestamp": utc_now(),
                    **payload,
                }
            )
        except Exception:
            pass

    def _attempt_task(
        self,
        task: Task,
        *,
        attempt: int,
        provider_name: str,
        info: WorktreeInfo,
        previous_failure_category: str | None,
        previous_tests: list[dict],
        previous_changes: list[str],
    ) -> Task:
        metadata = dict(task.metadata)
        metadata["controller_context"] = {
            "attempt": attempt,
            "provider": provider_name,
            "previous_failure_category": previous_failure_category,
            "previous_tests": previous_tests,
            "previous_changes": previous_changes,
            "worktree_path": os.fspath(info.path),
            "branch": info.branch,
            "base_commit": info.base_commit,
            "worktree_dirty": info.dirty,
            "worktree_dirty_paths": list(info.dirty_paths),
        }
        return Task.from_dict({**task.to_dict(), "metadata": metadata})

    def _run_tests(self, task: Task, worktree: Path) -> list[dict]:
        outcomes = []
        for command in task.tests:
            result = self.runner.run(
                command,
                cwd=worktree,
                timeout_seconds=self.config.test_timeout_seconds,
            )
            outcomes.append(
                {
                    "argv": command,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "stdout": result.stdout[-100_000:],
                    "stderr": result.stderr[-100_000:],
                    "started_at": result.started_at,
                    "finished_at": result.finished_at,
                }
            )
            if result.exit_code != 0 or result.timed_out:
                break
        return outcomes

    def _all_providers(self, task: Task) -> list[tuple[Provider, int]]:
        return [
            (provider, min(configured_attempts, task.max_attempts))
            for provider, configured_attempts in self.providers
            if configured_attempts > 0
        ]

    def run_once(self) -> bool | None:
        with FileLock(self.config.process_lock_path):
            recovered = self.queue.recover_orphans(
                older_than_seconds=self.config.orphan_after_seconds
            )
            if recovered:
                self._log("queue_recovered", task_ids=recovered)
            task = self.queue.claim_next()
            if task is None:
                return None

            started = utc_now()
            self._log("task_claimed", task_id=task.id, title=task.title)
            attempts: list[dict] = []
            tests: list[dict] = []
            info: WorktreeInfo | None = None
            changes: list = []
            success = False
            failure_category: str | None = "controller_error"
            error_detail: str | None = None
            worktree_dirty_initially = False
            worktree_dirty_paths: list[str] = []
            previous_failure_category: str | None = None
            previous_tests: list[dict] = []
            previous_changes: list[str] = []

            try:
                info = self.worktrees.create(task)
                worktree_dirty_initially = info.dirty
                worktree_dirty_paths = list(info.dirty_paths)
                if info.dirty:
                    self._log(
                        "worktree_dirty",
                        task_id=task.id,
                        worktree_path=os.fspath(info.path),
                        dirty_paths=worktree_dirty_paths,
                    )
                    self.worktrees.reset(info)
                initial = capture_changes(self.runner, info.path, info.base_commit)

                total_attempts = 0
                for provider, configured_attempts in self._all_providers(task):
                    provider_budget = min(configured_attempts, task.max_attempts - total_attempts)
                    if provider_budget <= 0:
                        break
                    for _ in range(provider_budget):
                        total_attempts += 1
                        attempt_task = self._attempt_task(
                            task,
                            attempt=total_attempts,
                            provider_name=provider.name,
                            info=info,
                            previous_failure_category=previous_failure_category,
                            previous_tests=previous_tests,
                            previous_changes=previous_changes,
                        )
                        result = provider.execute(
                            task=attempt_task,
                            worktree=info.path,
                            attempt=total_attempts,
                        )
                        changes = detect_changes(
                            self.runner, info.path, info.base_commit, initial
                        )
                        result.changed_files = sorted({item.path for item in changes})
                        previous_changes = list(result.changed_files)
                        if result.success and not result.changed_files:
                            result.success = False
                            result.category = "no_changes"
                            result.retryable = True
                        if result.success:
                            tests = self._run_tests(task, info.path)
                            result.tests = tests
                            if all(
                                item["exit_code"] == 0 and not item["timed_out"]
                                for item in tests
                            ):
                                success = True
                                failure_category = None
                                previous_failure_category = None
                            else:
                                result.success = False
                                result.category = "tests_failed"
                                result.retryable = True
                                failure_category = "tests_failed"
                                previous_failure_category = "tests_failed"
                                previous_tests = list(tests)
                        else:
                            result.tests = []
                            failure_category = result.category or "provider_failed"
                            previous_failure_category = failure_category
                            previous_tests = []
                        attempts.append(result.to_dict())
                        self._log(
                            "attempt_finished",
                            task_id=task.id,
                            attempt=total_attempts,
                            provider=result.provider,
                            success=result.success,
                            category=result.category,
                            retryable=result.retryable,
                            changed_files=result.changed_files,
                            http_status=result.http_status,
                            timed_out=result.timed_out,
                        )
                        if success:
                            break
                        if total_attempts < task.max_attempts:
                            self.worktrees.reset(info)
                        if not result.retryable:
                            break
                    if success or total_attempts >= task.max_attempts:
                        break
            except Exception as error:
                error_detail = f"{type(error).__name__}: {error}"
                failure_category = "controller_error"
                attempts.append(
                    {
                        "provider": "controller",
                        "success": False,
                        "category": failure_category,
                        "error": error_detail,
                        "traceback": traceback.format_exc(limit=10),
                    }
                )
                self._log("controller_error", task_id=task.id, error=error_detail)

            finished = utc_now()
            report = {
                "controller_version": __version__,
                "task_id": task.id,
                "mission_id": task.metadata.get("mission_id"),
                "mission_task_id": task.metadata.get("mission_task_id"),
                "title": task.title,
                "original_task_prompt": task.prompt,
                "status": "succeeded" if success else "failed",
                "failure_category": failure_category,
                "error_detail": error_detail,
                "base_commit": info.base_commit if info else None,
                "branch": info.branch if info else None,
                "worktree_path": os.fspath(info.path) if info else None,
                "worktree_dirty_initially": worktree_dirty_initially,
                "worktree_dirty_paths": worktree_dirty_paths,
                "files_changed": sorted({item.path for item in changes}),
                "changes": [asdict(item) for item in changes],
                "attempts": attempts,
                "tests": tests,
                "recovered_orphans": recovered,
                "started_at": started,
                "finished_at": finished,
            }
            self.reports.write(task.id, report)
            self.experience.append(
                {
                    "task_id": task.id,
                    "status": report["status"],
                    "failure_category": failure_category,
                    "providers": [item.get("provider") for item in attempts],
                    "files_changed": report["files_changed"],
                    "finished_at": finished,
                }
            )
            self.queue.finish(task, succeeded=success)
            if success and info and self.config.cleanup_successful_worktrees:
                self.worktrees.cleanup(info, task_id=task.id)
            self._log(
                "task_finished",
                task_id=task.id,
                status=report["status"],
                failure_category=failure_category,
                files_changed=report["files_changed"],
            )
            return success

    def run_forever(self) -> None:
        while True:
            if self.run_once() is None:
                time.sleep(self.config.poll_interval_seconds)
