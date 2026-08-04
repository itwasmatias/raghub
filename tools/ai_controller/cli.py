from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .config import ControllerConfig
from .controller import Controller
from .experience import ExperienceLedger
from .logs import EventLog
from .models import Task, utc_now
from .providers import CodexProvider, LocalOllamaProvider
from .providers.native_agent import NativeOllamaAgentProvider
from .queue import DurableQueue
from .remote import LocalRunner, SSHRunner
from .reports import ReportStore
from .worktrees import WorktreeManager
from ._locking import FileLock
from .mission import (
    MissionDefinition,
    MissionStore,
    MissionScheduler,
    TaskMaterializer,
    MissionEventLog,
    validate_mission,
    generate_mission_report,
    outline_to_mission,
    load_outline_file,
    save_mission_file,
)


def _runner(config: ControllerConfig) -> LocalRunner:
    if config.ssh_host:
        return SSHRunner(
            ssh_executable=config.ssh_executable or "ssh",
            host=config.ssh_host,
            key_path=config.ssh_key_path or "",
            remote_python=config.remote_python,
        )
    return LocalRunner()


def _ensure_remote_directory(runner: LocalRunner, path: str | Path, cwd: str | Path = ".") -> None:
    if isinstance(runner, SSHRunner):
        result = runner.run(
            [
                runner.remote_python,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).mkdir(parents=True, exist_ok=True)",
                str(path),
            ],
            cwd=cwd,
            timeout_seconds=60,
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or result.stdout or f"failed to create {path}")
        return
    Path(path).mkdir(parents=True, exist_ok=True)


def _ensure_local_directories(config: ControllerConfig) -> None:
    config.controller_root.mkdir(parents=True, exist_ok=True)
    config.queue_root.mkdir(parents=True, exist_ok=True)
    config.reports_root.mkdir(parents=True, exist_ok=True)
    config.logs_root.mkdir(parents=True, exist_ok=True)
    config.experience_path.parent.mkdir(parents=True, exist_ok=True)
    config.process_lock_path.parent.mkdir(parents=True, exist_ok=True)


