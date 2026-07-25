from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    package: str
    status: str
    exit_code: int
    signal: int | None
    installed_version: str | None
    import_result: str
    minimal_operation_result: str
    stderr_excerpt: str
    classification: str
    recommended_node: str
    why_installed: str
    cpu_flags: tuple[str, ...]


CLASSIFICATIONS: dict[str, tuple[str, str]] = {
    "flask": ("required Fedora core web runtime", "fedora-core"),
    "requests": ("required Fedora core network client", "fedora-core"),
    "sqlite3": ("required Fedora core persistence", "fedora-core"),
    "pydantic_core": ("required by OpenAI client validation", "fedora-core"),
    "tiktoken": ("required by OpenAI connector tokenization", "fedora-core"),
    "regex": ("transitive dependency of tiktoken", "fedora-core"),
    "psycopg": ("PostgreSQL driver used by repository database layer", "fedora-core"),
    "psycopg_binary": ("native backend for psycopg", "fedora-core"),
    "psycopg2": ("legacy/duplicate PostgreSQL driver", "optional"),
    "psycopg2_binary": ("legacy/duplicate PostgreSQL driver", "optional"),
    "numpy": ("numeric stack used by Windows worker workloads", "windows-worker"),
    "pandas": ("tabular analytics used by Windows worker workloads", "windows-worker"),
    "scipy": ("scientific stack used by Windows worker workloads", "windows-worker"),
    "sklearn": ("ML stack used by Windows worker workloads", "windows-worker"),
    "cryptography": ("optional native crypto support", "optional"),
    "lxml": ("optional accelerated XML parser", "optional"),
    "orjson": ("optional accelerated JSON parser", "optional"),
    "PIL": ("optional image processing support", "optional"),
    "tokenizers": ("optional accelerated tokenizer runtime", "optional"),
}

PACKAGE_NAMES = {
    "sklearn": "scikit-learn",
    "psycopg_binary": "psycopg-binary",
    "psycopg2": "psycopg2-binary",
    "psycopg2_binary": "psycopg2-binary",
    "PIL": "Pillow",
}

MINIMAL_OPERATIONS: dict[str, str] = {
    "sqlite3": "import sqlite3; sqlite3.connect(':memory:').execute('select 1').fetchone()",
    "numpy": "import numpy; numpy.array([1.0, 2.0]).sum()",
    "pandas": "import pandas; pandas.Series([1.0, 2.0]).sum()",
    "scipy": "import scipy; from scipy.special import expit; expit(0.0)",
    "sklearn": "import sklearn; from sklearn.metrics import log_loss; log_loss([0, 1], [[.9, .1], [.1, .9]])",
    "pydantic_core": "import pydantic_core; pydantic_core.SchemaValidator({'type': 'int'}).validate_python('1')",
    "tiktoken": "import tiktoken; tiktoken.get_encoding('cl100k_base').encode('probe text')",
    "psycopg": "import psycopg; psycopg.adapt.AdaptersMap()",
    "psycopg_binary": "import psycopg, psycopg_binary",
    "psycopg2": "import psycopg2; psycopg2.extensions.adapt('probe').getquoted()",
    "psycopg2_binary": "import psycopg2; psycopg2.extensions.adapt('probe').getquoted()",
    "regex": "import regex; regex.compile(r'probe').match('probe')",
    "cryptography": "from cryptography.hazmat.primitives import hashes; hashes.Hash(hashes.SHA256())",
    "lxml": "from lxml import etree; etree.fromstring(b'<root/>')",
    "orjson": "import orjson; orjson.dumps({'probe': True})",
    "PIL": "from PIL import Image; Image.new('RGB', (1, 1)).tobytes()",
    "tokenizers": "from tokenizers import Tokenizer; from tokenizers.models import WordLevel; Tokenizer(WordLevel({'[UNK]': 0}, unk_token='[UNK]'))",
}

DEFAULT_CANDIDATE_MODULES = {
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "pydantic_core",
    "tiktoken",
    "regex",
    "cryptography",
    "lxml",
    "psycopg",
    "psycopg_binary",
    "psycopg2",
    "orjson",
    "PIL",
    "tokenizers",
    "flask",
    "requests",
    "sqlite3",
}

REQUIREMENTS_FILE_NAMES = (
    "requirements.txt",
    "requirements-fedora-core.txt",
    "requirements-windows-worker.txt",
    "requirements-dev.txt",
)


def _workspace_root() -> Path:
    return Path.cwd()


