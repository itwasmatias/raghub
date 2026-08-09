"""Adversarial tests for the durable local model artifact registry (v0.1).

These tests use only temporary files. They never download a model, never start
a server, and never touch the network. Concurrency is exercised with real
operating-system processes so that the cross-process ``fcntl`` lock is honored.
"""

import ast
import dataclasses
import hashlib
import inspect
import json
import multiprocessing
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from federation import local_model_artifact_registry as registry_module
from federation.local_model_artifact_registry import (
    DEFAULT_CHUNK_SIZE,
    GGUF_MAGIC,
    ArtifactConflictError,
    ArtifactCorruptionError,
    ArtifactEvent,
    ArtifactFormat,
    ArtifactFormatError,
    ArtifactNotFoundError,
    ArtifactPathError,
    ArtifactRegistration,
    ArtifactState,
    ArtifactStatus,
    ArtifactVerificationError,
    InvalidationReason,
    LocalModelArtifactRegistry,
)


KEY = b"local-model-artifact-registry-v0.1-integrity-key"
WRONG_KEY = b"local-model-artifact-registry-v0.1-the-wrong-key"
FIXED = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


class MutableClock:
    """Deterministic timezone-aware clock."""

    def __init__(self, current=FIXED):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current = self.current + timedelta(seconds=seconds)


def _fixed_clock():
    return FIXED


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gguf_bytes(payload: bytes = b"", *, magic: bytes = GGUF_MAGIC) -> bytes:
    return magic + payload


def write_file(directory: Path, name: str, data: bytes):
    path = directory / name
    path.write_bytes(data)
    return path, sha256_hex(data)


def make_registry(tmp_path, *, key=KEY, clock=None, chunk_size=DEFAULT_CHUNK_SIZE):
    root = tmp_path / "models"
    root.mkdir(exist_ok=True)
    store = tmp_path / "state" / "artifacts.jsonl"
    registry = LocalModelArtifactRegistry(
        store,
        integrity_key=key,
        trusted_roots=[root],
        clock=clock or MutableClock(),
        chunk_size=chunk_size,
    )
    return registry, root, store


def register_valid(registry, root, *, name="model.gguf", payload=b"\x00" * 256, **overrides):
    path, digest = write_file(root, name, gguf_bytes(payload))
    fields = {
        "artifact_id": "artifact-1",
        "model_id": "llama-3-8b",
        "path": str(path),
        "expected_sha256": digest,
        "quantization": "Q4_K_M",
    }
    fields.update(overrides)
    return registry.register(**fields), path, digest


# --- concurrency workers (module level so fork can call them) ---------------


def _worker_register(store, root, key, path, expected, model_id, output):
    registry = LocalModelArtifactRegistry(
        store,
        integrity_key=key,
        trusted_roots=[root],
        clock=_fixed_clock,
    )
    try:
        state = registry.register(
            artifact_id="shared-artifact",
            model_id=model_id,
            path=path,
            expected_sha256=expected,
        )
        output.put(("accepted", state.fingerprint))
    except ArtifactConflictError as exc:
        output.put(("conflict", str(exc)))
    except Exception as exc:  # pragma: no cover - reported to the parent
        output.put((type(exc).__name__, str(exc)))


