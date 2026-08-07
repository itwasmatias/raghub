"""Windows Display Runtime durable intake store with chain authentication."""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from federation.file_lock import flock, LOCK_EX, LOCK_SH, LOCK_UN, FileLockError
from federation.integrity import (
    authentication_tag,
    authenticates,
    require_integrity_key,
)
from federation.power_action import (
    canonical_json,
    parse_timestamp,
    require_text,
    timestamp,
)
from federation.power_adapter import PowerCorruptionError


_SCHEMA_VERSION = 1
_EVENT_DOMAIN = b"raghub.windows-display-runtime-intake-event.v1"
_GENESIS_TAG = "0" * 64


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PowerCorruptionError("intake event has duplicate JSON keys")
        result[key] = value
    return result


class IntakeStore:
    """Append-only authenticated intake event store with chain authentication."""

    def __init__(self, path, *, runtime_identity, integrity_key, clock):
        self.path = Path(path)
        self.runtime_identity = require_text(runtime_identity, "runtime_identity")
        self._integrity_key = require_integrity_key(integrity_key)
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock

        # Validate existing store if it exists
        if self.path.exists():
            self._validate_store()

    def record_event(
        self,
        *,
        event_type,
        envelope_id,
        intake_sequence,
        authorization_sequence,
        runtime_identity,
        worker_id,
        component_id,
        adapter_id,
        action,
        proposal_id,
        previous_state,
        new_state,
        controller_timestamp,
        expiration,
        result_code,
        envelope_hash=None,
    ):
        """Record a new intake event with authentication chaining."""
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        if runtime_identity != self.runtime_identity:
            raise ValueError("runtime_identity does not match store")

        def mutate(handle, records):
            event = {
                "schema_version": _SCHEMA_VERSION,
                "event_sequence": len(records) + 1,
                "event_type": event_type,
                "envelope_id": envelope_id,
                "intake_sequence": intake_sequence,
                "authorization_sequence": authorization_sequence,
                "runtime_identity": runtime_identity,
                "worker_id": worker_id,
                "component_id": component_id,
                "adapter_id": adapter_id,
                "action": action,
                "proposal_id": proposal_id,
                "previous_state": previous_state,
                "new_state": new_state,
                "controller_timestamp": timestamp(controller_timestamp),
                "expiration": timestamp(expiration) if expiration else None,
                "result_code": result_code,
                "envelope_hash": envelope_hash,
                "predecessor_tag": (
                    _GENESIS_TAG if not records else records[-1]["authentication_tag"]
                ),
                "runtime_timestamp": timestamp(self._clock()),
            }
            event["authentication_tag"] = authentication_tag(
                self._integrity_key,
                _EVENT_DOMAIN,
                canonical_json(event),
            )
            handle.seek(0, 2)  # Seek to end
            handle.write(canonical_json(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
            records.append(event)

        return self._write(mutate)

    def inspect(self):
        """Return all events as a tuple."""
        return tuple(self._read())

    def find_intake_state(self, envelope_id, intake_sequence):
        """Find the latest state for a specific intake envelope.

        Returns:
            dict or None: Latest event for this envelope, or None if not found
        """
        events = self._read()
        matching = [
            e for e in events
            if e.get("envelope_id") == envelope_id
            and e.get("intake_sequence") == intake_sequence
        ]
        return matching[-1] if matching else None

    def reconcile_execution(self, envelope_id, intake_sequence):
        """Atomically reconcile an orphaned execution claim.

        The latest matching state is checked and, only when it is EXECUTING
        a single durable RECONCILIATION_REQUIRED transition is appended under
        the writer lock using only the already-authenticated stored evidence.
        Repeated or concurrent recovery attempts return the existing latest
        event without appending another transition.
        """
        def mutate(handle, records):
            matching = [
                event for event in records
                if event.get("envelope_id") == envelope_id
                and event.get("intake_sequence") == intake_sequence
            ]
            if not matching:
                return None

            current = matching[-1]
            if current.get("new_state") != "executing":
                return current

            stored_hash = next(
                (
                    event.get("envelope_hash")
                    for event in matching
                    if event.get("envelope_hash")
                ),
                None,
            )
            event = {
                "schema_version": _SCHEMA_VERSION,
                "event_sequence": len(records) + 1,
                "event_type": "reconciliation_required",
                "envelope_id": current["envelope_id"],
                "intake_sequence": current["intake_sequence"],
                "authorization_sequence": current["authorization_sequence"],
                "runtime_identity": current["runtime_identity"],
                "worker_id": current["worker_id"],
                "component_id": current["component_id"],
                "adapter_id": current["adapter_id"],
                "action": current["action"],
                "proposal_id": current["proposal_id"],
                "previous_state": "executing",
                "new_state": "reconciliation_required",
                "controller_timestamp": current["controller_timestamp"],
                "expiration": current["expiration"],
                "result_code": "ambiguous_crash_state",
                "envelope_hash": stored_hash,
                "predecessor_tag": (
                    _GENESIS_TAG if not records else records[-1]["authentication_tag"]
                ),
                "runtime_timestamp": timestamp(self._clock()),
            }
            event["authentication_tag"] = authentication_tag(
                self._integrity_key,
                _EVENT_DOMAIN,
                canonical_json(event),
            )
            handle.seek(0, 2)
            handle.write(canonical_json(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
            records.append(event)
            return event

        return self._write(mutate)

    def check_duplicate_or_replay(self, envelope_id, intake_sequence, envelope_hash, controller_authority):
        """Check if an envelope is a duplicate or replay attack.

        Returns:
            tuple: (is_exact_duplicate, conflict_reason)
                is_exact_duplicate: True if exact same envelope seen before
                conflict_reason: None if no conflict, or reason string if rejected
        """
        events = self._read()

        # Build tracking structures
        seen_sequences = set()
        envelope_records = {}  # envelope_id -> (hash, sequence, state)

        for event in events:
            env_id = event.get("envelope_id")
            seq = event.get("intake_sequence")
            state = event.get("new_state")

            # Track sequences (scoped to controller authority for this runtime)
            if event.get("runtime_identity") == self.runtime_identity:
                seen_sequences.add(seq)

            # Track envelope hashes
            if env_id and env_id not in envelope_records:
                # Store first occurrence
                envelope_records[env_id] = {
                    "hash": event.get("envelope_hash"),
                    "sequence": seq,
                    "state": state,
                }

        # Check for exact duplicate
        if envelope_id in envelope_records:
            existing = envelope_records[envelope_id]
            existing_hash = existing.get("hash")

            if existing_hash == envelope_hash:
                # Exact duplicate - idempotent
                return True, None
            else:
                # Same envelope_id but different content - attack
                return False, "envelope_id_reused_with_different_content"

        # Check for sequence replay
        if intake_sequence in seen_sequences:
            return False, "intake_sequence_replayed"

        if seen_sequences and intake_sequence < max(seen_sequences):
            return False, "intake_sequence_out_of_order"

        # No conflict
        return False, None

    def claim_execution(self, envelope_id, intake_sequence, authorization_sequence,
                       runtime_identity, worker_id, component_id, adapter_id, action,
                       proposal_id, controller_timestamp, expiration, envelope_hash):
        """Atomically claim an envelope for execution.

        This must be called while holding the write lock, before invoking
        the adapter. It records the EXECUTING state durably, ensuring that
        only one execution can proceed even if multiple processes attempt
        concurrent intake.

        Returns:
            bool: True if claim succeeded, False if already claimed
        """
        def mutate(handle, records):
            # Check if already claimed or terminal
            for event in records:
                if (event.get("envelope_id") == envelope_id
                    and event.get("intake_sequence") == intake_sequence):
                    state = event.get("new_state")
                    if state == "executing":
                        # Already claimed
                        return False
                    if state in ("succeeded", "failed", "expired", "refused",
                                "dry_run_validated", "reconciliation_required"):
                        # Terminal state - cannot execute
                        return False

            # Claim by recording EXECUTING state
            event = {
                "schema_version": _SCHEMA_VERSION,
                "event_sequence": len(records) + 1,
                "event_type": "executing",
                "envelope_id": envelope_id,
                "intake_sequence": intake_sequence,
                "authorization_sequence": authorization_sequence,
                "runtime_identity": runtime_identity,
                "worker_id": worker_id,
                "component_id": component_id,
                "adapter_id": adapter_id,
                "action": action,
                "proposal_id": proposal_id,
                "previous_state": "execution_authorized",
                "new_state": "executing",
                "controller_timestamp": timestamp(controller_timestamp),
                "expiration": timestamp(expiration) if expiration else None,
                "result_code": None,
                "envelope_hash": envelope_hash,
                "predecessor_tag": (
                    _GENESIS_TAG if not records else records[-1]["authentication_tag"]
                ),
                "runtime_timestamp": timestamp(self._clock()),
            }
            event["authentication_tag"] = authentication_tag(
                self._integrity_key,
                _EVENT_DOMAIN,
                canonical_json(event),
            )
            handle.seek(0, 2)  # Seek to end
            handle.write(canonical_json(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
            records.append(event)
            return True

        return self._write(mutate)

    def _write(self, operation):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            try:
                flock(handle.fileno(), LOCK_EX)
            except FileLockError as exc:
                raise PowerCorruptionError(
                    f"Failed to acquire exclusive lock on intake store: {exc}"
                ) from exc
            try:
                handle.seek(0)
                records = list(self._decode(handle.read()))
                result = operation(handle, records)
                return result
            finally:
                try:
                    flock(handle.fileno(), LOCK_UN)
                except FileLockError:
                    # Unlock failure is not fatal but should be noted
                    pass

    def _read(self):
        if not self.path.exists():
            return ()
        with self.path.open("rb") as handle:
            try:
                flock(handle.fileno(), LOCK_SH)
            except FileLockError as exc:
                raise PowerCorruptionError(
                    f"Failed to acquire shared lock on intake store: {exc}"
                ) from exc
            try:
                return self._decode(handle.read())
            finally:
                try:
                    flock(handle.fileno(), LOCK_UN)
                except FileLockError:
                    # Unlock failure is not fatal but should be noted
                    pass

    def _validate_store(self):
        """Validate the store on initialization."""
        self._decode(self.path.read_bytes())

    def _decode(self, data):
        """Decode and validate JSONL intake event records."""
        if not data:
            return ()

        if not data.endswith(b"\n"):
            raise PowerCorruptionError("intake store has a truncated record")

        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise PowerCorruptionError("intake store is not UTF-8") from exc

        records = []
        predecessor = _GENESIS_TAG

        for expected_sequence, line in enumerate(lines, 1):
            try:
                record = json.loads(line, object_pairs_hook=_no_duplicate_keys)
            except (json.JSONDecodeError, PowerCorruptionError) as exc:
                raise PowerCorruptionError("intake store is malformed") from exc

            if not isinstance(record, dict):
                raise PowerCorruptionError("intake event is not a JSON object")

            # Validate required fields
            if record.get("schema_version") != _SCHEMA_VERSION:
                raise PowerCorruptionError("intake event schema version is invalid")

            if record.get("event_sequence") != expected_sequence:
                raise PowerCorruptionError("intake event sequence is invalid")

            if record.get("runtime_identity") != self.runtime_identity:
                raise PowerCorruptionError("intake event runtime identity mismatch")

            if record.get("predecessor_tag") != predecessor:
                raise PowerCorruptionError(
                    "intake event predecessor authentication is invalid"
                )

            # Verify authentication
            unsigned = dict(record)
            claimed = unsigned.pop("authentication_tag")
            if not authenticates(
                self._integrity_key,
                _EVENT_DOMAIN,
                canonical_json(unsigned),
                claimed,
            ):
                raise PowerCorruptionError(
                    "intake event authentication tag is invalid"
                )

            # Validate timestamp
            try:
                parse_timestamp(
                    record["controller_timestamp"],
                    "controller_timestamp",
                )
                parse_timestamp(
                    record["runtime_timestamp"],
                    "runtime_timestamp",
                )
            except (TypeError, ValueError) as exc:
                raise PowerCorruptionError(
                    "intake event timestamp is invalid"
                ) from exc

            records.append(record)
            predecessor = claimed

        return tuple(records)
