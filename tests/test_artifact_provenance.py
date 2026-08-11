from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import tools.artifact_provenance.manifest as provenance
from tools.artifact_provenance import (
    ArtifactDigestMismatchError,
    ArtifactMissingError,
    ArtifactMutationError,
    ArtifactProvenanceManifest,
    ArtifactRecord,
    ManifestFormatError,
    ManifestWriteError,
    UnsafePathError,
    UnsupportedAlgorithmError,
    UnsupportedArtifactTypeError,
    build_manifest,
    create_manifest,
    inspect_manifest,
    load_manifest,
    verify_manifest,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def write_file(root: Path, relative: str, data: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "tools.artifact_provenance", *map(str, args)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def canonical_manifest_bytes(root: Path, files: list[tuple[str, bytes]]) -> bytes:
    for relative, data in files:
        write_file(root, relative, data)
    manifest = build_manifest(root, [relative for relative, _ in files])
    return manifest.to_bytes()


def test_identical_inputs_produce_byte_identical_manifests(tmp_path: Path) -> None:
    root = tmp_path / "root"
    before = canonical_manifest_bytes(
        root,
        [
            ("b.txt", b"beta"),
            ("a.txt", b"alpha"),
        ],
    )
    after = build_manifest(root, ["b.txt", "a.txt"]).to_bytes()
    assert before == after


def test_input_order_does_not_change_manifest_bytes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    write_file(root, "b.txt", b"beta")
    manifest_1 = build_manifest(root, ["a.txt", "b.txt"]).to_bytes()
    manifest_2 = build_manifest(root, ["b.txt", "a.txt"]).to_bytes()
    assert manifest_1 == manifest_2


def test_normalized_logical_paths_sort_deterministically(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "nested/a.txt", b"alpha")
    write_file(root, "b.txt", b"beta")
    manifest = build_manifest(root, ["b.txt", "nested/a.txt"])
    assert [record.logical_path for record in manifest.artifacts] == [
        "b.txt",
        "nested/a.txt",
    ] or [record.logical_path for record in manifest.artifacts] == [
        "nested/a.txt",
        "b.txt",
    ]
    # The canonical byte order is stable even when the input order is not.
    assert manifest.to_bytes() == build_manifest(root, ["nested/a.txt", "b.txt"]).to_bytes()


def test_no_timestamps_or_absolute_root_paths_stabilize_output(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    manifest_bytes = build_manifest(root, ["a.txt"]).to_bytes()
    assert str(root).encode("utf-8") not in manifest_bytes
    assert b"created_at" not in manifest_bytes
    assert b"2026-" not in manifest_bytes


def test_same_bytes_and_paths_reproduce_same_manifest(tmp_path: Path) -> None:
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    for root in (root_a, root_b):
        write_file(root, "a.txt", b"alpha")
        write_file(root, "b.txt", b"beta")
    manifest_a = build_manifest(root_a, ["a.txt", "b.txt"]).to_bytes()
    manifest_b = build_manifest(root_b, ["a.txt", "b.txt"]).to_bytes()
    assert manifest_a == manifest_b


def test_matching_files_pass(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    write_file(root, "b.txt", b"beta")
    manifest = create_manifest(root, tmp_path / "manifest.json", ["a.txt", "b.txt"])
    verified = verify_manifest(root, tmp_path / "manifest.json")
    assert verified == manifest


def test_modified_bytes_fail(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    write_file(root, "a.txt", b"omega")
    with pytest.raises(ArtifactDigestMismatchError, match="digest mismatch"):
        verify_manifest(root, output)


def test_same_size_modified_bytes_fail(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    write_file(root, "a.txt", b"omega")
    with pytest.raises(ArtifactDigestMismatchError):
        verify_manifest(root, output)


def test_missing_file_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    path.unlink()
    with pytest.raises(ArtifactMissingError):
        verify_manifest(root, output)


def test_unrelated_files_do_not_change_listed_verification_semantics(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    write_file(root, "unrelated.txt", b"noise")
    assert verify_manifest(root, output).artifacts[0].logical_path == "a.txt"


def test_malformed_digest_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    manifest = build_manifest(root, ["a.txt"])
    payload = json.loads(manifest.to_bytes())
    payload["artifacts"][0]["sha256"] = "not-a-digest"
    malformed = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(malformed, encoding="utf-8")
    with pytest.raises(ManifestFormatError):
        load_manifest(manifest_path)


def test_unsupported_algorithm_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    with pytest.raises(UnsupportedAlgorithmError):
        build_manifest(root, ["a.txt"], algorithm="sha512")


def test_wrong_byte_size_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    write_file(root, "a.txt", b"shorter")
    with pytest.raises(ArtifactDigestMismatchError, match="byte size mismatch"):
        verify_manifest(root, output)


def test_duplicate_manifest_path_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    manifest = build_manifest(root, ["a.txt"])
    payload = json.loads(manifest.to_bytes())
    payload["artifacts"].append(payload["artifacts"][0])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestFormatError, match="duplicate normalized artifact path"):
        load_manifest(manifest_path)


def test_manifest_entry_order_does_not_bypass_canonical_validation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    write_file(root, "b.txt", b"beta")
    manifest = build_manifest(root, ["a.txt", "b.txt"])
    payload = json.loads(manifest.to_bytes())
    payload["artifacts"] = list(reversed(payload["artifacts"]))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestFormatError, match="canonical"):
        load_manifest(manifest_path)


@pytest.mark.parametrize(
    "artifact_path",
    [
        "/absolute/path.txt",
        "../escape.txt",
        "nested/../../escape.txt",
        "bad\0name",
        "bad\nname",
        "bad\rname",
        "C:\\escape.txt",
    ],
)
def test_rejected_paths(artifact_path: str, tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "safe.txt", b"alpha")
    with pytest.raises(UnsafePathError):
        build_manifest(root, [artifact_path])


def test_symlink_input_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = write_file(outside, "payload.txt", b"alpha")
    link = root / "link.txt"
    root.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)
    with pytest.raises(UnsafePathError):
        build_manifest(root, ["link.txt"])


def test_symlinked_parent_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    payload = write_file(outside, "payload.txt", b"alpha")
    root.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, root / "linked")
    with pytest.raises(UnsafePathError):
        build_manifest(root, ["linked/payload.txt"])


def test_output_manifest_included_as_input_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "manifest.json", b"artifact bytes")
    with pytest.raises(UnsafePathError, match="aliases a selected artifact"):
        create_manifest(root, root / "manifest.json", ["manifest.json"])


def test_duplicate_normalized_path_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "nested/a.txt", b"alpha")
    with pytest.raises(UnsafePathError, match="duplicate normalized artifact path"):
        build_manifest(root, ["nested/a.txt", Path("nested") / "a.txt"])


@pytest.mark.parametrize("artifact_path", ["bad\0name", "bad\nname", "bad\rname"])
def test_control_characters_in_logical_paths_are_rejected(artifact_path: str) -> None:
    with pytest.raises(UnsafePathError):
        provenance._normalize_logical_path(artifact_path, error_cls=UnsafePathError)


def test_directory_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "directory").mkdir(parents=True, exist_ok=True)
    with pytest.raises(UnsupportedArtifactTypeError):
        build_manifest(root, ["directory"])