def _run_workers(target, args_list):
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    processes = [
        context.Process(target=target, args=(*args, output)) for args in args_list
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
    assert all(process.exitcode == 0 for process in processes)
    return [output.get(timeout=5) for _ in processes]


# --- 1. valid GGUF register -------------------------------------------------


def test_valid_gguf_register_binds_immutable_identity(tmp_path):
    registry, root, store = make_registry(tmp_path)
    state, path, digest = register_valid(
        registry,
        root,
        metadata={"provenance": {"source": "local-copy"}},
        runtime_compatibility={"context_length": 8192},
    )

    assert state.status is ArtifactStatus.REGISTERED
    assert state.artifact_id == "artifact-1"
    assert state.model_id == "llama-3-8b"
    assert state.path == str(path)
    assert state.expected_sha256 == digest
    assert state.format is ArtifactFormat.GGUF
    assert state.quantization == "Q4_K_M"
    assert state.size_bytes == os.path.getsize(path)
    assert state.device is not None and state.inode is not None
    assert state.registered_at == FIXED
    assert len(state.fingerprint) == 64
    assert store.read_bytes().endswith(b"\n")


def test_register_requires_absolute_existing_trusted_roots(tmp_path):
    with pytest.raises(ValueError, match="trusted root"):
        LocalModelArtifactRegistry(
            tmp_path / "s.jsonl", integrity_key=KEY, trusted_roots=["relative/models"]
        )
    with pytest.raises(ValueError, match="existing directory"):
        LocalModelArtifactRegistry(
            tmp_path / "s.jsonl",
            integrity_key=KEY,
            trusted_roots=[tmp_path / "does-not-exist"],
        )
    with pytest.raises(ValueError, match="at least one trusted root"):
        LocalModelArtifactRegistry(
            tmp_path / "s.jsonl", integrity_key=KEY, trusted_roots=[]
        )


def test_register_requires_controller_owned_integrity_key(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    with pytest.raises(TypeError, match="bytes"):
        LocalModelArtifactRegistry(
            tmp_path / "s.jsonl", integrity_key=None, trusted_roots=[root]
        )
    with pytest.raises(ValueError, match="at least 32 bytes"):
        LocalModelArtifactRegistry(
            tmp_path / "s.jsonl", integrity_key=b"short", trusted_roots=[root]
        )


# --- 2. exact hash verify ---------------------------------------------------


def test_exact_hash_verification_transitions_to_verified(tmp_path):
    clock = MutableClock()
    registry, root, _ = make_registry(tmp_path, clock=clock)
    register_valid(registry, root)
    clock.advance(30)

    verified = registry.verify("artifact-1")

    assert verified.status is ArtifactStatus.VERIFIED
    assert verified.verified_at == FIXED + timedelta(seconds=30)
    assert verified.last_observed_sha256 == verified.expected_sha256
    assert registry.is_verified("artifact-1") is True


def test_repeated_verification_of_unchanged_file_is_idempotent(tmp_path):
    registry, root, store = make_registry(tmp_path)
    register_valid(registry, root)
    registry.verify("artifact-1")
    before = store.read_bytes()

    again = registry.verify("artifact-1")

    assert again.status is ArtifactStatus.VERIFIED
    assert store.read_bytes() == before


# --- 3. restart -------------------------------------------------------------


def test_state_survives_process_restart(tmp_path):
    registry, root, store = make_registry(tmp_path)
    register_valid(registry, root)
    registry.verify("artifact-1")

    restarted = LocalModelArtifactRegistry(
        store, integrity_key=KEY, trusted_roots=[root], clock=MutableClock()
    )

    resolved = restarted.resolve("artifact-1")
    assert resolved.status is ArtifactStatus.VERIFIED
    assert [event.event_type for event in restarted.history("artifact-1")] == [
        "registered",
        "verified",
    ]


# --- 4. mismatch ------------------------------------------------------------


def test_registration_with_mismatched_hash_fails_closed(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    path, _ = write_file(root, "model.gguf", gguf_bytes(b"real-bytes"))

    with pytest.raises(ArtifactVerificationError):
        registry.register(
            artifact_id="artifact-1",
            model_id="m",
            path=str(path),
            expected_sha256="0" * 64,
        )
    assert not (tmp_path / "state" / "artifacts.jsonl").exists()


def test_non_gguf_bytes_are_rejected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    path, digest = write_file(root, "model.gguf", gguf_bytes(b"x", magic=b"NOPE"))

    with pytest.raises(ArtifactFormatError):
        registry.register(
            artifact_id="artifact-1", model_id="m", path=str(path), expected_sha256=digest
        )


# --- 5 & 24. modification invalidates; current invalid after change ---------


def test_modification_after_verification_invalidates_current_state(tmp_path):
    clock = MutableClock()
    registry, root, _ = make_registry(tmp_path, clock=clock)
    _, path, _ = register_valid(registry, root)
    registry.verify("artifact-1")

    path.write_bytes(gguf_bytes(b"\x00" * 512))
    clock.advance(60)
    invalidated = registry.verify("artifact-1")

    assert invalidated.status is ArtifactStatus.INVALIDATED
    assert invalidated.invalidation_reason is InvalidationReason.SIZE_CHANGED
    assert invalidated.invalidated_at == FIXED + timedelta(seconds=60)
    # current/resolve both reflect the fail-closed state
    assert registry.current("artifact-1").status is ArtifactStatus.INVALIDATED
    assert registry.resolve("artifact-1").status is ArtifactStatus.INVALIDATED
    assert registry.is_verified("artifact-1") is False


def test_same_size_content_mutation_is_detected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    _, path, _ = register_valid(registry, root, payload=b"A" * 256)
    registry.verify("artifact-1")

    path.write_bytes(gguf_bytes(b"B" * 256))  # identical length, different bytes
    invalidated = registry.verify("artifact-1")

    assert invalidated.status is ArtifactStatus.INVALIDATED
    assert invalidated.invalidation_reason is InvalidationReason.CONTENT_MISMATCH


def test_invalidated_artifact_never_resurrects(tmp_path):
    registry, root, store = make_registry(tmp_path)
    _, path, digest = register_valid(registry, root)
    registry.verify("artifact-1")
    path.write_bytes(gguf_bytes(b"\x00" * 4096))
    registry.verify("artifact-1")
    restored = gguf_bytes(b"\x00" * 256)  # exactly restore original bytes
    path.write_bytes(restored)
    before = store.read_bytes()

    still_invalid = registry.verify("artifact-1")

    assert still_invalid.status is ArtifactStatus.INVALIDATED
    assert store.read_bytes() == before  # no resurrection event appended


# --- 6. inode replacement where supported -----------------------------------


def test_inode_replacement_with_identical_bytes_is_detected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    _, path, digest = register_valid(registry, root)
    registry.verify("artifact-1")
    if registry.resolve("artifact-1").inode is None:  # pragma: no cover
        pytest.skip("inode identity is not available on this platform")

    replacement = root / "replacement.gguf"
    replacement.write_bytes(path.read_bytes())  # identical content, new inode
    os.replace(replacement, path)

    invalidated = registry.verify("artifact-1")
    assert invalidated.status is ArtifactStatus.INVALIDATED
    assert invalidated.invalidation_reason is InvalidationReason.INODE_REPLACEMENT


# --- 7. missing -------------------------------------------------------------


def test_missing_file_registration_is_rejected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    with pytest.raises(ArtifactPathError):
        registry.register(
            artifact_id="artifact-1",
            model_id="m",
            path=str(root / "nope.gguf"),
            expected_sha256="0" * 64,
        )


def test_disappearance_after_registration_fails_closed(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    _, path, _ = register_valid(registry, root)
    registry.verify("artifact-1")
    path.unlink()

    invalidated = registry.verify("artifact-1")
    assert invalidated.status is ArtifactStatus.INVALIDATED
    assert invalidated.invalidation_reason is InvalidationReason.DISAPPEARED
    assert invalidated.last_observed_sha256 is None


# --- 8, 9, 10, 11, 12. path safety -----------------------------------------


def test_path_outside_trusted_root_is_rejected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    path, digest = write_file(outside, "model.gguf", gguf_bytes(b"x"))
    with pytest.raises(ArtifactPathError, match="outside"):
        registry.register(
            artifact_id="a", model_id="m", path=str(path), expected_sha256=digest
        )


def test_traversal_escape_is_rejected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    path, digest = write_file(outside, "model.gguf", gguf_bytes(b"x"))
    traversal = root / ".." / "outside" / "model.gguf"
    with pytest.raises(ArtifactPathError):
        registry.register(
            artifact_id="a", model_id="m", path=str(traversal), expected_sha256=digest
        )


def test_symlink_component_and_symlink_escape_are_rejected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target, digest = write_file(outside, "model.gguf", gguf_bytes(b"x"))

    # direct symlink leaf escaping the root
    leaf_link = root / "link.gguf"
    os.symlink(target, leaf_link)
    with pytest.raises(ArtifactPathError):
        registry.register(
            artifact_id="a", model_id="m", path=str(leaf_link), expected_sha256=digest
        )

    # symlinked *directory component* inside the root
    real_dir = root / "real"
    real_dir.mkdir()
    inner, inner_digest = write_file(real_dir, "model.gguf", gguf_bytes(b"y"))
    dir_link = root / "linked-dir"
    os.symlink(real_dir, dir_link)
    with pytest.raises(ArtifactPathError):
        registry.register(
            artifact_id="b",
            model_id="m",
            path=str(dir_link / "model.gguf"),
            expected_sha256=inner_digest,
        )


def test_directory_path_is_rejected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    directory = root / "a-directory"
    directory.mkdir()
    with pytest.raises(ArtifactPathError):
        registry.register(
            artifact_id="a", model_id="m", path=str(directory), expected_sha256="0" * 64
        )


def test_fifo_path_is_rejected_without_blocking(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    fifo = root / "a-fifo"
    os.mkfifo(fifo)
    with pytest.raises(ArtifactPathError):
        registry.register(
            artifact_id="a", model_id="m", path=str(fifo), expected_sha256="0" * 64
        )


def test_socket_path_is_rejected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    sock_path = root / "a.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            server.bind(str(sock_path))
        except OSError:  # pragma: no cover - environment-specific path limits
            pytest.skip("cannot create an AF_UNIX socket at this path")
        with pytest.raises(ArtifactPathError):
            registry.register(
                artifact_id="a",
                model_id="m",
                path=str(sock_path),
                expected_sha256="0" * 64,
            )
    finally:
        server.close()


# --- 13. malformed hash -----------------------------------------------------


@pytest.mark.parametrize(
    "bad_hash",
    [
        "0" * 63,
        "0" * 65,
        "g" * 64,
        "ABCDEF" + "0" * 58,  # uppercase rejected: canonical lowercase only
        "",
    ],
)
def test_malformed_expected_hash_is_rejected(tmp_path, bad_hash):
    registry, root, _ = make_registry(tmp_path)
    path, _ = write_file(root, "model.gguf", gguf_bytes(b"x"))
    with pytest.raises(ValueError, match="SHA-256"):
        registry.register(
            artifact_id="a", model_id="m", path=str(path), expected_sha256=bad_hash
        )


def test_non_string_expected_hash_is_rejected(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    path, _ = write_file(root, "model.gguf", gguf_bytes(b"x"))
    with pytest.raises(TypeError):
        registry.register(
            artifact_id="a", model_id="m", path=str(path), expected_sha256=1234
        )


# --- 14. duplicate idempotency ----------------------------------------------


def test_duplicate_registration_is_idempotent(tmp_path):
    registry, root, store = make_registry(tmp_path)
    first, path, digest = register_valid(registry, root)
    before = store.read_bytes()

    second = registry.register(
        artifact_id="artifact-1",
        model_id="llama-3-8b",
        path=str(path),
        expected_sha256=digest,
        quantization="Q4_K_M",
    )

    assert second == first
    assert store.read_bytes() == before  # no duplicate event


def test_same_content_under_different_identity_is_allowed(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    payload = gguf_bytes(b"identical-bytes")
    path_a, digest = write_file(root, "a.gguf", payload)
    path_b, digest_b = write_file(root, "b.gguf", payload)
    assert digest == digest_b

    state_a = registry.register(
        artifact_id="artifact-a", model_id="model-a", path=str(path_a), expected_sha256=digest
    )
    state_b = registry.register(
        artifact_id="artifact-b", model_id="model-b", path=str(path_b), expected_sha256=digest
    )

    assert state_a.expected_sha256 == state_b.expected_sha256
    assert state_a.fingerprint != state_b.fingerprint
    assert {s.artifact_id for s in registry.list_artifacts()} == {
        "artifact-a",
        "artifact-b",
    }


# --- 15. conflict -----------------------------------------------------------


def test_conflicting_immutable_identity_is_rejected(tmp_path):
    registry, root, store = make_registry(tmp_path)
    _, path, digest = register_valid(registry, root)
    before = store.read_bytes()

    with pytest.raises(ArtifactConflictError):
        registry.register(
            artifact_id="artifact-1",
            model_id="a-different-model",
            path=str(path),
            expected_sha256=digest,
        )
    assert store.read_bytes() == before


# --- 16 & 17. concurrent registration (real processes) ----------------------


def test_concurrent_exact_registration_produces_one_event(tmp_path):
    registry, root, store = make_registry(tmp_path)
    path, digest = write_file(root, "model.gguf", gguf_bytes(b"\x00" * 4096))

    results = _run_workers(
        _worker_register,
        [(str(store), str(root), KEY, str(path), digest, "same-model") for _ in range(6)],
    )

    statuses = [status for status, _ in results]
    fingerprints = {value for status, value in results if status == "accepted"}
    assert statuses.count("accepted") == 6
    assert len(fingerprints) == 1
    assert len(store.read_text().splitlines()) == 1


def test_concurrent_conflicting_registration_has_one_winner(tmp_path):
    registry, root, store = make_registry(tmp_path)
    path, digest = write_file(root, "model.gguf", gguf_bytes(b"\x00" * 4096))

    results = _run_workers(
        _worker_register,
        [
            (str(store), str(root), KEY, str(path), digest, f"model-{index}")
            for index in range(6)
        ],
    )

    statuses = [status for status, _ in results]
    assert statuses.count("accepted") == 1
    assert statuses.count("conflict") == 5
    assert len(store.read_text().splitlines()) == 1


# --- 18 & 19. tamper, truncate, corrupt -------------------------------------


def test_tampered_store_byte_fails_closed(tmp_path):
    registry, root, store = make_registry(tmp_path)
    register_valid(registry, root)
    raw = bytearray(store.read_bytes())
    raw[40] ^= 0x01
    store.write_bytes(bytes(raw))

    with pytest.raises(ArtifactCorruptionError):
        registry.resolve("artifact-1")


def test_truncated_store_fails_closed(tmp_path):
    registry, root, store = make_registry(tmp_path)
    register_valid(registry, root)
    raw = store.read_bytes()
    store.write_bytes(raw[:-1])  # drop the trailing newline

    with pytest.raises(ArtifactCorruptionError):
        registry.list_artifacts()


def test_partial_line_loss_fails_closed(tmp_path):
    registry, root, store = make_registry(tmp_path)
    register_valid(registry, root)
    raw = store.read_bytes()
    store.write_bytes(raw[: len(raw) // 2] + b"\n")

    with pytest.raises(ArtifactCorruptionError):
        registry.resolve("artifact-1")


def test_duplicate_json_key_is_rejected(tmp_path):
    registry, root, store = make_registry(tmp_path)
    register_valid(registry, root)
    line = store.read_text().splitlines()[0]
    tampered = line[:-1] + ',"sequence":9}'
    store.write_text(tampered + "\n")

    with pytest.raises(ArtifactCorruptionError):
        registry.resolve("artifact-1")


def test_wrong_integrity_key_rejects_existing_evidence(tmp_path):
    registry, root, store = make_registry(tmp_path)
    register_valid(registry, root)

    other = LocalModelArtifactRegistry(
        store, integrity_key=WRONG_KEY, trusted_roots=[root], clock=MutableClock()
    )
    with pytest.raises(ArtifactCorruptionError):
        other.resolve("artifact-1")


# --- 20. nested immutability ------------------------------------------------


def test_metadata_is_deeply_immutable_and_detached(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    metadata = {"provenance": {"chain": [1, 2]}, "labels": ["a"]}
    state, _, _ = register_valid(registry, root, metadata=metadata)

    assert isinstance(state.metadata, MappingProxyType)
    assert isinstance(state.metadata["provenance"], MappingProxyType)
    assert state.metadata["provenance"]["chain"] == (1, 2)
    with pytest.raises(TypeError):
        state.metadata["provenance"] = {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.status = ArtifactStatus.VERIFIED

    metadata["provenance"]["chain"].append(3)  # mutate the caller's original
    assert registry.resolve("artifact-1").metadata["provenance"]["chain"] == (1, 2)


# --- 21. deterministic / changing fingerprint -------------------------------


def test_fingerprint_is_deterministic_and_identity_sensitive(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    path, digest = write_file(root, "model.gguf", gguf_bytes(b"\x00" * 128))
    base = dict(
        artifact_id="artifact-1",
        model_id="m",
        path=str(path),
        expected_sha256=digest,
        quantization="Q4_K_M",
        metadata={"k": "v"},
    )
    first = registry.register(**base).fingerprint
    again = registry.register(**base).fingerprint
    assert first == again

    def fingerprint_with(slot, **changes):
        # Same root and file; a fresh store isolates each variation so that only
        # the field under test differs from the baseline identity.
        other = LocalModelArtifactRegistry(
            tmp_path / f"store-{slot}.jsonl",
            integrity_key=KEY,
            trusted_roots=[root],
            clock=MutableClock(),
        )
        payload = dict(base)
        payload.update(changes)
        return other.register(**payload).fingerprint

    assert fingerprint_with("q", quantization="Q8_0") != first
    assert fingerprint_with("m", model_id="other") != first
    assert fingerprint_with("a", alias="nickname") != first
    assert fingerprint_with("d", display_name="Nickname") != first
    assert fingerprint_with("meta", metadata={"k": "different"}) != first
    assert fingerprint_with("rt", runtime_compatibility={"ctx": 4096}) != first
    # An identical baseline in a fresh store reproduces the same fingerprint.
    assert fingerprint_with("same") == first


def test_changing_bytes_changes_content_identity(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    path_a, digest_a = write_file(root, "a.gguf", gguf_bytes(b"one"))
    path_b, digest_b = write_file(root, "b.gguf", gguf_bytes(b"two"))
    state_a = registry.register(
        artifact_id="a", model_id="m", path=str(path_a), expected_sha256=digest_a
    )
    state_b = registry.register(
        artifact_id="b", model_id="m", path=str(path_b), expected_sha256=digest_b
    )
    assert state_a.expected_sha256 != state_b.expected_sha256
    assert state_a.fingerprint != state_b.fingerprint


def test_replacing_inode_changes_fingerprint(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    path, digest = write_file(root, "model.gguf", gguf_bytes(b"same"))
    first = registry.register(
        artifact_id="a", model_id="m", path=str(path), expected_sha256=digest
    )
    if first.inode is None:  # pragma: no cover
        pytest.skip("inode identity is not available on this platform")

    replacement = root / "replacement.gguf"
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    other = LocalModelArtifactRegistry(
        tmp_path / "other.jsonl",
        integrity_key=KEY,
        trusted_roots=[root],
        clock=MutableClock(),
    )
    second = other.register(
        artifact_id="a", model_id="m", path=str(path), expected_sha256=digest
    )

    assert second.inode != first.inode
    assert second.fingerprint != first.fingerprint


# --- 22. NaN / inf ----------------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [
        {"v": float("nan")},
        {"v": float("inf")},
        {"v": float("-inf")},
        {"nested": {"v": float("nan")}},
        {"nested": [float("inf")]},
    ],
)
def test_non_finite_metadata_is_rejected(tmp_path, metadata):
    registry, root, _ = make_registry(tmp_path)
    path, digest = write_file(root, "model.gguf", gguf_bytes(b"x"))
    with pytest.raises(ValueError, match="finite"):
        registry.register(
            artifact_id="a",
            model_id="m",
            path=str(path),
            expected_sha256=digest,
            metadata=metadata,
        )


@pytest.mark.parametrize("metadata", [{1: "x"}, {"nested": {2: "y"}}, {"o": object()}])
def test_unsafe_metadata_values_are_rejected(tmp_path, metadata):
    registry, root, _ = make_registry(tmp_path)
    path, digest = write_file(root, "model.gguf", gguf_bytes(b"x"))
    with pytest.raises((TypeError, ValueError)):
        registry.register(
            artifact_id="a",
            model_id="m",
            path=str(path),
            expected_sha256=digest,
            metadata=metadata,
        )


def test_stored_nan_in_metadata_is_rejected_on_read(tmp_path):
    registry, root, store = make_registry(tmp_path)
    register_valid(registry, root)
    line = store.read_text().splitlines()[0]
    assert '"metadata":{}' in line
    # Inject a bare NaN token the way a corrupt/tampered store could contain one.
    tampered = line.replace('"metadata":{}', '"metadata":{"v":NaN}')
    store.write_text(tampered + "\n")

    with pytest.raises(ArtifactCorruptionError):
        registry.resolve("artifact-1")


# --- 23. ordering -----------------------------------------------------------


def test_events_are_totally_ordered_and_reordering_is_rejected(tmp_path):
    registry, root, store = make_registry(tmp_path)
    path_a, digest_a = write_file(root, "a.gguf", gguf_bytes(b"a"))
    path_b, digest_b = write_file(root, "b.gguf", gguf_bytes(b"b"))
    registry.register(artifact_id="a", model_id="m", path=str(path_a), expected_sha256=digest_a)
    registry.register(artifact_id="b", model_id="m", path=str(path_b), expected_sha256=digest_b)
    registry.verify("a")

    lines = store.read_text().splitlines()
    assert [json.loads(line)["event_type"] for line in lines] == [
        "registered",
        "registered",
        "verified",
    ]

    reordered = "\n".join([lines[1], lines[0], lines[2]]) + "\n"
    store.write_text(reordered)
    with pytest.raises(ArtifactCorruptionError):
        registry.list_artifacts()


# --- 25. historical verification auditable ----------------------------------


def test_verification_history_is_appended_and_never_rewritten(tmp_path):
    clock = MutableClock()
    registry, root, store = make_registry(tmp_path, clock=clock)
    _, path, _ = register_valid(registry, root)
    clock.advance(10)
    registry.verify("artifact-1")
    verified_snapshot = store.read_bytes()

    clock.advance(10)
    path.write_bytes(gguf_bytes(b"\x00" * 4096))
    registry.verify("artifact-1")

    events = registry.history("artifact-1")
    assert [event.event_type for event in events] == [
        "registered",
        "verified",
        "invalidated",
    ]
    assert all(isinstance(event, ArtifactEvent) for event in events)
    assert events[1].occurred_at == FIXED + timedelta(seconds=10)
    assert events[2].reason is InvalidationReason.SIZE_CHANGED
    # the earlier verified evidence is preserved, not overwritten
    assert store.read_bytes().startswith(verified_snapshot)


# --- 26. chunk hashing ------------------------------------------------------


def test_hashing_uses_bounded_chunks(tmp_path):
    registry, root, _ = make_registry(tmp_path, chunk_size=7)
    payload = os.urandom(100)
    data = gguf_bytes(payload)
    path, digest = write_file(root, "model.gguf", data)

    state = registry.register(
        artifact_id="a", model_id="m", path=str(path), expected_sha256=digest
    )
    assert state.expected_sha256 == hashlib.sha256(data).hexdigest()
    assert state.size_bytes == len(data)
    assert DEFAULT_CHUNK_SIZE > 0

    source = inspect.getsource(registry_module.LocalModelArtifactRegistry._read_evidence)
    assert "handle.read(self._chunk_size)" in source
    assert "handle.read()" not in source


def test_invalid_chunk_size_is_rejected(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    with pytest.raises(ValueError, match="chunk_size"):
        LocalModelArtifactRegistry(
            tmp_path / "s.jsonl", integrity_key=KEY, trusted_roots=[root], chunk_size=0
        )


# --- 27. no key serialization -----------------------------------------------


def test_integrity_key_is_never_serialized(tmp_path):
    registry, root, store = make_registry(tmp_path)
    state, _, _ = register_valid(registry, root)
    registry.verify("artifact-1")

    raw = store.read_bytes()
    assert KEY not in raw
    assert KEY.hex().encode() not in raw
    for line in store.read_text().splitlines():
        assert "authentication_tag" in json.loads(line)
    # snapshots and reprs must not leak the key either
    assert KEY.hex() not in repr(state)
    assert KEY.decode() not in repr(state)
    assert not any("key" in field.name.lower() for field in dataclasses.fields(ArtifactState))


# --- 28 & 29. no subprocess, no network -------------------------------------


def test_module_uses_no_subprocess_or_shell_primitives():
    # Match usage/import patterns rather than bare words so that the module's
    # own prose (which describes what it does *not* do) is not a false positive.
    source = inspect.getsource(registry_module).casefold()
    forbidden = (
        "import subprocess",
        "subprocess.",
        "os.system(",
        "os.popen(",
        "popen(",
        "pty.spawn",
        "shell=true",
        "eval(",
        "exec(",
        "__import__",
        "importlib.",
    )
    assert not any(token in source for token in forbidden)


def test_module_uses_no_network_primitives():
    source = inspect.getsource(registry_module).casefold()
    forbidden = (
        "import socket",
        "socket.",
        "import urllib",
        "urllib.",
        "http.client",
        "httpx",
        "import requests",
        "requests.",
        "import ftplib",
        "import asyncio",
        "asyncio.",
        "ssl.",
        ".connect(",
        "getaddrinfo",
    )
    assert not any(token in source for token in forbidden)


def test_module_imports_are_a_local_only_whitelist():
    tree = ast.parse(inspect.getsource(registry_module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    allowed = {
        "__future__",
        "os",
        "stat",
        "hashlib",
        "hmac",
        "json",
        "dataclasses",
        "datetime",
        "enum",
        "math",
        "pathlib",
        "types",
        "typing",
        "federation",
    }
    assert imported <= allowed


# --- read-only guarantees ---------------------------------------------------


def test_read_only_access_to_missing_store_creates_nothing(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    missing = tmp_path / "missing" / "artifacts.jsonl"
    registry = LocalModelArtifactRegistry(
        missing, integrity_key=KEY, trusted_roots=[root], clock=MutableClock()
    )

    assert registry.list_artifacts() == ()
    assert registry.current("artifact-1") is None
    with pytest.raises(ArtifactNotFoundError):
        registry.resolve("artifact-1")
    with pytest.raises(ArtifactNotFoundError):
        registry.verify("artifact-1")
    assert not missing.parent.exists()


def test_list_and_resolve_return_detached_snapshots(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    register_valid(registry, root)

    first = registry.resolve("artifact-1")
    second = registry.resolve("artifact-1")
    assert first == second
    assert first is not second  # freshly projected each call
    assert isinstance(registry.list_artifacts(), tuple)


def test_registration_accepts_registration_object(tmp_path):
    registry, root, _ = make_registry(tmp_path)
    path, digest = write_file(root, "model.gguf", gguf_bytes(b"x"))
    registration = ArtifactRegistration(
        artifact_id="artifact-1",
        model_id="m",
        path=str(path),
        expected_sha256=digest,
        display_name="Local Llama",
        alias="llama",
    )
    state = registry.register(registration)
    assert state.display_name == "Local Llama"
    assert state.alias == "llama"


# --- documentation boundary -------------------------------------------------


def test_documentation_states_execution_authorization_boundary():
    doc = Path("docs/local-model-artifact-registry-v0.1.md").read_text().casefold()
    assert "does not authorize execution" in doc
    assert "authorization" in doc
    assert "approval" in doc
    assert "worker execution" in doc
    assert "lifecycle governance" in doc
    assert "trusted root" in doc
    assert "non-goals" in doc