def _latest_report_path(root: Path) -> Path | None:
    reports = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime)
    return reports[-1] if reports else None


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def build(config: ControllerConfig) -> Controller:
    runner = _runner(config)
    providers: list[tuple[object, int]] = []
    if config.codex_attempts:
        providers.append(
            (
                CodexProvider(
                    runner=runner,
                    executable=config.codex_executable,
                    prefix_args=config.codex_prefix_args,
                    timeout_seconds=config.provider_timeout_seconds,
                ),
                config.codex_attempts,
            )
        )
    if config.local_attempts:
        providers.append(
            (
                LocalOllamaProvider(
                    runner=runner,
                    base_url=config.ollama_base_url,
                    model=config.ollama_model,
                    request_timeout_seconds=config.ollama_request_timeout_seconds,
                    generation_timeout_seconds=config.ollama_generation_timeout_seconds,
                    context_size=config.ollama_context_size,
                    temperature=config.ollama_temperature,
                    max_retries=config.ollama_max_retries,
                    max_output_chars=config.ollama_max_output_chars,
                ),
                config.local_attempts,
            )
        )
    if config.native_agent_attempts:
        providers.append(
            (
                NativeOllamaAgentProvider(
                    runner=runner,
                    base_url=config.ollama_base_url,
                    model=config.ollama_model,
                    request_timeout_seconds=config.ollama_request_timeout_seconds,
                    generation_timeout_seconds=config.ollama_generation_timeout_seconds,
                    context_size=config.ollama_context_size,
                    temperature=config.ollama_temperature,
                    keep_alive=config.ollama_keep_alive,
                    max_agent_steps=config.ollama_max_agent_steps,
                    max_tool_failures=config.ollama_max_tool_failures,
                    max_output_chars=config.ollama_max_output_chars,
                    allowed_commands=config.native_agent_allowed_commands,
                    command_timeout_seconds=config.native_agent_command_timeout_seconds,
                ),
                config.native_agent_attempts,
            )
        )
    return Controller(
        config=config,
        queue=DurableQueue(config.queue_root),
        runner=runner,
        worktrees=WorktreeManager(
            runner,
            config.repository_path,
            config.worktree_root,
            lock_root=config.controller_root,
        ),
        providers=providers,
        reports=ReportStore(config.reports_root),
        experience=ExperienceLedger(config.experience_path),
        events=EventLog(config.logs_root / "controller.jsonl"),
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="RAGHub AI Controller")
    result.add_argument("--config", required=True, type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("task", type=Path)
    run = commands.add_parser("run")
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--continuous", action="store_true")
    recover = commands.add_parser("recover")
    recover.add_argument("--older-than-seconds", type=float)
    commands.add_parser("status")
    report = commands.add_parser("report")
    report.add_argument("--task-id")
    doctor_cmd = commands.add_parser("doctor")
    doctor_cmd.add_argument("--json", action="store_true", dest="json_output")
    commands.add_parser("smoke-local")

    # Mission orchestrator subcommand
    mission_cmd = commands.add_parser("mission")
    mission_sub = mission_cmd.add_subparsers(dest="mission_action", required=True)

    m_validate = mission_sub.add_parser("validate")
    m_validate.add_argument("mission_file", type=Path)

    m_create = mission_sub.add_parser("create")
    m_create.add_argument("mission_file", type=Path)

    m_status = mission_sub.add_parser("status")
    m_status.add_argument("mission_id")
    m_status.add_argument("--json", action="store_true", dest="json_output")

    m_graph = mission_sub.add_parser("graph")
    m_graph.add_argument("mission_id")

    m_run = mission_sub.add_parser("run")
    m_run.add_argument("mission_id")
    m_run_mode = m_run.add_mutually_exclusive_group(required=True)
    m_run_mode.add_argument("--once", action="store_true")
    m_run_mode.add_argument("--continuous", action="store_true")

    m_pause = mission_sub.add_parser("pause")
    m_pause.add_argument("mission_id")

    m_resume = mission_sub.add_parser("resume")
    m_resume.add_argument("mission_id")

    m_cancel = mission_sub.add_parser("cancel")
    m_cancel.add_argument("mission_id")

    m_report = mission_sub.add_parser("report")
    m_report.add_argument("mission_id")
    m_report.add_argument("--json", action="store_true", dest="json_output")

    m_plan = mission_sub.add_parser("plan")
    m_plan.add_argument("outline_file", type=Path)
    m_plan.add_argument("--output", type=Path, default=None)

    m_repair = mission_sub.add_parser("repair")
    m_repair.add_argument("mission_id")
    m_repair.add_argument(
        "--apply",
        metavar="ACTION_ID",
        dest="apply_action_id",
        help="Apply exactly one SAFE_REPAIR action by stable action ID",
    )
    m_repair.add_argument("--json", action="store_true", dest="json_output")

    m_migrate = mission_sub.add_parser("migrate-legacy-records")
    m_migrate.add_argument("mission_id")
    m_migrate_mode = m_migrate.add_mutually_exclusive_group(required=True)
    m_migrate_mode.add_argument("--scan", action="store_true", help="Read-only discovery of legacy records")
    m_migrate_mode.add_argument("--plan", action="store_true", dest="plan_migration", help="Deterministic action proposal")
    m_migrate_mode.add_argument("--apply", action="store_true", help="Safe mutation (requires explicit flag)")
    m_migrate.add_argument("--json", action="store_true", dest="json_output")

    return result


def _doctor_check(label: str, ok: bool, detail: str) -> bool:
    status = "OK" if ok else "FAIL"
    print(f"[{status}] {label}: {detail}")
    return ok


def _ollama_check(config: ControllerConfig) -> bool:
    try:
        with urlopen(
            f"{config.ollama_base_url.rstrip('/')}/api/version",
            timeout=min(config.ollama_request_timeout_seconds, 10),
        ) as response:
            if response.status != 200:
                return _doctor_check("ollama version", False, f"HTTP {response.status}")
        with urlopen(
            f"{config.ollama_base_url.rstrip('/')}/api/tags",
            timeout=min(config.ollama_request_timeout_seconds, 10),
        ) as response:
            raw = response.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        models = payload.get("models", []) if isinstance(payload, dict) else []
        if not isinstance(models, list):
            return _doctor_check("ollama tags", False, "invalid response payload")
        present = any(
            isinstance(item, dict)
            and (item.get("name") == config.ollama_model or item.get("model") == config.ollama_model)
            for item in models
        )
        if not present:
            return _doctor_check("ollama model", False, f"missing {config.ollama_model}")
        return _doctor_check("ollama", True, f"{config.ollama_base_url} / {config.ollama_model}")
    except (OSError, URLError, json.JSONDecodeError) as error:
        return _doctor_check("ollama", False, str(error))


def _report_payload(reports_root: Path, task_id: str | None = None) -> dict | None:
    if task_id is not None:
        path = reports_root / f"{task_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None
    latest = _latest_report_path(reports_root)
    if latest is None:
        return None
    return json.loads(latest.read_text(encoding="utf-8"))


def _queue_counts(queue_root: Path) -> dict[str, int]:
    return {
        name: len(list((queue_root / name).glob("*.json")))
        for name in ("pending", "running", "succeeded", "failed", "invalid")
    }


def _doctor(settings: ControllerConfig, runner: LocalRunner) -> dict:
    """Run all doctor checks. Returns structured result dict."""
    checks: list[dict] = []

    def check(label: str, ok: bool, detail: str, required: bool = True) -> bool:
        status = "ok" if ok else ("fail" if required else "warn")
        checks.append({"label": label, "status": status, "detail": detail})
        marker = "OK" if ok else ("FAIL" if required else "WARN")
        print(f"[{marker}] {label}: {detail}")
        return ok

    all_ok = True

    all_ok &= check("controller root", settings.controller_root.exists(), str(settings.controller_root))
    all_ok &= check(
        "queue directories",
        all((settings.queue_root / name).exists() for name in ("pending", "running", "succeeded", "failed", "invalid")),
        str(settings.queue_root),
    )
    all_ok &= check("reports directory", settings.reports_root.exists(), str(settings.reports_root))
    all_ok &= check("logs directory", settings.logs_root.exists(), str(settings.logs_root))
    all_ok &= check("ledger directory", settings.experience_path.parent.exists(), str(settings.experience_path.parent))

    try:
        with FileLock(settings.process_lock_path):
            check("process lock", True, str(settings.process_lock_path))
    except Exception as error:
        all_ok &= check("process lock", False, str(error))

    # Stale running tasks
    running = list((settings.queue_root / "running").glob("*.json")) if (settings.queue_root / "running").exists() else []
    check(
        "stale running tasks",
        len(running) == 0,
        f"{len(running)} task(s) in running state" if running else "none",
        required=False,
    )

    if settings.ssh_host:
        ssh_executable = settings.ssh_executable or "ssh"
        ssh_ok = check(
            "ssh executable",
            bool(Path(ssh_executable).exists() or shutil.which(ssh_executable)),
            ssh_executable,
        )
        ssh_ok &= check("ssh key", Path(settings.ssh_key_path or "").exists(), settings.ssh_key_path or "")
        if ssh_ok:
            repo_check = runner.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=settings.repository_path,
                timeout_seconds=60,
            )
            check(
                "fedora repository",
                repo_check.exit_code == 0,
                repo_check.stdout.strip() or repo_check.stderr.strip() or str(settings.repository_path),
            )
            if settings.codex_attempts:
                codex_check = runner.run(
                    [settings.codex_executable, "--version"],
                    cwd=settings.repository_path,
                    timeout_seconds=60,
                )
                check(
                    "fedora codex",
                    codex_check.exit_code == 0,
                    codex_check.stdout.strip() or codex_check.stderr.strip() or settings.codex_executable,
                    required=bool(settings.codex_attempts),
                )
            worktree_check = runner.run(
                [
                    settings.remote_python,
                    "-c",
                    "from pathlib import Path; import sys; print('1' if Path(sys.argv[1]).exists() else '0')",
                    str(settings.worktree_root),
                ],
                cwd=settings.repository_path,
                timeout_seconds=60,
            )
            check(
                "fedora worktree root",
                worktree_check.exit_code == 0 and worktree_check.stdout.strip() == "1",
                str(settings.worktree_root),
            )
        all_ok &= ssh_ok
    else:
        check("ssh", False, "ssh_host is not configured", required=False)

    # Ollama
    try:
        with urlopen(
            f"{settings.ollama_base_url.rstrip('/')}/api/version",
            timeout=min(settings.ollama_request_timeout_seconds, 10),
        ) as resp:
            ollama_version_ok = resp.status == 200
        with urlopen(
            f"{settings.ollama_base_url.rstrip('/')}/api/tags",
            timeout=min(settings.ollama_request_timeout_seconds, 10),
        ) as resp:
            tags_raw = resp.read().decode("utf-8", errors="replace")
        tags_payload = json.loads(tags_raw)
        model_list = tags_payload.get("models", []) if isinstance(tags_payload, dict) else []
        model_present = any(
            isinstance(m, dict) and (m.get("name") == settings.ollama_model or m.get("model") == settings.ollama_model)
            for m in model_list
        )
        check("ollama reachable", ollama_version_ok, settings.ollama_base_url)
        check("ollama model installed", model_present, f"{settings.ollama_model} at {settings.ollama_base_url}")
        if settings.native_agent_attempts:
            check(
                "native agent model",
                model_present,
                f"native-ollama-agent will use {settings.ollama_model}",
            )
        all_ok &= ollama_version_ok and model_present
    except (OSError, URLError, json.JSONDecodeError) as error:
        all_ok &= check("ollama", False, str(error))

    print(f"\n{'PASS' if all_ok else 'FAIL'}: controller {'operational' if all_ok else 'has problems'}")
    return {"ok": all_ok, "checks": checks}


