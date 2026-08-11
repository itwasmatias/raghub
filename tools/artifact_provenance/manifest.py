from __future__ import annotations

import hashlib
import json
import os
import re
import stat as statmod
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from tools.ai_controller._locking import FileLock


MANIFEST_SCHEMA_VERSION = "artifact-provenance-manifest-v0.1"
DIGEST_VERIFIED = "DIGEST_VERIFIED"
DEFAULT_DIGEST_ALGORITHM = "sha256"
DEFAULT_CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_SIZE = 10 * 1024 * 1024  # 10MB reasonable limit

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_INVALID_LOGICAL_CHARS = set('<>:"|?*\0\r\n')


class ArtifactProvenanceError(Exception):
    exit_code = 1


class ManifestFormatError(ArtifactProvenanceError):
    exit_code = 10


class ArtifactMissingError(ArtifactProvenanceError):
    exit_code = 11


class ArtifactDigestMismatchError(ArtifactProvenanceError):
    exit_code = 12


class ArtifactMutationError(ArtifactProvenanceError):
    exit_code = 13


class UnsafePathError(ArtifactProvenanceError):
    exit_code = 14


class UnsupportedArtifactTypeError(ArtifactProvenanceError):
    exit_code = 15


class UnsupportedAlgorithmError(ArtifactProvenanceError):
    exit_code = 15


class ManifestWriteError(ArtifactProvenanceError):
    exit_code = 16


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    logical_path: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ArtifactProvenanceManifest:
    schema_version: str
    guarantee: str
    algorithm: str
    artifacts: tuple[ArtifactRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "guarantee": self.guarantee,
            "algorithm": self.algorithm,
            "artifacts": [record.to_dict() for record in self.artifacts],
        }

    def to_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_dict()) + b"\n"

    def summary_lines(self) -> tuple[str, ...]:
        lines = [
            f"schema_version: {self.schema_version}",
            f"guarantee: {self.guarantee} (digest matching only; not authentication)",
            f"algorithm: {self.algorithm}",
            f"artifacts: {len(self.artifacts)}",
        ]
        for record in self.artifacts:
            lines.append(
                f"- {record.logical_path} | {record.size_bytes} bytes | {record.sha256}"
            )
        return tuple(lines)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
    *,
    error_cls: type[ArtifactProvenanceError],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise error_cls(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str, *, error_cls: type[ArtifactProvenanceError]) -> None:
    raise error_cls(f"non-finite JSON constant: {token}")


def _require_non_negative_int(
    value: Any,
    field_name: str,
    *,
    error_cls: type[ArtifactProvenanceError],
) -> int:
    if type(value) is not int or value < 0:
        raise error_cls(f"{field_name} must be a non-negative integer")
    return value


def _require_sha256(
    value: Any,
    field_name: str,
    *,
    error_cls: type[ArtifactProvenanceError],
) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise error_cls(f"{field_name} must be a lowercase SHA-256 hex digest")
    return value


def _normalize_logical_path(
    value: Any,
    *,
    error_cls: type[ArtifactProvenanceError],
) -> str:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if type(value) is not str:
        raise error_cls("logical_path must be a string")
    if not value:
        raise error_cls("logical_path must be non-empty")
    if value != value.strip():
        raise error_cls("logical_path must not have surrounding whitespace")
    if any(ch in value for ch in _INVALID_LOGICAL_CHARS):
        raise error_cls("logical_path contains forbidden characters")
    candidate = value.replace("\\", "/")
    if candidate.startswith(("/", "//")):
        raise error_cls("logical_path must be relative")
    if _WINDOWS_DRIVE_RE.match(candidate):
        raise error_cls("logical_path must be relative")
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise error_cls("logical_path contains an unsafe component")
    normalized_parts = []
    for part in parts:
        collapsed = part.rstrip(" .")
        if not collapsed:
            raise error_cls("logical_path contains an unsafe component")
        if collapsed.upper() in _WINDOWS_RESERVED_NAMES:
            raise error_cls("logical_path contains a Windows reserved name")
        normalized_parts.append(collapsed)
    return "/".join(normalized_parts)


def _resolve_root(root: str | os.PathLike[str]) -> Path:
    root_path = Path(os.fspath(root))
    try:
        resolved = root_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise UnsafePathError("artifact root does not exist") from exc
    except OSError as exc:
        raise UnsafePathError(f"artifact root is not readable: {exc}") from exc
    if not resolved.is_dir():
        raise UnsafePathError("artifact root is not a directory")
    return resolved


