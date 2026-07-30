from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Sequence

from .models import ProcessResult, utc_now


class LocalRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        input_text: str | None = None,
        timeout_seconds: float = 600,
    ) -> ProcessResult:
        started = utc_now()
        try:
            completed = subprocess.run(
                list(argv),
                cwd=str(cwd),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return ProcessResult(
                list(argv), str(cwd), completed.returncode, completed.stdout,
                completed.stderr, started, utc_now(), False
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else (error.stderr or "")
            return ProcessResult(
                list(argv), str(cwd), None, stdout, stderr, started, utc_now(), True
            )
        except OSError as error:
            return ProcessResult(
                list(argv), str(cwd), None, "", str(error), started, utc_now(), False
            )


class SSHRunner:
    _MARKER = "__RAGHUB_CONTROLLER_RESULT__"

    def __init__(
        self,
        *,
        ssh_executable: str,
        host: str,
        key_path: str,
        remote_python: str = "python3",
    ) -> None:
        self.ssh_executable = ssh_executable
        self.host = host
        self.key_path = key_path
        self.remote_python = remote_python

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        input_text: str | None = None,
        timeout_seconds: float = 600,
    ) -> ProcessResult:
        payload = base64.b64encode(
            json.dumps(
                {"argv": list(argv), "cwd": str(cwd), "stdin": input_text},
                ensure_ascii=False,
            ).encode("utf-8")
        ).decode("ascii")
        script = (
            "import base64,json,subprocess\n"
            f"p=json.loads(base64.b64decode({payload!r}).decode('utf-8'))\n"
            "try:\n"
            " r=subprocess.run(p['argv'],cwd=p['cwd'],input=p['stdin'],text=True,"
            "capture_output=True,check=False)\n"
            " d={'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr}\n"
            "except OSError as e:\n"
            " d={'exit_code':None,'stdout':'','stderr':str(e)}\n"
            f"print({self._MARKER!r}+base64.b64encode(json.dumps(d,ensure_ascii=False).encode()).decode())\n"
        )
        started = utc_now()
        transport = LocalRunner().run(
            [
                self.ssh_executable,
                "-i",
                self.key_path,
                "-o",
                "BatchMode=yes",
                self.host,
                self.remote_python,
                "-",
            ],
            cwd=Path.cwd(),
            input_text=script,
            timeout_seconds=timeout_seconds,
        )
        if transport.timed_out:
            return ProcessResult(list(argv), str(cwd), None, "", transport.stderr, started, utc_now(), True)
        line = next(
            (item for item in reversed(transport.stdout.splitlines()) if item.startswith(self._MARKER)),
            None,
        )
        if line is None:
            error = transport.stderr or transport.stdout or "SSH returned no structured result"
            return ProcessResult(list(argv), str(cwd), transport.exit_code, "", error, started, utc_now(), False)
        decoded = json.loads(base64.b64decode(line[len(self._MARKER):]).decode("utf-8"))
        return ProcessResult(
            list(argv), str(cwd), decoded["exit_code"], decoded["stdout"], decoded["stderr"],
            started, utc_now(), False
        )
