from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .config import ControllerConfig
from .controller import Controller
from .experience import ExperienceLedger
from .logs import EventLog
from .models import Task
from .providers import CodexProvider, LocalOllamaProvider
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
    commands.add_parser("doctor")
    return result


def _doctor_check(label: str, ok: bool, detail: str) -> bool:
    status = "OK" if ok else "FAIL"
    print(f"[{status}] {label}: {detail}")
    return ok


def _ollama_check(config: ControllerConfig) -> bool:
    try:
        with urlopen(f"{config.ollama_base_url.rstrip('/')}/api/version", timeout=config.ollama_request_timeout_seconds) as response:
            if response.status != 200:
                return _doctor_check("ollama version", False, f"HTTP {response.status}")
        with urlopen(f"{config.ollama_base_url.rstrip('/')}/api/tags", timeout=config.ollama_request_timeout_seconds) as response:
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
        ok = True
        ok &= _doctor_check("controller root", settings.controller_root.exists(), str(settings.controller_root))
        ok &= _doctor_check(
            "queue directories",
            all((settings.queue_root / name).exists() for name in ("pending", "running", "succeeded", "failed", "invalid")),
            str(settings.queue_root),
        )
        ok &= _doctor_check("reports directory", settings.reports_root.exists(), str(settings.reports_root))
        ok &= _doctor_check("logs directory", settings.logs_root.exists(), str(settings.logs_root))
        ok &= _doctor_check("ledger directory", settings.experience_path.parent.exists(), str(settings.experience_path.parent))
        try:
            with FileLock(settings.process_lock_path):
                ok &= _doctor_check("process lock", True, str(settings.process_lock_path))
        except Exception as error:
            ok &= _doctor_check("process lock", False, str(error))

        if settings.ssh_host:
            ssh_ok = True
            ssh_executable = settings.ssh_executable or "ssh"
            if Path(ssh_executable).exists():
                ssh_ok &= _doctor_check("ssh executable", True, ssh_executable)
            else:
                ssh_ok &= _doctor_check("ssh executable", shutil.which(ssh_executable) is not None, ssh_executable)
            ssh_ok &= _doctor_check("ssh key", Path(settings.ssh_key_path or "").exists(), settings.ssh_key_path or "")
            repo_check = runner.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=settings.repository_path, timeout_seconds=60)
            ssh_ok &= _doctor_check(
                "fedora repository",
                repo_check.exit_code == 0,
                repo_check.stdout.strip() or repo_check.stderr.strip() or str(settings.repository_path),
            )
            codex_check = runner.run(
                [settings.codex_executable, "--version"],
                cwd=settings.repository_path,
                timeout_seconds=60,
            )
            ssh_ok &= _doctor_check(
                "fedora codex",
                codex_check.exit_code == 0,
                codex_check.stdout.strip() or codex_check.stderr.strip() or settings.codex_executable,
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
            ssh_ok &= _doctor_check(
                "fedora worktree root",
                worktree_check.exit_code == 0 and worktree_check.stdout.strip() == "1",
                str(settings.worktree_root),
            )
            ok &= ssh_ok
        else:
            ok &= _doctor_check("ssh", False, "ssh_host is not configured")

        ok &= _ollama_check(settings)
        return 0 if ok else 1

    controller = build(settings)
    if args.once:
        result = controller.run_once()
        return 0 if result is not False else 1
    controller.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
