from __future__ import annotations

import argparse
import json
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
    stderr_excerpt: str
    classification: str
    recommended_node: str


CLASSIFICATIONS = {
    "requests": ("required Fedora core dependency", "fedora"),
    "flask": ("required Fedora core dependency", "fedora"),
    "sqlite3": ("required Fedora core dependency", "fedora"),
    "numpy": ("optional accelerated dependency", "windows-worker"),
    "pandas": ("Windows-worker-only dependency", "windows-worker"),
    "scipy": ("Windows-worker-only dependency", "windows-worker"),
    "sklearn": ("Windows-worker-only dependency", "windows-worker"),
}


def diagnose_module(
    package: str, *, command: list[str] | None = None
) -> DiagnosticResult:
    process = subprocess.run(
        command or [sys.executable, "-c", f"import {package}"],
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
        stderr_excerpt=process.stderr.strip().replace("\n", " ")[:500],
        classification=classification,
        recommended_node=node,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    results = [
        diagnose_module(name)
        for name in ("requests", "flask", "sqlite3", "numpy", "pandas", "scipy", "sklearn")
    ]
    if arguments.json:
        print(json.dumps([asdict(item) for item in results], indent=2))
    else:
        for item in results:
            print(
                f"{item.package:<22} {item.status:<8} "
                f"{item.classification:<34} {item.recommended_node}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