def _smoke_local(settings: ControllerConfig) -> int:
    """
    Controlled fixture smoke test for the NativeOllamaAgentProvider.

    Creates a temporary git repository, enqueues a task, runs the native
    agent, verifies changes, generates a report, and writes to the experience
    ledger. Does NOT use a fake Ollama server — requires live Ollama.

    If Ollama is unavailable, fails clearly with a preserved report.
    """
    print("smoke-local: starting fixture smoke test")
    print(f"  Ollama: {settings.ollama_base_url}")
    print(f"  Model:  {settings.ollama_model}")

    # 1. Check Ollama reachability before touching any fixture
    try:
        with urlopen(
            f"{settings.ollama_base_url.rstrip('/')}/api/version",
            timeout=min(settings.ollama_request_timeout_seconds, 10),
        ) as resp:
            if resp.status != 200:
                print(f"\nFAIL: Ollama returned HTTP {resp.status} — smoke test cannot proceed.")
                print("This is an external environment limitation, not a code defect.")
                return 1
    except (OSError, URLError) as error:
        print(f"\nFAIL: Ollama not reachable at {settings.ollama_base_url}")
        print(f"  Error: {error}")
        print("This is an external environment limitation, not a code defect.")
        return 1

    with tempfile.TemporaryDirectory(prefix="raghub-smoke-") as tmpdir:
        root = Path(tmpdir)
        repo = root / "fixture-repo"
        worktree_root = root / "worktrees"
        controller_root = root / "controller"

        # 2. Create fixture git repository
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "smoke@test.local"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Smoke Test"], cwd=repo, check=True, capture_output=True)
        fixture = repo / "fixture.py"
        fixture.write_text('# Fixture file for smoke test\nVERSION = "1.0"\n', encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        base_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        print(f"  Fixture repo: {repo}")
        print(f"  Base commit:  {base_commit[:12]}")

        # 3. Set up controller infrastructure
        worktree_root.mkdir(parents=True)
        controller_root.mkdir(parents=True)
        for subdir in ("pending", "running", "succeeded", "failed", "invalid"):
            (controller_root / "queue" / subdir).mkdir(parents=True)
        (controller_root / "reports").mkdir()
        (controller_root / "logs").mkdir()

        # 4. Build task
        task = Task(
            id="smoke-native-agent",
            title="Smoke test: native Ollama agent",
            prompt=(
                "Add a comment to fixture.py explaining it is used for testing.\n"
                "The file templates/sip_markets.html must remain unchanged.\n"
                "Only modify fixture.py."
            ),
            base_ref="HEAD",
            tests=[],
        )

        # 5. Create worktree
        runner = LocalRunner()
        wm = WorktreeManager(runner, repo, worktree_root, lock_root=controller_root)
        info = wm.create(task)
        print(f"  Worktree:    {info.path}")

        # 6. Instantiate native provider
        provider = NativeOllamaAgentProvider(
            runner=runner,
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            request_timeout_seconds=settings.ollama_request_timeout_seconds,
            generation_timeout_seconds=settings.ollama_generation_timeout_seconds,
            context_size=settings.ollama_context_size,
            temperature=settings.ollama_temperature,
            keep_alive=settings.ollama_keep_alive,
            max_agent_steps=settings.ollama_max_agent_steps,
            max_tool_failures=settings.ollama_max_tool_failures,
            max_output_chars=settings.ollama_max_output_chars,
            allowed_commands=settings.native_agent_allowed_commands,
            command_timeout_seconds=settings.native_agent_command_timeout_seconds,
        )

        # 7. Execute provider
        print(f"\n  Running NativeOllamaAgentProvider (max {settings.ollama_max_agent_steps} steps)...")
        started = utc_now()
        try:
            result = provider.execute(task=task, worktree=info.path, attempt=1)
        except Exception as exc:
            result = None
            print(f"  Provider raised exception: {exc}")
            traceback.print_exc()

        # 8. Detect git changes (controller-owned, independent of model claims)
        from .changes import capture_changes
        changes = capture_changes(runner, info.path, info.base_commit) if result else []
        changed_files = sorted({c.path for c in changes})
        finished_at = utc_now()

        print(f"\n  Provider success (model claim): {result.success if result else 'N/A'}")
        print(f"  Changed files (controller git): {changed_files}")

        # Show agent steps for diagnostics
        if result and result.returned_text:
            try:
                steps_data = json.loads(result.returned_text).get("steps", [])
                if steps_data:
                    print(f"\n  Agent steps ({len(steps_data)} total):")
                    for s in steps_data[:8]:
                        status = "OK" if s.get("success") else "FAIL"
                        err = f" [{s.get('error', '')[:60]}]" if s.get("error") else ""
                        print(f"    step {s.get('step')}: [{status}] {s.get('tool')} — {s.get('thought', '')[:60]}{err}")
            except (json.JSONDecodeError, AttributeError):
                print(f"\n  returned_text preview: {result.returned_text[:300]}")

        # 9. Build report — persisted to settings.reports_root (survives temp dir cleanup)
        _ensure_local_directories(settings)
        report = {
            "smoke_test": "smoke-local",
            "task_id": task.id,
            "provider": "native-ollama-agent",
            "model": settings.ollama_model,
            "ollama_base_url": settings.ollama_base_url,
            "base_commit": base_commit,
            "started_at": started,
            "finished_at": finished_at,
            "provider_success_claim": result.success if result else False,
            "controller_verified_changes": bool(changed_files),
            "final_success": result is not None and result.success and bool(changed_files),
            "category": result.category if result else "exception",
            "changed_files": changed_files,
            "returned_text_preview": (result.returned_text[:1000] if result else ""),
            "stderr": result.stderr if result else "",
        }

        # 10. Write experience ledger entry
        ledger = ExperienceLedger(settings.experience_path)
        ledger.append({
            "task_id": task.id,
            "smoke_test": True,
            "status": "succeeded" if report["final_success"] else "failed",
            "failure_category": result.category if result and not result.success else (
                "no_changes" if result and result.success and not changed_files else None
            ),
            "providers": ["native-ollama-agent"],
            "files_changed": changed_files,
            "finished_at": finished_at,
        })

        # 11. Write report to persistent location
        report_path = settings.reports_root / f"{task.id}.json"
        from .reports import report_lock_path
        with FileLock(report_lock_path(settings.reports_root)):
            report_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        print(f"\n  Report: {report_path}")
        print(f"  Ledger: {settings.experience_path}")

        success = report["final_success"]
        print(f"\n{'PASS' if success else 'FAIL'}: smoke-local {'succeeded' if success else 'failed'}")
        if not success:
            if result and result.success and not changed_files:
                print("  Reason: model claimed success but controller found no git changes")
                print("  This indicates the model completed without writing files.")
                print("  Framework behavior is correct — no-change tasks fail at the controller level.")
            elif result:
                print(f"  Reason: {result.stderr or result.category or 'unknown'}")
        return 0 if success else 1


def _mission_command(args: argparse.Namespace, settings: ControllerConfig) -> int:
    """Handle all `mission` subcommands."""
    action = args.mission_action
    repair_dry_run = (
        action == "repair"
        and getattr(args, "apply_action_id", None) is None
    )
    store = MissionStore(
        settings.missions_root,
        create=not repair_dry_run,
    )
    queue = DurableQueue(
        settings.queue_root,
        create=not repair_dry_run,
    )
    materializer = TaskMaterializer(queue, settings.reports_root)

    if action == "validate":
        try:
            definition = MissionDefinition.from_file(str(args.mission_file))
        except (OSError, KeyError, ValueError) as exc:
            print(f"ERROR loading mission file: {exc}", file=sys.stderr)
            return 1
        result = validate_mission(definition)
        if result.valid:
            print(f"OK: mission {definition.mission_id!r} is valid ({len(definition.tasks)} tasks)")
            return 0
        print(f"INVALID: {len(result.errors)} error(s) in {args.mission_file}")
        for err in result.errors:
            loc = f"task={err.task_id}" if err.task_id else "mission"
            print(f"  [{err.code}] {loc}: {err.message}")
        return 1

    if action == "create":
        try:
            definition = MissionDefinition.from_file(str(args.mission_file))
        except (OSError, KeyError, ValueError) as exc:
            print(f"ERROR loading mission file: {exc}", file=sys.stderr)
            return 1
        result = validate_mission(definition)
        if not result.valid:
            print(f"INVALID: cannot create mission with {len(result.errors)} error(s)")
            for err in result.errors:
                loc = f"task={err.task_id}" if err.task_id else "mission"
                print(f"  [{err.code}] {loc}: {err.message}")
            return 1
        import datetime
        from .mission.models import (
            MissionState, MissionTaskState, MissionTaskStatus,
            MissionStatus, BudgetUsage,
        )
        if not definition.created_at:
            definition.created_at = datetime.datetime.utcnow().isoformat() + "Z"
        initial_task_states = {
            t.task_id: MissionTaskState(task_id=t.task_id, status=MissionTaskStatus.pending)
            for t in definition.tasks
        }
        initial_state = MissionState(
            mission_id=definition.mission_id,
            status=MissionStatus.pending,
            task_states=initial_task_states,
            events_path=str(settings.missions_root / "events" / f"{definition.mission_id}.jsonl"),
        )
        try:
            store.create(definition, initial_state)
        except FileExistsError:
            print(f"ERROR: mission {definition.mission_id!r} already exists", file=sys.stderr)
            return 1
        print(f"OK: mission {definition.mission_id!r} created ({len(definition.tasks)} tasks)")
        return 0

    if action == "status":
        try:
            state = store.load_state(args.mission_id)
            definition = store.load_definition(args.mission_id)
        except FileNotFoundError:
            print(f"ERROR: mission {args.mission_id!r} not found", file=sys.stderr)
            return 1
        from .mission.models import MissionTaskStatus
        counts: dict[str, int] = {s.value: 0 for s in MissionTaskStatus}
        for ts in state.task_states.values():
            counts[ts.status.value] = counts.get(ts.status.value, 0) + 1
        payload = {
            "mission_id": state.mission_id,
            "status": state.status.value,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "task_counts": counts,
            "budget_usage": state.budget_usage.to_dict(),
            "failure_reason": state.failure_reason,
            "root_cause_task_ids": state.root_cause_task_ids,
        }
        if getattr(args, "json_output", False):
            _print_json(payload)
        else:
            print(f"Mission:  {state.mission_id}")
            print(f"Status:   {state.status.value}")
            print(f"Tasks:    {len(state.task_states)} total")
            for status_val, count in sorted(counts.items()):
                if count:
                    print(f"  {status_val}: {count}")
        return 0

    if action == "graph":
        try:
            state = store.load_state(args.mission_id)
            definition = store.load_definition(args.mission_id)
        except FileNotFoundError:
            print(f"ERROR: mission {args.mission_id!r} not found", file=sys.stderr)
            return 1
        from .mission.graph import DependencyGraph
        graph = DependencyGraph(definition, state)
        print(graph.render_text())
        return 0

    if action == "run":
        try:
            state = store.load_state(args.mission_id)
            definition = store.load_definition(args.mission_id)
        except FileNotFoundError:
            print(f"ERROR: mission {args.mission_id!r} not found", file=sys.stderr)
            return 1
        from .mission.models import MissionStatus
        scheduler = MissionScheduler(
            store=store,
            queue=queue,
            reports_root=settings.reports_root,
            missions_root=settings.missions_root,
            poll_interval_seconds=settings.mission_poll_interval_seconds,
        )
        if args.once:
            status = scheduler.run_once(args.mission_id)
            print(f"Mission status after one cycle: {status.value}")
            return 0 if status in (MissionStatus.succeeded, MissionStatus.running, MissionStatus.pending) else 1
        scheduler.run_continuous(args.mission_id)
        return 0

    if action == "pause":
        try:
            store.load_state(args.mission_id)
        except FileNotFoundError:
            print(f"ERROR: mission {args.mission_id!r} not found", file=sys.stderr)
            return 1
        scheduler = MissionScheduler(
            store=store, queue=queue, reports_root=settings.reports_root,
            missions_root=settings.missions_root,
        )
        scheduler.pause(args.mission_id)
        print(f"OK: mission {args.mission_id!r} paused")
        return 0

    if action == "resume":
        try:
            store.load_state(args.mission_id)
        except FileNotFoundError:
            print(f"ERROR: mission {args.mission_id!r} not found", file=sys.stderr)
            return 1
        scheduler = MissionScheduler(
            store=store, queue=queue, reports_root=settings.reports_root,
            missions_root=settings.missions_root,
        )
        scheduler.resume(args.mission_id)
        print(f"OK: mission {args.mission_id!r} resumed")
        return 0

    if action == "cancel":
        try:
            store.load_state(args.mission_id)
        except FileNotFoundError:
            print(f"ERROR: mission {args.mission_id!r} not found", file=sys.stderr)
            return 1
        scheduler = MissionScheduler(
            store=store, queue=queue, reports_root=settings.reports_root,
            missions_root=settings.missions_root,
        )
        scheduler.cancel(args.mission_id)
        print(f"OK: mission {args.mission_id!r} cancelled")
        return 0

    if action == "report":
        try:
            state = store.load_state(args.mission_id)
            definition = store.load_definition(args.mission_id)
        except FileNotFoundError:
            print(f"ERROR: mission {args.mission_id!r} not found", file=sys.stderr)
            return 1
        event_log = MissionEventLog(settings.missions_root / "events", args.mission_id)
        report = generate_mission_report(definition, state, event_log)
        saved_path = store.write_report(args.mission_id, report)
        if getattr(args, "json_output", False):
            _print_json(report)
        else:
            print(f"Mission:  {report['mission_id']}")
            print(f"Status:   {report['mission_status']}")
            print(f"Outcome:  {report['outcome']}")
            print(f"Action:   {report['recommended_action']}")
            print(f"Report:   {saved_path}")
        return 0

    if action == "plan":
        try:
            outline = load_outline_file(str(args.outline_file))
        except (OSError, ValueError) as exc:
            print(f"ERROR loading outline: {exc}", file=sys.stderr)
            return 1
        definition, result = outline_to_mission(outline)
        if not result.valid:
            print(f"Plan produced {len(result.errors)} validation error(s):")
            for err in result.errors:
                loc = f"task={err.task_id}" if err.task_id else "mission"
                print(f"  [{err.code}] {loc}: {err.message}")
            return 1
        output_path = args.output or Path(f"{definition.mission_id}.json")
        save_mission_file(definition, str(output_path))
        print(f"OK: plan written to {output_path} ({len(definition.tasks)} tasks)")
        return 0

    if action == "repair":
        from .mission.repair import (
            RepairApplyStatus,
            RepairClassification,
            apply_mission_repair,
            plan_mission_repairs,
        )

        try:
            actions = plan_mission_repairs(
                args.mission_id,
                store,
                queue,
                settings.reports_root,
            )
        except FileNotFoundError:
            print(f"ERROR: mission {args.mission_id!r} not found", file=sys.stderr)
            return 1
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(
                f"ERROR: could not plan repairs for {args.mission_id!r}: {exc}",
                file=sys.stderr,
            )
            return 1

        apply_action_id = getattr(args, "apply_action_id", None)
        if apply_action_id is None:
            payload = {
                "mode": "dry-run",
                "mission_id": args.mission_id,
                "actions": [item.to_dict() for item in actions],
            }
            if getattr(args, "json_output", False):
                _print_json(payload)
                return 0

            print(f"DRY-RUN: mission {args.mission_id}")
            for item in actions:
                print(
                    f"{item.action_id}  {item.mission_id}  {item.task_id}  "
                    f"{item.classification.value}  {item.proposed_action.value}"
                )
                print(f"  Reason: {item.reason}")
            return 0

        selected = next(
            (item for item in actions if item.action_id == apply_action_id),
            None,
        )
        if selected is None:
            print(
                f"ERROR: repair action {apply_action_id!r} was not found in the current plan",
                file=sys.stderr,
            )
            return 1
        if selected.classification != RepairClassification.SAFE_REPAIR:
            print(
                f"ERROR: repair action {apply_action_id!r} is not safe to apply "
                f"({selected.classification.value})",
                file=sys.stderr,
            )
            return 1

        result = apply_mission_repair(
            selected,
            store,
            queue,
            settings.reports_root,
            settings.missions_root,
        )
        payload = {
            "mode": "apply",
            "mission_id": args.mission_id,
            "action": selected.to_dict(),
            "result": result.to_dict(),
        }
        if getattr(args, "json_output", False):
            _print_json(payload)
        else:
            print(
                f"{result.status.value}: {selected.action_id} "
                f"{selected.mission_id}/{selected.task_id}"
            )
            print(f"  Reason: {result.reason}")
        return 0 if result.status in {
            RepairApplyStatus.APPLIED,
            RepairApplyStatus.NO_LONGER_NEEDED,
        } else 1

    if action == "migrate-legacy-records":
        from .mission.migration import (
            scan_mission_migration,
            plan_mission_migration,
            apply_mission_migration,
        )

        try:
            # Verify mission exists
            store.load_state(args.mission_id)
            store.load_definition(args.mission_id)
        except FileNotFoundError:
            print(f"ERROR: mission {args.mission_id!r} not found", file=sys.stderr)
            return 1

        # Scan mode (read-only discovery)
        if args.scan:
            scan_results = scan_mission_migration(args.mission_id, store, queue)

            if getattr(args, "json_output", False):
                _print_json({"scan_results": scan_results})
                return 0

            if not scan_results:
                print(f"OK: no legacy records found for mission {args.mission_id!r}")
                return 0

            print(f"Mission: {args.mission_id}")
            print(f"Found {len(scan_results)} task(s) with migration considerations:\n")

            for result in scan_results:
                print(f"  Task: {result['task_id']}")
                print(f"    Classification: {result['classification']}")
                print(f"    Legacy exists:  {result['legacy_exists']}")
                print(f"    Current exists: {result['current_exists']}")
                if result.get("persisted_linkage"):
                    print(f"    Persisted link: {result['persisted_linkage']}")
                if result.get("metadata_valid") is not None:
                    print(f"    Metadata valid: {result['metadata_valid']}")
                print()

            return 0

        # Plan mode (deterministic action proposal)
        if getattr(args, "plan_migration", False):
            scan_results = scan_mission_migration(args.mission_id, store, queue)
            plan = plan_mission_migration(scan_results)

            if getattr(args, "json_output", False):
                _print_json({"plan": plan})
                return 0

            if not plan:
                print(f"OK: no migration actions needed for mission {args.mission_id!r}")
                return 0

            print(f"Mission: {args.mission_id}")
            print(f"Proposed {len(plan)} migration action(s):\n")

            for action_item in plan:
                action_type = action_item["action"]
                task_id = action_item["task_id"]
                reason = action_item["reason"]

                print(f"  [{action_type}] {task_id}")
                print(f"    Reason: {reason}")
                if action_item.get("queue_task_id"):
                    print(f"    Queue ID: {action_item['queue_task_id']}")
                if action_item.get("legacy_queue_id"):
                    print(f"    Legacy ID: {action_item['legacy_queue_id']}")
                print()

            print("Run with --apply to execute these actions")
            return 0

        # Apply mode (safe mutation)
        if args.apply:
            scan_results = scan_mission_migration(args.mission_id, store, queue)
            plan = plan_mission_migration(scan_results)

            if not plan:
                print(f"OK: no migration actions needed for mission {args.mission_id!r}")
                return 0

            apply_result = apply_mission_migration(args.mission_id, plan, store, queue)

            if getattr(args, "json_output", False):
                _print_json(apply_result)
                return 0 if apply_result["success"] else 1

            print(f"Mission: {args.mission_id}")
            print(f"Applied {apply_result['actions_applied']} migration action(s)")

            if apply_result["warnings"]:
                print(f"\nWarnings ({len(apply_result['warnings'])}):")
                for warning in apply_result["warnings"]:
                    print(f"  {warning}")

            if apply_result["errors"]:
                print(f"\nErrors ({len(apply_result['errors'])}):")
                for error in apply_result["errors"]:
                    print(f"  {error}")

            if apply_result["success"]:
                print("\nOK: migration completed successfully")
                return 0
            else:
                print("\nFAIL: migration encountered errors")
                return 1

    print(f"Unknown mission action: {action}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = ControllerConfig.from_json(args.config)
    runner = _runner(settings)

    if args.command == "init":
        _ensure_local_directories(settings)
        DurableQueue(settings.queue_root)
        _ensure_remote_directory(runner, settings.worktree_root)
        return 0

    if args.command == "enqueue":
        queue = DurableQueue(settings.queue_root)
        task = Task.from_dict(json.loads(args.task.read_text(encoding="utf-8")))
        print(queue.enqueue(task))
        return 0

    if args.command == "recover":
        queue = DurableQueue(settings.queue_root)
        age = args.older_than_seconds or settings.orphan_after_seconds
        print(json.dumps(queue.recover_orphans(older_than_seconds=age)))
        return 0

    if args.command == "status":
        payload = _queue_counts(settings.queue_root)
        payload["latest_report"] = _latest_report_path(settings.reports_root).name if _latest_report_path(settings.reports_root) else None
        _print_json(payload)
        return 0

    if args.command == "report":
        payload = _report_payload(settings.reports_root, args.task_id)
        if payload is None:
            print("No report found", flush=True)
            return 1
        _print_json(payload)
        return 0

    if args.command == "doctor":
        result = _doctor(settings, runner)
        if getattr(args, "json_output", False):
            _print_json(result)
        return 0 if result["ok"] else 1

    if args.command == "smoke-local":
        return _smoke_local(settings)

    if args.command == "mission":
        return _mission_command(args, settings)

    controller = build(settings)
    if args.command == "run":
        if args.once:
            result = controller.run_once()
            return 0 if result is not False else 1
        controller.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
