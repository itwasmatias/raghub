from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
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


CLASSIFICATIONS = {
    "requests": ("required Fedora core dependency", "fedora"),
    "flask": ("required Fedora core dependency", "fedora"),
    "sqlite3": ("required Fedora core dependency", "fedora"),
    "numpy": ("optional accelerated dependency", "windows-worker"),
    "pandas": ("Windows-worker-only dependency", "windows-worker"),
    "scipy": ("Windows-worker-only dependency", "windows-worker"),
    "sklearn": ("Windows-worker-only dependency", "windows-worker"),
    "pydantic_core": ("OpenAI client validation dependency", "fedora"),
    "tiktoken": ("OpenAI connector tokenization dependency", "fedora"),
    "psycopg": ("pgvector connector dependency", "fedora"),
    "psycopg_binary": ("psycopg native implementation", "fedora"),
    "psycopg2": ("duplicate PostgreSQL driver; not required", "optional"),
    "regex": ("tiktoken transitive dependency", "optional"),
    "jiter": ("OpenAI client transitive dependency", "fedora"),
    "charset_normalizer": ("requests transitive dependency", "fedora"),
    "markupsafe": ("Flask/Jinja template dependency", "fedora"),
}

PACKAGE_NAMES = {
    "sklearn": "scikit-learn",
    "psycopg_binary": "psycopg-binary",
    "psycopg2": "psycopg2-binary",
}

MINIMAL_OPERATIONS = {
    "sqlite3": "import sqlite3; sqlite3.connect(':memory:').execute('select 1').fetchone()",
    "numpy": "import numpy; numpy.array([1.0, 2.0]).sum()",
    "pandas": "import pandas; pandas.Series([1.0, 2.0]).sum()",
    "scipy": "import scipy; from scipy.special import expit; expit(0.0)",
    "sklearn": "import sklearn; from sklearn.metrics import log_loss; log_loss([0, 1], [[.9, .1], [.1, .9]])",
    "pydantic_core": "import pydantic_core; pydantic_core.SchemaValidator({'type': 'int'}).validate_python('1')",
    "tiktoken": "from tiktoken import _tiktoken; _tiktoken.CoreBPE({b'a': 0}, {}, r'(?s).')",
    "psycopg": "import psycopg; psycopg.adapt.AdaptersMap()",
    "psycopg_binary": "import psycopg, psycopg_binary",
    "psycopg2": "import psycopg2; psycopg2.extensions.adapt('probe').getquoted()",
    "regex": "import regex; regex.compile(r'probe').match('probe')",
    "markupsafe": "import markupsafe; markupsafe.escape('<probe>')",
    "jiter": "import jiter; jiter.from_json(b'{\"ok\": true}')",
    "charset_normalizer": "import charset_normalizer; charset_normalizer.from_bytes(b'probe').best()",
}


def _cpu_flags() -> tuple[str, ...]:
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.exists():
        return (platform.machine(),)
    for line in cpuinfo.read_text(encoding="utf-8").splitlines():
        if line.startswith("flags"):
            return tuple(line.partition(":")[2].strip().split())
    return (platform.machine(),)


def diagnose_module(
    package: str, *, command: list[str] | None = None
) -> DiagnosticResult:
    distribution = PACKAGE_NAMES.get(package, package)
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        version = None
    operation = MINIMAL_OPERATIONS.get(package, f"import {package}")
    process = subprocess.run(
        command or [sys.executable, "-c", operation],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    caught_signal = -process.returncode if process.returncode < 0 else None
    if caught_signal == signal.SIGILL or process.returncode == 128 + signal.SIGILL:
        status = "SIGILL"
    elif process.returncode == 0:
        status = "PASS"
    else:
        status = "BLOCKED"
    classification, node = CLASSIFICATIONS.get(
        package, ("optional dependency", "windows-worker")
    )
    return DiagnosticResult(
        package=package,
        status=status,
        exit_code=process.returncode,
        signal=caught_signal or (
            signal.SIGILL if process.returncode == 128 + signal.SIGILL else None
        ),
        installed_version=version,
        import_result=("pass" if process.returncode == 0 else status.lower()),
        minimal_operation_result=(
            "pass" if process.returncode == 0 else status.lower()
        ),
        stderr_excerpt=process.stderr.strip().replace("\n", " ")[:500],
        classification=classification,
        recommended_node=node,
        why_installed=classification,
        cpu_flags=_cpu_flags(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    results = [
        diagnose_module(name)
        for name in (
            "requests", "flask", "sqlite3", "charset_normalizer", "markupsafe",
            "pydantic_core", "jiter", "tiktoken", "regex", "psycopg",
            "psycopg_binary", "psycopg2", "numpy", "pandas", "scipy", "sklearn",
        )
    ]
    if arguments.json:
        print(json.dumps([asdict(item) for item in results], indent=2))
    else:
        for item in results:
            print(
                f"{item.package:<22} {item.installed_version or 'not-installed':<14} "
                f"{item.status:<8} operation={item.minimal_operation_result:<8} "
                f"exit={item.exit_code:<4} signal={item.signal or '-':<3} "
                f"{item.classification} ({item.recommended_node})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
