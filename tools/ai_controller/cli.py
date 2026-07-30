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
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
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

    controller = build(settings)
    if args.command == "run":
        if args.once:
            result = controller.run_once()
            return 0 if result is not False else 1
        controller.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