def test_fifo_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    fifo = root / "pipe"
    root.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)
    with pytest.raises(UnsupportedArtifactTypeError):
        build_manifest(root, ["pipe"])


def test_socket_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    sock_path = root / "socket.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        with pytest.raises(UnsupportedArtifactTypeError):
            build_manifest(root, ["socket.sock"])
    finally:
        server.close()
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass


def test_device_rejected_when_supported(tmp_path: Path) -> None:
    if not hasattr(os, "mknod") or os.geteuid() != 0:
        pytest.skip("device files are not safely creatable in this environment")
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    device = root / "device"
    os.mknod(device, 0o600 | statmod.S_IFCHR, os.makedev(1, 3))
    with pytest.raises(UnsupportedArtifactTypeError):
        build_manifest(root, ["device"])


def test_artifact_mutation_during_hashing_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    path = write_file(root, "artifact.txt", b"0123456789abcdef")

    original_fdopen = provenance.os.fdopen

    class MutatingReader:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped
            self._mutated = False

        def read(self, size: int = -1) -> bytes:
            chunk = self._wrapped.read(size)
            if chunk and not self._mutated:
                path.write_bytes(b"fedcba9876543210")
                self._mutated = True
            return chunk

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

        def __enter__(self) -> "MutatingReader":
            self._wrapped.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb) -> object:
            return self._wrapped.__exit__(exc_type, exc, tb)

    def fake_fdopen(*args, **kwargs):
        return MutatingReader(original_fdopen(*args, **kwargs))

    monkeypatch.setattr(provenance.os, "fdopen", fake_fdopen)
    with pytest.raises(ArtifactMutationError):
        build_manifest(root, ["artifact.txt"], chunk_size=4)