def _close_fds(fds: list[int]) -> None:
    while fds:
        fd = fds.pop()
        try:
            os.close(fd)
        except OSError:
            pass


def _identity_snapshot(stat_result: os.stat_result) -> tuple[int, int, int, int | None, int | None]:
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns) if hasattr(stat_result, "st_mtime_ns") else None,
        int(stat_result.st_ctime_ns) if hasattr(stat_result, "st_ctime_ns") else None,
    )


def _can_use_dirfd_open() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
    )


def _open_regular_file_under_root(
    root: Path,
    logical_path: str,
) -> tuple[int, os.stat_result, list[int]]:
    parts = logical_path.split("/")
    open_fds: list[int] = []
    if _can_use_dirfd_open():
        try:
            current_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            open_fds.append(current_fd)
            for part in parts[:-1]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
                open_fds.append(next_fd)
                current_fd = next_fd
                if not statmod.S_ISDIR(os.fstat(current_fd).st_mode):
                    raise UnsupportedArtifactTypeError("artifact path is not a directory")
            leaf_path = root.joinpath(*parts)
            leaf_lstat = os.lstat(leaf_path)
            if os.path.islink(leaf_path):
                raise UnsafePathError("artifact path is a symlink")
            if not statmod.S_ISREG(leaf_lstat.st_mode):
                raise UnsupportedArtifactTypeError("artifact path is not a regular file")
            leaf_fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current_fd,
            )
            open_fds.append(leaf_fd)
            leaf_stat = os.fstat(leaf_fd)
            if _identity_snapshot(leaf_lstat) != _identity_snapshot(leaf_stat):
                raise ArtifactMutationError("artifact changed before hashing")
            if not statmod.S_ISREG(leaf_stat.st_mode):
                raise UnsupportedArtifactTypeError("artifact path is not a regular file")
            return leaf_fd, leaf_stat, open_fds
        except FileNotFoundError as exc:
            _close_fds(open_fds)
            raise ArtifactMissingError("artifact file is missing") from exc
        except OSError as exc:
            _close_fds(open_fds)
            raise UnsafePathError(f"artifact path could not be opened safely: {exc}") from exc
        except Exception:
            _close_fds(open_fds)
            raise

    candidate = root.joinpath(*parts)
    try:
        leaf_lstat = None
        for index in range(1, len(parts) + 1):
            current = root.joinpath(*parts[:index])
            current_stat = os.lstat(current)
            if index < len(parts):
                if os.path.islink(current):
                    raise UnsafePathError("artifact path contains a symlink component")
                if not statmod.S_ISDIR(current_stat.st_mode):
                    raise UnsupportedArtifactTypeError("artifact path is not a directory")
            else:
                if os.path.islink(current):
                    raise UnsafePathError("artifact path is a symlink")
                if not statmod.S_ISREG(current_stat.st_mode):
                    raise UnsupportedArtifactTypeError("artifact path is not a regular file")
                leaf_lstat = current_stat
        resolved_candidate = candidate.resolve(strict=True)
        if resolved_candidate != candidate:
            raise UnsafePathError("artifact path contains a symlink component")
        leaf_fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        open_fds.append(leaf_fd)
        leaf_stat = os.fstat(leaf_fd)
        if leaf_lstat is None:
            raise UnsafePathError("artifact path could not be resolved safely")
        if _identity_snapshot(leaf_lstat) != _identity_snapshot(leaf_stat):
            raise ArtifactMutationError("artifact changed before hashing")
        if not statmod.S_ISREG(leaf_stat.st_mode):
            raise UnsupportedArtifactTypeError("artifact path is not a regular file")
        return leaf_fd, leaf_stat, open_fds
    except FileNotFoundError as exc:
        _close_fds(open_fds)
        raise ArtifactMissingError("artifact file is missing") from exc
    except OSError as exc:
        _close_fds(open_fds)
        raise UnsafePathError(f"artifact path could not be opened safely: {exc}") from exc
    except Exception:
        _close_fds(open_fds)
        raise