def _parse_requirement_name(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-r"):
        return None
    if stripped.startswith(("-e", "--")):
        return None
    match = re.match(r"([A-Za-z0-9_.-]+)", stripped)
    if not match:
        return None
    return match.group(1)


def _collect_requirement_distributions(root: Path) -> set[str]:
    discovered: set[str] = set()
    for filename in REQUIREMENTS_FILE_NAMES:
        path = root / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            name = _parse_requirement_name(line)
            if name:
                discovered.add(name.lower().replace("_", "-"))
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8")
        for token in re.findall(r'"([A-Za-z0-9_.-]+)(?:[<>=!~].*?)?"', content):
            discovered.add(token.lower().replace("_", "-"))
    return discovered


def _collect_repo_import_roots(root: Path) -> set[str]:
    imports: set[str] = set()
    for py_path in root.rglob("*.py"):
        parts = py_path.parts
        if ".venv" in parts or ".git" in parts or "__pycache__" in parts:
            continue
        try:
            source = py_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
    return imports


def _distribution_name(raw_name: str) -> str:
    return raw_name.lower().replace("_", "-")


def _collect_installed_compiled_distributions() -> set[str]:
    compiled: set[str] = set()
    for distribution in importlib.metadata.distributions():
        files = distribution.files or ()
        if any(str(item).endswith((".so", ".pyd", ".dylib")) for item in files):
            name = distribution.metadata.get("Name")
            if name:
                compiled.add(_distribution_name(name))
    return compiled


def _top_levels_for_distribution(
    distribution: importlib.metadata.Distribution,
) -> set[str]:
    discovered: set[str] = set()
    top_level = distribution.read_text("top_level.txt")
    if top_level:
        for line in top_level.splitlines():
            module = line.strip()
            if module:
                discovered.add(module)
    if discovered:
        return discovered
    files = distribution.files or ()
    for item in files:
        item_str = str(item)
        if item_str.endswith(".py") and "/" in item_str:
            discovered.add(item_str.split("/", 1)[0])
        elif item_str.endswith((".so", ".pyd")):
            stem = Path(item_str).name.split(".", 1)[0]
            if stem and stem not in {"__init__"}:
                discovered.add(stem)
    return discovered


def _build_distribution_top_levels() -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        mapping[_distribution_name(name)] = _top_levels_for_distribution(distribution)
    return mapping


def _cpu_flags() -> tuple[str, ...]:
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.exists():
        return (platform.machine(),)
    for line in cpuinfo.read_text(encoding="utf-8").splitlines():
        if line.startswith("flags"):
            return tuple(line.partition(":")[2].strip().split())
    return (platform.machine(),)


def _safe_command_output(command: list[str], *, max_chars: int = 20000) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except Exception as error:  # pragma: no cover - best effort diagnostics
        return f"<unavailable: {error}>"
    output = completed.stdout.strip()
    if not output:
        output = completed.stderr.strip()
    if len(output) > max_chars:
        output = output[:max_chars] + "\n...<truncated>"
    return output


def _safe_stderr(stderr: str) -> str:
    compact = " ".join(stderr.strip().split())
    return compact[:500]


def _child_probe_command(package: str, operation: str | None) -> list[str]:
    operation_json = json.dumps(operation or "")
    code = "\n".join(
        [
            "import importlib",
            "import json",
            "package = " + json.dumps(package),
            "operation = " + operation_json,
            "payload = {'import_success': False, 'minimal_operation_success': False, 'import_error': '', 'operation_error': ''}",
            "try:",
            "    importlib.import_module(package)",
            "    payload['import_success'] = True",
            "except Exception as exc:",
            "    payload['import_error'] = f'{type(exc).__name__}: {exc}'",
            "if payload['import_success'] and operation:",
            "    try:",
            "        exec(operation, {})",
            "        payload['minimal_operation_success'] = True",
            "    except Exception as exc:",
            "        payload['operation_error'] = f'{type(exc).__name__}: {exc}'",
            "elif payload['import_success']:",
            "    payload['minimal_operation_success'] = True",
            "print(json.dumps(payload))",
        ]
    )
    return [sys.executable, "-c", code]


def _resolve_classification(package: str) -> tuple[str, str]:
    return CLASSIFICATIONS.get(package, ("optional dependency", "windows-worker"))


def _recommended_reason(package: str) -> str:
    reason, _node = _resolve_classification(package)
    return reason


def discover_diagnostic_candidates() -> list[str]:
    root = _workspace_root()
    requirement_distributions = _collect_requirement_distributions(root)
    repo_imports = _collect_repo_import_roots(root)
    compiled_distributions = _collect_installed_compiled_distributions()
    distribution_top_levels = _build_distribution_top_levels()

    modules: set[str] = set(DEFAULT_CANDIDATE_MODULES)

    for distribution_name, top_levels in distribution_top_levels.items():
        if distribution_name in compiled_distributions and (
            distribution_name in requirement_distributions
            or any(name in repo_imports for name in top_levels)
        ):
            modules.update(top_levels)

    # Keep default modules and externally relevant modules only.
    filtered: set[str] = set()
    for module in modules:
        distribution = _distribution_name(PACKAGE_NAMES.get(module, module))
        if (
            module in repo_imports
            or distribution in requirement_distributions
            or module in DEFAULT_CANDIDATE_MODULES
        ):
            filtered.add(module)

    # Probe in deterministic order for stable reports.
    return sorted(filtered)


def diagnose_module(
    package: str, *, command: list[str] | None = None
) -> DiagnosticResult:
    distribution = PACKAGE_NAMES.get(package, package)
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        version = None
    operation = MINIMAL_OPERATIONS.get(package)
    process = subprocess.run(
        command or _child_probe_command(package, operation),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    caught_signal = -process.returncode if process.returncode < 0 else None
    payload: dict[str, object] = {}
    if process.returncode == 0:
        try:
            payload = json.loads(process.stdout.strip() or "{}")
        except json.JSONDecodeError:
            payload = {}

    if caught_signal == signal.SIGILL or process.returncode == 128 + signal.SIGILL:
        status = "SIGILL"
    elif process.returncode != 0:
        status = "BLOCKED"
    elif payload.get("import_success") and payload.get("minimal_operation_success"):
        status = "PASS"
    else:
        status = "BLOCKED"
    classification, node = _resolve_classification(package)
    import_result = "unknown"
    minimal_result = "unknown"
    if process.returncode == 0 and payload:
        import_result = "pass" if payload.get("import_success") else "fail"
        minimal_result = "pass" if payload.get("minimal_operation_success") else "fail"
    elif status == "SIGILL":
        import_result = "sigill"
        minimal_result = "sigill"
    else:
        import_result = status.lower()
        minimal_result = status.lower()

    stderr_excerpt = _safe_stderr(process.stderr)
    if not stderr_excerpt and process.returncode == 0 and payload:
        combined_error = " ".join(
            value
            for value in [
                payload.get("import_error", ""),
                payload.get("operation_error", ""),
            ]
            if value
        )
        stderr_excerpt = combined_error[:500]

    return DiagnosticResult(
        package=package,
        status=status,
        exit_code=process.returncode,
        signal=caught_signal
        or (signal.SIGILL if process.returncode == 128 + signal.SIGILL else None),
        installed_version=version,
        import_result=import_result,
        minimal_operation_result=minimal_result,
        stderr_excerpt=stderr_excerpt,
        classification=classification,
        recommended_node=node,
        why_installed=_recommended_reason(package),
        cpu_flags=_cpu_flags(),
    )


def _system_snapshot() -> dict[str, str]:
    return {
        "uname": _safe_command_output(["uname", "-a"]),
        "lscpu": _safe_command_output(["lscpu"]),
        "python_version": _safe_command_output([sys.executable, "--version"]),
        "pip_version": _safe_command_output([sys.executable, "-m", "pip", "--version"]),
        "pip_freeze": _safe_command_output([sys.executable, "-m", "pip", "freeze"]),
    }


def _format_table(results: Iterable[DiagnosticResult]) -> str:
    lines = []
    for item in results:
        lines.append(
            f"{item.package:<16} {item.status:<7} "
            f"v={item.installed_version or 'not-installed':<16} "
            f"import={item.import_result:<6} "
            f"op={item.minimal_operation_result:<6} "
            f"exit={item.exit_code:<4} "
            f"sig={item.signal or '-':<3} "
            f"node={item.recommended_node:<14} "
            f"why={item.why_installed}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--packages", nargs="*", default=None)
    arguments = parser.parse_args()
    candidates = arguments.packages or discover_diagnostic_candidates()
    results = [diagnose_module(name) for name in candidates]
    system_snapshot = _system_snapshot()
    if arguments.json:
        print(
            json.dumps(
                {
                    "system": system_snapshot,
                    "results": [asdict(item) for item in results],
                },
                indent=2,
            )
        )
    else:
        print("== Runtime Environment ==")
        print(f"uname -a: {system_snapshot['uname']}")
        print("lscpu:")
        print(system_snapshot["lscpu"])
        print(f"python --version: {system_snapshot['python_version']}")
        print(f"python -m pip --version: {system_snapshot['pip_version']}")
        print("python -m pip freeze:")
        print(system_snapshot["pip_freeze"])
        print()
        print("== Native Dependency Diagnostics ==")
        print(_format_table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