def test_failed_creation_preserves_previous_valid_manifest_and_cleans_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    write_file(root, "b.txt", b"beta")
    output = tmp_path / "manifest.json"
    original = create_manifest(root, output, ["a.txt"])
    before = output.read_bytes()
    original_replace = provenance.os.replace

    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(provenance.os, "replace", boom)
    with pytest.raises(ManifestWriteError):
        create_manifest(root, output, ["b.txt"])

    assert output.read_bytes() == before
    assert load_manifest(output) == original
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))

    monkeypatch.setattr(provenance.os, "replace", original_replace)
    replacement = create_manifest(root, output, ["b.txt"])
    assert replacement.artifacts[0].logical_path == "b.txt"


def test_concurrent_writers_do_not_produce_torn_json(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "alpha.txt", b"alpha")
    write_file(root, "beta.txt", b"beta")
    output = tmp_path / "manifest.json"
    candidate_a = build_manifest(root, ["alpha.txt"]).to_bytes()
    candidate_b = build_manifest(root, ["beta.txt"]).to_bytes()

    def writer(artifacts: list[str]) -> None:
        create_manifest(root, output, artifacts)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(writer, ["alpha.txt"]),
            executor.submit(writer, ["beta.txt"]),
            executor.submit(writer, ["alpha.txt"]),
            executor.submit(writer, ["beta.txt"]),
            executor.submit(writer, ["alpha.txt"]),
            executor.submit(writer, ["beta.txt"]),
        ]
        for future in futures:
            future.result()

    final_bytes = output.read_bytes()
    assert final_bytes in {candidate_a, candidate_b}
    assert load_manifest(output).to_bytes() == final_bytes
    assert final_bytes.endswith(b"\n")


def test_verifier_never_mutates_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    artifact_before = (root / "a.txt").read_bytes()
    manifest_before = output.read_bytes()
    verify_manifest(root, output)
    inspect_manifest(output)
    assert (root / "a.txt").read_bytes() == artifact_before
    assert output.read_bytes() == manifest_before


def test_artifact_contents_never_appear_in_manifest(tmp_path: Path) -> None:
    root = tmp_path / "root"
    secret = b"super-secret-payload"
    write_file(root, "a.txt", secret)
    manifest_bytes = build_manifest(root, ["a.txt"]).to_bytes()
    assert secret not in manifest_bytes
    assert str(root).encode("utf-8") not in manifest_bytes


def test_failures_do_not_expose_synthetic_secret_contents(tmp_path: Path) -> None:
    root = tmp_path / "root"
    secret = b"super-secret-payload"
    write_file(root, "a.txt", secret)
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    write_file(root, "a.txt", b"other-secret-payload")
    with pytest.raises(ArtifactDigestMismatchError) as excinfo:
        verify_manifest(root, output)
    assert secret.decode("utf-8") not in str(excinfo.value)
    assert "other-secret-payload" not in str(excinfo.value)