def _hash_file(
    root: Path,
    logical_path: str,
    *,
    chunk_size: int,
) -> tuple[ArtifactRecord, tuple[int, int, int, int | None, int | None], tuple[int, int, int, int | None, int | None]]:
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    leaf_fd, pre_stat, open_fds = _open_regular_file_under_root(root, logical_path)
    digest = hashlib.sha256()
    size = 0
    try:
        pre_identity = _identity_snapshot(pre_stat)
        with os.fdopen(os.dup(leaf_fd), "rb", closefd=True) as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        post_stat = os.fstat(leaf_fd)
        post_identity = _identity_snapshot(post_stat)
        if pre_identity != post_identity or size != pre_stat.st_size:
            raise ArtifactMutationError("artifact changed while being read")
        return (
            ArtifactRecord(
                logical_path=logical_path,
                size_bytes=size,
                sha256=digest.hexdigest(),
            ),
            pre_identity,
            post_identity,
        )
    finally:
        _close_fds(open_fds)


def _artifact_resolved_path(root: Path, logical_path: str) -> Path:
    return root.joinpath(*logical_path.split("/"))


def _same_file(path_a: Path, path_b: Path) -> bool:
    try:
        return path_a.exists() and path_b.exists() and path_a.samefile(path_b)
    except OSError:
        return False


def _validate_artifact_record(
    record: Mapping[str, Any],
    *,
    error_cls: type[ArtifactProvenanceError],
) -> ArtifactRecord:
    if type(record) is not dict or set(record) != {"logical_path", "size_bytes", "sha256"}:
        raise error_cls("artifact record schema is invalid")
    logical_path = _normalize_logical_path(record["logical_path"], error_cls=error_cls)
    if logical_path != record["logical_path"]:
        raise error_cls("artifact logical_path is not canonical")
    size_bytes = _require_non_negative_int(record["size_bytes"], "size_bytes", error_cls=error_cls)
    sha256 = _require_sha256(record["sha256"], "sha256", error_cls=error_cls)
    return ArtifactRecord(logical_path=logical_path, size_bytes=size_bytes, sha256=sha256)


def _validate_manifest_dict(
    payload: Mapping[str, Any],
    *,
    error_cls: type[ArtifactProvenanceError],
) -> ArtifactProvenanceManifest:
    if type(payload) is not dict or set(payload) != {"schema_version", "guarantee", "algorithm", "artifacts"}:
        raise error_cls("manifest schema is invalid")
    if payload["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise error_cls("manifest schema version is invalid")
    if payload["guarantee"] != DIGEST_VERIFIED:
        raise error_cls("manifest guarantee is invalid")
    if payload["algorithm"] != DEFAULT_DIGEST_ALGORITHM:
        raise error_cls("manifest digest algorithm is unsupported")
    if type(payload["artifacts"]) is not list:
        raise error_cls("manifest artifacts must be a list")
    records: list[ArtifactRecord] = []
    seen: set[str] = set()
    for item in payload["artifacts"]:
        record = _validate_artifact_record(item, error_cls=error_cls)
        collision_key = record.logical_path.casefold()
        if collision_key in seen:
            raise error_cls("duplicate normalized artifact path")
        seen.add(collision_key)
        records.append(record)
    records.sort(key=lambda item: (item.logical_path.casefold(), item.logical_path))
    return ArtifactProvenanceManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        guarantee=DIGEST_VERIFIED,
        algorithm=DEFAULT_DIGEST_ALGORITHM,
        artifacts=tuple(records),
    )


