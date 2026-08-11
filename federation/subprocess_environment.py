from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath


POLICY_ID = "subprocess-environment-containment-v0.1"

_POSIX_DEFAULT_ENV = {
    "HOME": "/",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
    "TMPDIR": "/tmp",
}

_WINDOWS_DEFAULT_ENV = {
    "ComSpec": r"C:\Windows\System32\cmd.exe",
    "HOME": r"C:\Windows\Temp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": r"C:\Windows\System32;C:\Windows",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
    "TEMP": r"C:\Windows\Temp",
    "TMP": r"C:\Windows\Temp",
    "TMPDIR": r"C:\Windows\Temp",
    "USERPROFILE": r"C:\Windows\Temp",
    "WINDIR": r"C:\Windows",
    "SystemRoot": r"C:\Windows",
}

_ALLOWED_TRUSTED_ADDITION_NAMES = frozenset({"LANG", "LC_ALL", "TERM", "TZ"})
_DANGEROUS_ENVIRONMENT_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "DB_PASSWORD",
        "DATABASE_URL",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GIT_SSH_COMMAND",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "OPENAI_API_KEY",
        "PYTHONHOME",
        "PYTHONPATH",
        "RAGHUB_CONTROLLER_TOKENS",
        "RAGHUB_INTEGRITY_KEY",
        "SSH_AUTH_SOCK",
    }
)
_DANGEROUS_ENVIRONMENT_NAMES_CASEFOLDED = frozenset(
    item.casefold() for item in _DANGEROUS_ENVIRONMENT_NAMES
)
_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_windows(platform_name: str) -> bool:
    return platform_name == "nt"


def _default_environment(platform_name: str) -> dict[str, str]:
    return dict(_WINDOWS_DEFAULT_ENV if _is_windows(platform_name) else _POSIX_DEFAULT_ENV)


def _path_separator(platform_name: str) -> str:
    return ";" if _is_windows(platform_name) else ":"


def _path_validator(platform_name: str):
    return PureWindowsPath if _is_windows(platform_name) else PurePosixPath


def _normalize_name(name: str, *, platform_name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("environment variable name must be a non-empty string")
    if "\0" in name:
        raise ValueError("environment variable name contains a null byte")
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"environment variable name {name!r} is not valid")
    if _is_windows(platform_name):
        return name.upper()
    return name


def _validate_value(name: str, value: object) -> str:
    if type(value) is not str:
        raise ValueError(f"environment variable {name!r} value must be a string")
    if "\0" in value:
        raise ValueError(f"environment variable {name!r} value contains a null byte")
    if "\n" in value or "\r" in value:
        raise ValueError(f"environment variable {name!r} value contains a newline")
    if value == "":
        raise ValueError(f"environment variable {name!r} value must be non-empty")
    return value


def _validate_path(path: str, *, platform_name: str) -> str:
    if type(path) is not str or not path:
        raise ValueError("path must be a non-empty string")
    separator = _path_separator(platform_name)
    path_type = _path_validator(platform_name)
    parts = path.split(separator)
    if any(not part for part in parts):
        raise ValueError("path entries must be non-empty")
    for part in parts:
        candidate = path_type(part)
        if not candidate.is_absolute():
            raise ValueError(f"path entry must be absolute: {part!r}")
    return path


def _is_denied_name(name: str) -> bool:
    return name.casefold() in _DANGEROUS_ENVIRONMENT_NAMES_CASEFOLDED


def build_subprocess_environment(
    *,
    path: str | None = None,
    platform_name: str | None = None,
    trusted_additions: Mapping[str, str] | None = None,
) -> dict[str, str]:
    platform_name = os.name if platform_name is None else platform_name
    environment = _default_environment(platform_name)
    environment["PATH"] = _validate_path(
        path if path is not None else environment["PATH"],
        platform_name=platform_name,
    )

    if trusted_additions:
        for raw_name, raw_value in sorted(
            trusted_additions.items(), key=lambda item: item[0].casefold()
        ):
            name = _normalize_name(raw_name, platform_name=platform_name)
            canonical_name = name if not _is_windows(platform_name) else name.upper()
            if _is_denied_name(canonical_name):
                raise ValueError(f"environment variable {raw_name!r} is not allowed")
            if canonical_name not in _ALLOWED_TRUSTED_ADDITION_NAMES:
                raise ValueError(f"environment variable {raw_name!r} is not allowed")
            environment[canonical_name] = _validate_value(canonical_name, raw_value)

    return dict(environment)


def subprocess_environment_assignments(environment: Mapping[str, str]) -> tuple[str, ...]:
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be a mapping")
    return tuple(
        f"{name}={value}"
        for name, value in sorted(environment.items(), key=lambda item: item[0].casefold())
    )


def subprocess_environment_fingerprint(environment: Mapping[str, str]) -> str:
    payload = {
        "environment": [
            [name, value]
            for name, value in sorted(environment.items(), key=lambda item: item[0].casefold())
        ],
        "policy_id": POLICY_ID,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
