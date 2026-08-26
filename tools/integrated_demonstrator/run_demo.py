"""One-command operational runner for Integrated Demonstrator v0.1.

The runner composes the accepted demonstrator, Mission Control, and durable
stores.  It does not define effect, reconciliation, authority, or mission
truth.  Verification works from copies of completed stores so the preserved
run directory remains read-only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable

from federation.durable_effect_store import DurableEffectStore
from federation.effect_safety import AuthorityDisposition, ReconciliationState
from federation.mission_runtime_store import MissionRuntimeStore
from pavilionos.mission_control import (
    MissionControlView,
    load_mission_control_view,
    render_mission_control,
)
from tools.integrated_demonstrator.governed_ambiguous_dispatch import (
    reconcile_and_verify_governed_dispatch,
)


DEMONSTRATOR_SCHEMA_VERSION = "missionaryx.integrated-demonstrator-manifest.v0.1"
EVIDENCE_SCHEMA_VERSION = "missionaryx.integrated-demonstrator-evidence.v0.1"
CONTROL_DOMAIN = "integrated-demonstrator"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACTS_ROOT = REPOSITORY_ROOT / "artifacts" / "integrated-demonstrator"
MANIFEST_FILENAME = "manifest.json"
EVIDENCE_ARTIFACTS = (
    "effects.sqlite3",
    "evidence-report.json",
    "mission-control.html",
    "mission-control.json",
    "mission.sqlite3",
    "run-summary.txt",
    "service.sqlite3",
)
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 16 * 1024 * 1024


class DemonstratorError(Exception):
    """Base operationalization error."""


class DemonstratorVerificationError(DemonstratorError):
    """A completed run does not satisfy its evidence contract."""


@dataclass(frozen=True, slots=True)
class DemonstratorRun:
    run_id: str
    run_directory: Path
    report: dict[str, Any]
    mission_control_path: Path
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class _ValidatedSources:
    report: dict[str, Any]
    view: MissionControlView
    mission_control_projection: dict[str, Any]


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DemonstratorVerificationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_non_finite(token: str) -> None:
    raise DemonstratorVerificationError(f"non-finite JSON value: {token}")


def _load_json_object(path: Path, *, canonical: bool = False) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DemonstratorVerificationError(f"cannot read {path.name}: {exc}") from exc
    if len(raw) > _MAX_JSON_BYTES:
        raise DemonstratorVerificationError(f"{path.name} exceeds the JSON size limit")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except DemonstratorVerificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemonstratorVerificationError(f"{path.name} is malformed JSON") from exc
    if type(payload) is not dict:
        raise DemonstratorVerificationError(f"{path.name} must contain a JSON object")
    if canonical and raw != _canonical_json_bytes(payload):
        raise DemonstratorVerificationError(f"{path.name} is not canonical JSON")
    return payload


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _validate_run_id(run_id: str) -> str:
    if type(run_id) is not str or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run_id must be 1-128 characters using letters, digits, dot, underscore, or hyphen"
        )
    return run_id


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _repository_head() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DemonstratorError("cannot determine the repository starting commit") from exc
    value = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise DemonstratorError("git returned an invalid repository commit SHA")
    return value


def _regular_artifact_path(run_directory: Path, name: str) -> Path:
    if name not in EVIDENCE_ARTIFACTS:
        raise DemonstratorVerificationError(f"unexpected artifact path: {name!r}")
    path = run_directory / name
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DemonstratorVerificationError(f"artifact is missing: {name}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise DemonstratorVerificationError(f"artifact is not a regular file: {name}")
    return path


def _hash_artifact(path: Path) -> tuple[int, str]:
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        after = path.stat()
    except OSError as exc:
        raise DemonstratorVerificationError(f"cannot hash artifact {path.name}: {exc}") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise DemonstratorVerificationError(f"artifact changed while hashing: {path.name}")
    return before.st_size, digest.hexdigest()


def _read_service_state(database_path: Path) -> dict[str, int]:
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DemonstratorVerificationError("service store cannot be reopened read-only") from exc
    try:
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity != ("ok",):
            raise DemonstratorVerificationError("service store integrity check failed")
        row = connection.execute(
            """
            SELECT active_version, deployment_attempt_count, successful_transition_count
            FROM service_state WHERE singleton = 1
            """
        ).fetchone()
    except sqlite3.Error as exc:
        raise DemonstratorVerificationError("service store is missing valid durable state") from exc
    finally:
        connection.close()
    if row is None or any(type(value) is not int for value in row):
        raise DemonstratorVerificationError("service store state is invalid")
    return {
        "active_version": row[0],
        "deployment_attempt_count": row[1],
        "successful_transition_count": row[2],
    }


def _checkpoint_sqlite_stores(run_directory: Path) -> None:
    """Fold committed WAL pages into each evidence database before hashing."""
    for name in ("effects.sqlite3", "mission.sqlite3", "service.sqlite3"):
        path = run_directory / name
        try:
            connection = sqlite3.connect(path, timeout=5.0)
            try:
                result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise DemonstratorError(f"could not finalize durable store {name}") from exc
        if result != (0, 0, 0):
            raise DemonstratorError(
                f"durable store {name} has uncheckpointed WAL state: {result!r}"
            )


def _assert_no_sqlite_sidecars(run_directory: Path) -> None:
    for database_name in ("effects.sqlite3", "mission.sqlite3", "service.sqlite3"):
        for suffix in ("-wal", "-shm"):
            if (run_directory / f"{database_name}{suffix}").exists():
                raise DemonstratorVerificationError(
                    f"unmanifested SQLite sidecar is present: {database_name}{suffix}"
                )


def _require_report_value(report: dict[str, Any], name: str, expected: object) -> None:
    if report.get(name) != expected:
        raise DemonstratorVerificationError(
            f"evidence report {name} is inconsistent: expected {expected!r}"
        )


def _validate_sources(run_directory: Path) -> _ValidatedSources:
    report = _load_json_object(run_directory / "evidence-report.json", canonical=True)
    _require_report_value(report, "evidence_schema_version", EVIDENCE_SCHEMA_VERSION)
    _require_report_value(report, "control_domain", CONTROL_DOMAIN)
    mission_id = report.get("mission_id")
    if type(mission_id) is not str or not mission_id:
        raise DemonstratorVerificationError("evidence report mission_id is invalid")

    service_state = _read_service_state(run_directory / "service.sqlite3")
    expected_service_state = {
        "active_version": 2,
        "deployment_attempt_count": 1,
        "successful_transition_count": 1,
    }
    if service_state != expected_service_state:
        raise DemonstratorVerificationError(
            f"durable service counters are inconsistent: {service_state!r}"
        )
    for name, expected in expected_service_state.items():
        report_name = "service_active_version" if name == "active_version" else name
        _require_report_value(report, report_name, expected)

    duplicate_count = max(
        0,
        service_state["deployment_attempt_count"]
        - service_state["successful_transition_count"],
    )
    _require_report_value(report, "duplicate_deployment_count", duplicate_count)
    _require_report_value(report, "injected_failure_count", 1)
    _require_report_value(report, "effect_history", ["indeterminate", "something_landed"])
    _require_report_value(report, "final_effect_posture", "something_landed")
    _require_report_value(report, "reconciliation_result", "something_landed")
    _require_report_value(report, "reconciliation_state", "resolved")
    _require_report_value(report, "authority_disposition", "consumed")
    _require_report_value(report, "independent_verification_result", "v2_active")
    _require_report_value(report, "final_mission_state", "completed")

    effect_store: DurableEffectStore | None = None
    try:
        effect_store = DurableEffectStore(run_directory / "effects.sqlite3")
        claim = effect_store.get_gateway_claim(report.get("gateway_claim_id"), CONTROL_DOMAIN)
        obligation = effect_store.get_obligation(
            report.get("reconciliation_obligation_id"), CONTROL_DOMAIN
        )
        reservation = effect_store.get_reservation(
            report.get("authority_reservation_id"), CONTROL_DOMAIN
        )
        intent = effect_store.get_intent(report.get("effect_intent_id"), CONTROL_DOMAIN)
        dispatch = effect_store.get_dispatch(report.get("effect_dispatch_id"), CONTROL_DOMAIN)
        if claim is None or claim["state"] != "reconciled":
            raise DemonstratorVerificationError("gateway claim is not durably reconciled")
        if claim["receipt_recorded_at"] is not None:
            raise DemonstratorVerificationError("ambiguous dispatch unexpectedly has a receipt")
        if claim["handoff_started_at"] is None:
            raise DemonstratorVerificationError("provider handoff evidence is missing")
        if obligation is None or obligation.state is not ReconciliationState.RESOLVED:
            raise DemonstratorVerificationError("reconciliation obligation is not resolved")
        if reservation is None or reservation.disposition is not AuthorityDisposition.CONSUMED:
            raise DemonstratorVerificationError("authority was not consumed by reconciliation")
        if intent is None or dispatch is None:
            raise DemonstratorVerificationError("intent or dispatch evidence is missing")
        if (
            claim["effect_intent_id"] != intent.effect_intent_id
            or claim["effect_dispatch_id"] != dispatch.dispatch_id
            or obligation.effect_intent_id != intent.effect_intent_id
            or obligation.dispatch_id != dispatch.dispatch_id
            or reservation.effect_intent_id != intent.effect_intent_id
        ):
            raise DemonstratorVerificationError("effect evidence bindings are inconsistent")
        history = list(obligation.probe_history)
        if len(history) != 2:
            raise DemonstratorVerificationError("reconciliation evidence history count is invalid")
        if history[0].get("kind") != "reconciliation" or history[0].get("result") != "something_landed":
            raise DemonstratorVerificationError("landed reconciliation evidence is missing")
        verification = history[1]
        if (
            verification.get("kind") != "independent_verification"
            or verification.get("action") != "GET /state"
            or verification.get("read_only") is not True
            or verification.get("result") != "v2_active"
            or verification.get("observation") != service_state
        ):
            raise DemonstratorVerificationError("independent verification evidence is invalid")
        _require_report_value(report, "persisted_evidence_history_count", len(history))
    except DemonstratorVerificationError:
        raise
    except Exception as exc:
        raise DemonstratorVerificationError(f"effect store validation failed: {exc}") from exc
    finally:
        if effect_store is not None:
            effect_store.close()

    try:
        mission_store = MissionRuntimeStore(run_directory / "mission.sqlite3")
        _, lifecycle, _, _ = mission_store.get_mission(CONTROL_DOMAIN, mission_id)
        references = mission_store.list_effect_references(CONTROL_DOMAIN, mission_id)
        checkpoints = mission_store.list_checkpoints(CONTROL_DOMAIN, mission_id)
    except Exception as exc:
        raise DemonstratorVerificationError(f"mission store validation failed: {exc}") from exc
    if lifecycle.value != "completed":
        raise DemonstratorVerificationError("mission is not durably completed")
    if len(references) != 1:
        raise DemonstratorVerificationError("mission must contain exactly one effect reference")
    reference = references[0]
    if (
        reference.effect_intent_id != report.get("effect_intent_id")
        or reference.effect_dispatch_id != report.get("effect_dispatch_id")
        or reference.gateway_claim_id != report.get("gateway_claim_id")
    ):
        raise DemonstratorVerificationError("mission effect reference is inconsistent")
    required_checkpoints = {
        "ambiguous_dispatch_established",
        "retry_blocked",
        "reconciliation_started",
        "external_state_observed",
        "ambiguity_resolved",
        "independent_verification",
    }
    checkpoint_reasons = {checkpoint.reason for checkpoint in checkpoints}
    missing_checkpoints = sorted(required_checkpoints - checkpoint_reasons)
    if missing_checkpoints:
        raise DemonstratorVerificationError(
            "prior INDETERMINATE history is incomplete: " + ", ".join(missing_checkpoints)
        )
    # One authorized handoff and one external attempt establish zero unauthorized
    # operations without relying on the report's asserted counter.
    authorized_handoffs = 1
    unauthorized_count = max(
        0, service_state["deployment_attempt_count"] - authorized_handoffs
    )
    _require_report_value(report, "unauthorized_operation_count", unauthorized_count)

    view = load_mission_control_view(
        run_directory / "mission.sqlite3",
        run_directory / "effects.sqlite3",
        run_directory / "evidence-report.json",
        CONTROL_DOMAIN,
        mission_id,
    )
    if not view.ready:
        raise DemonstratorVerificationError(
            f"Mission Control projection is not ready: {view.source_error or 'unknown source error'}"
        )
    projection = view.to_dict()
    if not any(
        item.get("reason") == "retry_blocked" for item in projection.get("timeline", [])
    ):
        raise DemonstratorVerificationError("Mission Control omitted blind-retry blocking")
    return _ValidatedSources(report=report, view=view, mission_control_projection=projection)


def _summary_text(
    *, run_id: str, run_directory: Path, report: dict[str, Any], passed: bool
) -> str:
    if not passed:
        return (
            "MISSIONARYX INTEGRATED DEMONSTRATOR — FAIL\n\n"
            f"Run ID:               {run_id}\n"
            "The run stopped before all acceptance invariants passed.\n"
            "Partial durable evidence was preserved when available.\n"
        )
    return (
        "MISSIONARYX INTEGRATED DEMONSTRATOR — PASS\n\n"
        f"Run ID:               {run_id}\n"
        "Mission:              COMPLETED\n"
        "Service:              v2\n"
        f"Deployment attempts:  {report['deployment_attempt_count']}\n"
        f"Successful changes:   {report['successful_transition_count']}\n"
        f"Duplicates:           {report['duplicate_deployment_count']}\n"
        f"Unauthorized ops:     {report['unauthorized_operation_count']}\n"
        f"Injected failures:    {report['injected_failure_count']}\n"
        "Effect history:       INDETERMINATE → SOMETHING_LANDED\n"
        "Verification:         PASS\n"
        f"Evidence:             {run_directory / 'evidence-report.json'}\n"
        f"Mission Control:      {run_directory / 'mission-control.html'}\n"
        f"Manifest:             {run_directory / MANIFEST_FILENAME}\n"
    )


def _build_manifest(
    run_directory: Path,
    *,
    run_id: str,
    report: dict[str, Any],
) -> dict[str, Any]:
    artifacts = []
    for name in EVIDENCE_ARTIFACTS:
        path = _regular_artifact_path(run_directory, name)
        size_bytes, sha256 = _hash_artifact(path)
        artifacts.append({"path": name, "size_bytes": size_bytes, "sha256": sha256})
    return {
        "schema_version": DEMONSTRATOR_SCHEMA_VERSION,
        "digest_algorithm": "sha256",
        "hash_scope": "all listed artifacts; manifest.json excluded",
        "run_id": run_id,
        "starting_repository_commit_sha": report["starting_repository_commit_sha"],
        "mission_id": report["mission_id"],
        "control_domain": report["control_domain"],
        "artifacts": artifacts,
        "final_mission_state": report["final_mission_state"],
        "final_effect_posture": report["final_effect_posture"],
        "deployment_attempt_count": report["deployment_attempt_count"],
        "successful_transition_count": report["successful_transition_count"],
        "duplicate_deployment_count": report["duplicate_deployment_count"],
        "unauthorized_operation_count": report["unauthorized_operation_count"],
        "injected_failure_count": report["injected_failure_count"],
        "verification_result": "pass",
    }


def _validate_manifest(run_directory: Path) -> dict[str, Any]:
    manifest = _load_json_object(run_directory / MANIFEST_FILENAME, canonical=True)
    required_values = {
        "schema_version": DEMONSTRATOR_SCHEMA_VERSION,
        "digest_algorithm": "sha256",
        "hash_scope": "all listed artifacts; manifest.json excluded",
        "control_domain": CONTROL_DOMAIN,
        "final_mission_state": "completed",
        "final_effect_posture": "something_landed",
        "deployment_attempt_count": 1,
        "successful_transition_count": 1,
        "duplicate_deployment_count": 0,
        "unauthorized_operation_count": 0,
        "injected_failure_count": 1,
        "verification_result": "pass",
    }
    for name, expected in required_values.items():
        if manifest.get(name) != expected:
            raise DemonstratorVerificationError(
                f"manifest {name} is inconsistent: expected {expected!r}"
            )
    run_id = manifest.get("run_id")
    try:
        _validate_run_id(run_id)
    except ValueError as exc:
        raise DemonstratorVerificationError("manifest run_id is invalid") from exc
    if run_directory.name != run_id:
        raise DemonstratorVerificationError("manifest run_id does not match run directory")
    for name in ("starting_repository_commit_sha", "mission_id"):
        if type(manifest.get(name)) is not str or not manifest[name]:
            raise DemonstratorVerificationError(f"manifest {name} is invalid")

    artifact_rows = manifest.get("artifacts")
    if type(artifact_rows) is not list:
        raise DemonstratorVerificationError("manifest artifacts must be a list")
    if len(artifact_rows) != len(EVIDENCE_ARTIFACTS):
        raise DemonstratorVerificationError("manifest artifact set is incomplete")
    seen: set[str] = set()
    for row in artifact_rows:
        if type(row) is not dict or set(row) != {"path", "size_bytes", "sha256"}:
            raise DemonstratorVerificationError("manifest artifact record is invalid")
        name = row.get("path")
        if type(name) is not str or name in seen:
            raise DemonstratorVerificationError("manifest artifact path is invalid or duplicated")
        seen.add(name)
        path = _regular_artifact_path(run_directory, name)
        observed_size, observed_digest = _hash_artifact(path)
        if type(row.get("size_bytes")) is not int or row["size_bytes"] < 0:
            raise DemonstratorVerificationError(f"manifest size is invalid for {name}")
        if type(row.get("sha256")) is not str or not _SHA256_RE.fullmatch(row["sha256"]):
            raise DemonstratorVerificationError(f"manifest digest is invalid for {name}")
        if observed_size != row["size_bytes"] or observed_digest != row["sha256"]:
            raise DemonstratorVerificationError(f"artifact digest mismatch: {name}")
    if seen != set(EVIDENCE_ARTIFACTS):
        raise DemonstratorVerificationError("manifest artifact set is inconsistent")
    return manifest


def _copy_evidence_stores(source: Path, destination: Path) -> None:
    for name in ("effects.sqlite3", "mission.sqlite3", "service.sqlite3"):
        shutil.copyfile(_regular_artifact_path(source, name), destination / name)
    for name in ("evidence-report.json",):
        shutil.copyfile(_regular_artifact_path(source, name), destination / name)


def verify_run(run_directory: str | Path) -> DemonstratorRun:
    """Read-only evidence verification for one completed run.

    Original artifacts are hash-checked twice.  Canonical stores are reopened
    only from temporary copies, preventing journal/schema activity in the
    preserved evidence directory.
    """
    requested = Path(run_directory)
    try:
        if requested.is_symlink() or not requested.is_dir():
            raise DemonstratorVerificationError("run directory must be a real directory")
        root = requested.resolve(strict=True)
    except OSError as exc:
        raise DemonstratorVerificationError("run directory is unavailable") from exc
    _assert_no_sqlite_sidecars(root)
    first_manifest = _validate_manifest(root)
    with tempfile.TemporaryDirectory(prefix="missionaryx-demo-verify-") as temporary:
        copy_root = Path(temporary)
        _copy_evidence_stores(root, copy_root)
        validated = _validate_sources(copy_root)
        expected_projection = _load_json_object(
            root / "mission-control.json", canonical=True
        )
        if validated.mission_control_projection != expected_projection:
            raise DemonstratorVerificationError(
                "reconstructed Mission Control projection does not match its artifact"
            )
        expected_html = (root / "mission-control.html").read_text(encoding="utf-8")
        if render_mission_control(validated.view) != expected_html:
            raise DemonstratorVerificationError(
                "reconstructed Mission Control HTML does not match its artifact"
            )
    report = validated.report
    cross_checks = {
        "starting_repository_commit_sha": report.get("starting_repository_commit_sha"),
        "mission_id": report.get("mission_id"),
        "control_domain": report.get("control_domain"),
        "final_mission_state": report.get("final_mission_state"),
        "final_effect_posture": report.get("final_effect_posture"),
        "deployment_attempt_count": report.get("deployment_attempt_count"),
        "successful_transition_count": report.get("successful_transition_count"),
        "duplicate_deployment_count": report.get("duplicate_deployment_count"),
        "unauthorized_operation_count": report.get("unauthorized_operation_count"),
        "injected_failure_count": report.get("injected_failure_count"),
    }
    for name, expected in cross_checks.items():
        if first_manifest.get(name) != expected:
            raise DemonstratorVerificationError(
                f"manifest and evidence report disagree on {name}"
            )
    second_manifest = _validate_manifest(root)
    if second_manifest != first_manifest:
        raise DemonstratorVerificationError("manifest changed during verification")
    return DemonstratorRun(
        run_id=first_manifest["run_id"],
        run_directory=root,
        report=report,
        mission_control_path=root / "mission-control.html",
        manifest_path=root / MANIFEST_FILENAME,
    )


def run_demo(
    *,
    artifacts_root: str | Path = DEFAULT_ARTIFACTS_ROOT,
    run_id: str | None = None,
    starting_repository_sha: str | None = None,
) -> DemonstratorRun:
    """Execute, package, and self-verify one isolated demonstrator run."""
    selected_run_id = _validate_run_id(run_id or _new_run_id())
    root = Path(artifacts_root).resolve(strict=False)
    run_directory = root / selected_run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    starting_sha = starting_repository_sha or _repository_head()
    if type(starting_sha) is not str or not starting_sha:
        raise ValueError("starting_repository_sha must be a non-empty string")
    try:
        reconcile_and_verify_governed_dispatch(
            run_directory,
            starting_repository_sha=starting_sha,
        )
        validated = _validate_sources(run_directory)
        projection_path = run_directory / "mission-control.json"
        _atomic_write(
            projection_path,
            _canonical_json_bytes(validated.mission_control_projection),
        )
        mission_control_path = run_directory / "mission-control.html"
        _atomic_write(
            mission_control_path,
            render_mission_control(validated.view).encode("utf-8"),
        )
        summary_path = run_directory / "run-summary.txt"
        _atomic_write(
            summary_path,
            _summary_text(
                run_id=selected_run_id,
                run_directory=run_directory,
                report=validated.report,
                passed=True,
            ).encode("utf-8"),
        )
        _checkpoint_sqlite_stores(run_directory)
        _assert_no_sqlite_sidecars(run_directory)
        manifest = _build_manifest(
            run_directory,
            run_id=selected_run_id,
            report=validated.report,
        )
        _atomic_write(
            run_directory / MANIFEST_FILENAME,
            _canonical_json_bytes(manifest),
        )
        return verify_run(run_directory)
    except BaseException as exc:
        try:
            _atomic_write(
                run_directory / "run-summary.txt",
                _summary_text(
                    run_id=selected_run_id,
                    run_directory=run_directory,
                    report={},
                    passed=False,
                ).encode("utf-8"),
            )
        except Exception as summary_exc:
            raise DemonstratorError(
                "demonstrator failed and failure evidence could not be persisted"
            ) from summary_exc
        raise exc


def _print_result(result: DemonstratorRun, *, verified_only: bool = False) -> None:
    if verified_only:
        print("MISSIONARYX INTEGRATED DEMONSTRATOR — VERIFIED")
        print()
    else:
        print("MISSIONARYX INTEGRATED DEMONSTRATOR — PASS")
        print()
    report = result.report
    print("Mission:              COMPLETED")
    print("Service:              v2")
    print(f"Deployment attempts:  {report['deployment_attempt_count']}")
    print(f"Successful changes:   {report['successful_transition_count']}")
    print(f"Duplicates:           {report['duplicate_deployment_count']}")
    print(f"Unauthorized ops:     {report['unauthorized_operation_count']}")
    print(f"Injected failures:    {report['injected_failure_count']}")
    print("Effect history:       INDETERMINATE → SOMETHING_LANDED")
    print("Verification:         PASS")
    print(f"Evidence:             {result.run_directory / 'evidence-report.json'}")
    print(f"Mission Control:      {result.mission_control_path}")
    print(f"Manifest:             {result.manifest_path}")


def _run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.integrated_demonstrator.run_demo",
        description="Run the isolated MissionaryX Integrated Demonstrator v0.1.",
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=DEFAULT_ARTIFACTS_ROOT,
        help="parent directory for isolated run bundles",
    )
    parser.add_argument(
        "--run-id",
        help="optional safe run ID; fails if that run directory already exists",
    )
    return parser


def _verify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.integrated_demonstrator.run_demo verify",
        description="Verify a completed run without mutating its evidence.",
    )
    parser.add_argument("run_directory", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "verify":
            args = _verify_parser().parse_args(arguments[1:])
            result = verify_run(args.run_directory)
            _print_result(result, verified_only=True)
        else:
            args = _run_parser().parse_args(arguments)
            result = run_demo(
                artifacts_root=args.artifacts_root,
                run_id=args.run_id,
            )
            _print_result(result)
        return 0
    except (DemonstratorError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"MISSIONARYX INTEGRATED DEMONSTRATOR — FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DemonstratorError",
    "DemonstratorRun",
    "DemonstratorVerificationError",
    "main",
    "run_demo",
    "verify_run",
]