def test_cli_create_success(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    write_file(root, "b.txt", b"beta")
    output = tmp_path / "manifest.json"
    result = run_cli("create", "--root", root, "--output", output, "a.txt", "b.txt")
    assert result.returncode == 0
    assert "created DIGEST_VERIFIED manifest with 2 artifacts" in result.stdout
    assert result.stderr == ""
    assert load_manifest(output).artifacts[0].logical_path == "a.txt"


def test_cli_verify_success(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    result = run_cli("verify", "--root", root, "--manifest", output)
    assert result.returncode == 0
    assert "verified DIGEST_VERIFIED manifest with 1 artifacts" in result.stdout
    assert result.stderr == ""


def test_cli_mismatch_exit_code(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    write_file(root, "a.txt", b"omega")
    result = run_cli("verify", "--root", root, "--manifest", output)
    assert result.returncode == ArtifactDigestMismatchError.exit_code
    assert result.stdout == ""
    assert "digest mismatch" in result.stderr


def test_cli_unsafe_path_exit_code(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    result = run_cli(
        "create",
        "--root",
        root,
        "--output",
        output,
        "/absolute/path.txt",
    )
    assert result.returncode == UnsafePathError.exit_code
    assert result.stdout == ""
    assert "relative" in result.stderr or "absolute" in result.stderr


def test_cli_malformed_manifest_exit_code(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{\"not\": \"canonical\"}\n", encoding="utf-8")
    result = run_cli("inspect", "--manifest", manifest_path)
    assert result.returncode == ManifestFormatError.exit_code
    assert result.stdout == ""
    assert result.stderr


def test_cli_inspect_output_includes_guarantee_limitation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    result = run_cli("inspect", "--manifest", output)
    assert result.returncode == 0
    assert "DIGEST_VERIFIED" in result.stdout
    assert "digest matching only; not authentication" in result.stdout


def test_cli_stdout_stderr_separation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    result = run_cli("create", "--root", root, "--output", output, "a.txt")
    assert result.returncode == 0
    assert result.stdout
    assert result.stderr == ""


def test_cli_verify_and_inspect_do_not_mutate(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    output = tmp_path / "manifest.json"
    create_manifest(root, output, ["a.txt"])
    artifact_before = (root / "a.txt").read_bytes()
    manifest_before = output.read_bytes()
    verify_result = run_cli("verify", "--root", root, "--manifest", output)
    inspect_result = run_cli("inspect", "--manifest", output)
    assert verify_result.returncode == 0
    assert inspect_result.returncode == 0
    assert (root / "a.txt").read_bytes() == artifact_before
    assert output.read_bytes() == manifest_before


def test_windows_path_forms_are_handled_explicitly() -> None:
    assert provenance._normalize_logical_path("nested\\file.txt", error_cls=UnsafePathError) == "nested/file.txt"
    with pytest.raises(UnsafePathError):
        provenance._normalize_logical_path("C:\\nested\\file.txt", error_cls=UnsafePathError)


def test_trailing_space_dot_normalized() -> None:
    """Trailing spaces and dots in path components are normalized to prevent Windows filesystem confusion."""
    # These pass initial whitespace check but have trailing space/dot in components
    assert provenance._normalize_logical_path("file.txt.", error_cls=UnsafePathError) == "file.txt"
    assert provenance._normalize_logical_path("nested./file.txt", error_cls=UnsafePathError) == "nested/file.txt"
    assert provenance._normalize_logical_path("nested /file.txt", error_cls=UnsafePathError) == "nested/file.txt"
    assert provenance._normalize_logical_path("a./b.", error_cls=UnsafePathError) == "a/b"
    # Surrounding whitespace is still rejected
    with pytest.raises(UnsafePathError, match="surrounding whitespace"):
        provenance._normalize_logical_path("file.txt ", error_cls=UnsafePathError)
    with pytest.raises(UnsafePathError, match="surrounding whitespace"):
        provenance._normalize_logical_path(" file.txt", error_cls=UnsafePathError)


def test_oversized_manifest_rejected(tmp_path: Path) -> None:
    """Manifests exceeding MAX_MANIFEST_SIZE are rejected to prevent memory exhaustion."""
    manifest_path = tmp_path / "huge.json"
    manifest_path.write_bytes(b"x" * (provenance.MAX_MANIFEST_SIZE + 1))
    with pytest.raises(ManifestFormatError, match="exceeds maximum size"):
        load_manifest(manifest_path)


def test_dirfd_fallback_is_feature_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    write_file(root, "a.txt", b"alpha")
    monkeypatch.setattr(provenance, "_can_use_dirfd_open", lambda: False)
    manifest = build_manifest(root, ["a.txt"])
    assert manifest.artifacts[0].logical_path == "a.txt"


def test_module_has_no_unconditional_posix_only_imports() -> None:
    assert "fcntl" not in provenance.__dict__
