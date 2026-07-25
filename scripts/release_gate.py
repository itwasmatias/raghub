from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sports.release.demo import NBAReplayDemo  # noqa: E402
from sports.release.settings import ReleaseSettings  # noqa: E402


def run(command: list[str], timeout: int) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        timeout=timeout,
    )


def main() -> int:
    settings = ReleaseSettings.from_environment()
    print(
        f"Release environment: {settings.environment}; "
        f"read-only demo: {settings.demo_read_only}",
        flush=True,
    )
    python = str(ROOT / ".venv" / "bin" / "python")
    pytest = str(ROOT / ".venv" / "bin" / "pytest")
    run(
        [
            pytest,
            "-q",
            "tests/test_release_candidate.py",
            "tests/test_release_pipeline.py",
            "tests/test_release_operations.py",
            "tests/test_nba_opportunity_intelligence_loop.py",
        ],
        timeout=120,
    )
    run([pytest, "-q"], timeout=300)
    with TemporaryDirectory(prefix="raghub-release-gate-") as directory:
        result = NBAReplayDemo(Path(directory) / "demo.db").run()
    if result["situation"]["current_stage"] != "learn":
        raise RuntimeError("Flagship replay did not reach the Learn stage.")
    run(
        [
            python,
            "-m",
            "compileall",
            "-q",
            "app.py",
            "sports",
            "intelligence",
            "services",
        ],
        timeout=120,
    )
    run(["git", "diff", "--check"], timeout=30)
    print("Automated release gate passed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
