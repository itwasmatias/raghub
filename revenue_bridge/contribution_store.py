"""Durable append-only contribution storage for MissionaryX Profit Loop v0.1.

The store persists original accounting evidence, then reconstructs a
ContributionLedger by replaying every record through the same domain rules.

Fail-closed invariants:
- malformed, partial, empty, or unknown records refuse replay;
- existing corruption refuses further appends;
- duplicate ledger/payment identities are rejected during replay;
- writes occur under the repository FileLock convention;
- accepted appends are flushed and fsynced before returning.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from revenue_bridge.capital import CapitalSnapshot
from revenue_bridge.contribution import (
    ContributionEntry,
    ContributionKind,
    ContributionLedger,
    PaymentObservation,
)
from tools.ai_controller._locking import FileLock


class ContributionStoreCorruptionError(RuntimeError):
    """Persisted contribution evidence cannot be trusted or replayed."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class DurableContributionLedger:
    """Append-only JSONL persistence facade over ContributionLedger."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load(self) -> ContributionLedger:
        """Replay one stable persisted snapshot under the authoritative lock."""
        with FileLock(self.lock_path):
            return self._load_locked()

    @property
    def entries(self) -> tuple[ContributionEntry, ...]:
        return self.load().entries

    def capital_snapshot(
        self,
        *,
        as_of_date: date | None = None,
    ) -> CapitalSnapshot:
        return self.load().capital_snapshot(as_of_date=as_of_date)

    def record_cost(
        self,
        *,
        entry_id: str,
        opportunity_id: str,
        kind: ContributionKind,
        amount_usd: float,
        evidence_ref: str,
        occurred_at: datetime | None = None,
        source_ref: str | None = None,
    ) -> ContributionEntry:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with FileLock(self.lock_path):
            # Validate and replay ALL existing history before accepting more.
            ledger = self._load_locked()

            entry = ledger.record_cost(
                entry_id=entry_id,
                opportunity_id=opportunity_id,
                kind=kind,
                amount_usd=amount_usd,
                evidence_ref=evidence_ref,
                occurred_at=occurred_at,
                source_ref=source_ref,
            )

            record = {
                "record_type": "cost",
                "entry_id": entry.entry_id,
                "opportunity_id": entry.opportunity_id,
                "kind": entry.kind.value,
                "amount_usd": entry.amount_usd,
                "evidence_ref": entry.evidence_ref,
                "occurred_at": entry.occurred_at.isoformat(),
                "source_ref": entry.source_ref,
                "fingerprint": entry.fingerprint,
            }
            self._append_locked(record)
            return entry

    def record_verified_payment(
        self,
        payment: PaymentObservation,
    ) -> ContributionEntry:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with FileLock(self.lock_path):
            # Replay first so duplicates/corruption fail before writing.
            ledger = self._load_locked()
            entry = ledger.record_verified_payment(payment)

            record = {
                "record_type": "verified_payment",
                "payment": payment.to_dict(),
            }
            self._append_locked(record)
            return entry

    def _load_locked(self) -> ContributionLedger:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raw = b""

        return self._replay(raw)

    def _replay(self, raw: bytes) -> ContributionLedger:
        if raw and not raw.endswith(b"\n"):
            raise ContributionStoreCorruptionError(
                f"contribution ledger has an incomplete final record: {self.path}"
            )

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContributionStoreCorruptionError(
                f"contribution ledger is not valid UTF-8: {self.path}"
            ) from exc

        ledger = ContributionLedger()

        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                raise ContributionStoreCorruptionError(
                    f"contribution ledger has an empty record at line {lineno}: "
                    f"{self.path}"
                )

            try:
                data = json.loads(line, object_pairs_hook=_strict_object)
                if not isinstance(data, dict):
                    raise TypeError("contribution record is not an object")

                record_type = data.get("record_type")

                if record_type == "cost":
                    occurred_at = data["occurred_at"]
                    if not isinstance(occurred_at, str):
                        raise TypeError("occurred_at must be an ISO-8601 string")

                    replayed_entry = ledger.record_cost(
                        entry_id=data["entry_id"],
                        opportunity_id=data["opportunity_id"],
                        kind=ContributionKind(data["kind"]),
                        amount_usd=float(data["amount_usd"]),
                        evidence_ref=data["evidence_ref"],
                        occurred_at=datetime.fromisoformat(
                            occurred_at.replace("Z", "+00:00")
                        ),
                        source_ref=data.get("source_ref"),
                    )

                    if data.get("fingerprint") != replayed_entry.fingerprint:
                        raise ValueError(
                            "persisted cost fingerprint does not match entry"
                        )

                elif record_type == "verified_payment":
                    payment_data = data["payment"]
                    if not isinstance(payment_data, dict):
                        raise TypeError("payment must be an object")

                    payment = PaymentObservation.from_dict(payment_data)

                    persisted_fingerprint = payment_data.get("fingerprint")
                    if persisted_fingerprint != payment.fingerprint:
                        raise ValueError(
                            "persisted payment fingerprint does not match payment"
                        )

                    ledger.record_verified_payment(payment)

                else:
                    raise ValueError(
                        f"unknown contribution record_type: {record_type!r}"
                    )

            except (
                json.JSONDecodeError,
                TypeError,
                KeyError,
                ValueError,
            ) as exc:
                raise ContributionStoreCorruptionError(
                    f"malformed contribution record at line {lineno} "
                    f"in {self.path}: {exc}"
                ) from exc

        return ledger

    def _append_locked(self, record: dict[str, Any]) -> None:
        line = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n"

        with self.path.open("ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