def load_manifest(path: str | os.PathLike[str]) -> ArtifactProvenanceManifest:
    manifest_path = Path(os.fspath(path))
    try:
        stat_result = manifest_path.stat()
        if stat_result.st_size > MAX_MANIFEST_SIZE:
            raise ManifestFormatError(
                f"manifest exceeds maximum size of {MAX_MANIFEST_SIZE} bytes"
            )
        raw = manifest_path.read_bytes()
    except FileNotFoundError as exc:
        raise ManifestFormatError("manifest file is missing") from exc
    except OSError as exc:
        raise ManifestFormatError(f"manifest file is unreadable: {exc}") from exc
    if not raw:
        raise ManifestFormatError("manifest is empty")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=lambda pairs: _reject_duplicate_keys(
                pairs, error_cls=ManifestFormatError
            ),
            parse_constant=lambda token: _reject_constant(
                token, error_cls=ManifestFormatError
            ),
        )
    except UnicodeDecodeError as exc:
        raise ManifestFormatError("manifest is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ManifestFormatError("manifest is malformed JSON") from exc

    manifest = _validate_manifest_dict(payload, error_cls=ManifestFormatError)
    if manifest.to_bytes() != raw:
        raise ManifestFormatError("manifest is not canonical")
    return manifest


def inspect_manifest(path: str | os.PathLike[str]) -> ArtifactProvenanceManifest:
    return load_manifest(path)


def build_manifest(
    root: str | os.PathLike[str],
    artifacts: Iterable[str | os.PathLike[str]] | str | os.PathLike[str],
    *,
    algorithm: str = DEFAULT_DIGEST_ALGORITHM,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ArtifactProvenanceManifest:
    if algorithm != DEFAULT_DIGEST_ALGORITHM:
        raise UnsupportedAlgorithmError("only sha256 is supported in v0.1")
    if isinstance(artifacts, (str, bytes, os.PathLike)):
        artifacts = [artifacts]
    root_path = _resolve_root(root)
    seen: set[str] = set()
    records: list[ArtifactRecord] = []
    for raw_path in artifacts:
        logical_path = _normalize_logical_path(raw_path, error_cls=UnsafePathError)
        collision_key = logical_path.casefold()
        if collision_key in seen:
            raise UnsafePathError("duplicate normalized artifact path")
        seen.add(collision_key)
        record, _, _ = _hash_file(root_path, logical_path, chunk_size=chunk_size)
        records.append(record)
    records.sort(key=lambda item: (item.logical_path.casefold(), item.logical_path))
    return ArtifactProvenanceManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        guarantee=DIGEST_VERIFIED,
        algorithm=DEFAULT_DIGEST_ALGORITHM,
        artifacts=tuple(records),
    )


def _resolve_output_path(output_path: str | os.PathLike[str]) -> Path:
    output = Path(os.fspath(output_path))
    try:
        return output.resolve(strict=False)
    except OSError as exc:
        raise UnsafePathError(f"output path is not usable: {exc}") from exc


def _ensure_output_is_not_selected_artifact(
    output_path: str | os.PathLike[str],
    root: Path,
    manifest: ArtifactProvenanceManifest,
) -> None:
    output = Path(os.fspath(output_path))
    for record in manifest.artifacts:
        candidate = _artifact_resolved_path(root, record.logical_path)
        if _same_file(output, candidate):
            raise UnsafePathError("output manifest aliases a selected artifact")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> Path:
    output = Path(os.fspath(path))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
        _fsync_directory(output.parent)
        return output
    except Exception as exc:
        raise ManifestWriteError(f"failed to write manifest: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                if temporary_path.exists():
                    temporary_path.unlink()
            except FileNotFoundError:
                pass


def create_manifest(
    root: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    artifacts: Iterable[str | os.PathLike[str]] | str | os.PathLike[str],
    *,
    algorithm: str = DEFAULT_DIGEST_ALGORITHM,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ArtifactProvenanceManifest:
    output = Path(os.fspath(output_path))
    lock_path = output.parent / f".{output.name}.lock"
    with FileLock(lock_path):
        root_path = _resolve_root(root)
        manifest = build_manifest(
            root_path,
            artifacts,
            algorithm=algorithm,
            chunk_size=chunk_size,
        )
        _ensure_output_is_not_selected_artifact(output_path, root_path, manifest)
        _atomic_write_bytes(output_path, manifest.to_bytes())
        return manifest


def verify_manifest(
    root: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ArtifactProvenanceManifest:
    root_path = _resolve_root(root)
    manifest = load_manifest(manifest_path)
    selected_manifest_path = Path(os.fspath(manifest_path))
    for record in manifest.artifacts:
        candidate = _artifact_resolved_path(root_path, record.logical_path)
        if _same_file(selected_manifest_path, candidate):
            raise UnsafePathError("manifest file aliases a selected artifact")
        observed_record, _, _ = _hash_file(root_path, record.logical_path, chunk_size=chunk_size)
        if observed_record.size_bytes != record.size_bytes:
            raise ArtifactDigestMismatchError(
                f"byte size mismatch for {record.logical_path}"
            )
        if observed_record.sha256 != record.sha256:
            raise ArtifactDigestMismatchError(f"digest mismatch for {record.logical_path}")
    return manifest
