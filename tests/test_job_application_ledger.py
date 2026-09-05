"""Tests for DurableApplicationLedger — state machine, durability, corruption detection."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_application.ledger import (
    ApplicationLedgerCorruptionError,
    ApplicationRecord,
    ApplicationState,
    ApplicationStateError,
    DurableApplicationLedger,
)


def _make_record(aid: str = "app_001", job_id: str = "job_001") -> ApplicationRecord:
    now = datetime.now(timezone.utc)
    return ApplicationRecord(
        application_id=aid,
        job_id=job_id,
        company="Acme",
        title="Dev",
        job_url=None,
        application_url=None,
        posting_hash="abc123",
        fit_score=70,
        fit_rationale="test rationale",
        state=ApplicationState.DISCOVERED,
        candidate_profile_hash="prof_hash",
        created_at=now,
        updated_at=now,
    )


class TestApplicationStateMachine:
    def test_valid_initial_transition(self, tmp_path: Path):
        ledger = DurableApplicationLedger(tmp_path / "apps.jsonl")
        record = _make_record()
        ledger.create(record)
        updated = ledger.transition(record.application_id, ApplicationState.QUALIFIED)
        assert updated.state == ApplicationState.QUALIFIED

    def test_invalid_transition_raises(self, tmp_path: Path):
        ledger = DurableApplicationLedger(tmp_path / "apps.jsonl")
        record = _make_record()
        ledger.create(record)
        with pytest.raises(ApplicationStateError):
            ledger.transition(record.application_id, ApplicationState.SUBMITTING)

    def test_transition_to_terminal_states(self, tmp_path: Path):
        ledger = DurableApplicationLedger(tmp_path / "apps.jsonl")
        record = _make_record()
        ledger.create(record)
        ledger.transition(record.application_id, ApplicationState.SKIPPED)
        # Cannot transition from terminal state
        with pytest.raises(ApplicationStateError):
            ledger.transition(record.application_id, ApplicationState.QUALIFIED)

    def test_submission_path_allowed(self, tmp_path: Path):
        ledger = DurableApplicationLedger(tmp_path / "apps.jsonl")
        record = _make_record()
        ledger.create(record)
        ledger.transition(record.application_id, ApplicationState.QUALIFIED)
        ledger.transition(record.application_id, ApplicationState.PACKET_DRAFTED)
        ledger.transition(record.application_id, ApplicationState.READY_FOR_APPLICATION)
        ledger.transition(record.application_id, ApplicationState.BEGIN_APPROVED)
        ledger.transition(record.application_id, ApplicationState.FORM_IN_PROGRESS)
        ledger.transition(record.application_id, ApplicationState.READY_TO_SUBMIT)
        ledger.transition(record.application_id, ApplicationState.SUBMISSION_APPROVED)
        ledger.transition(record.application_id, ApplicationState.SUBMITTING)
        ledger.transition(record.application_id, ApplicationState.SUBMITTED_CONFIRMED)
        r = ledger.get(record.application_id)
        assert r.state == ApplicationState.SUBMITTED_CONFIRMED

    def test_indeterminate_path(self, tmp_path: Path):
        ledger = DurableApplicationLedger(tmp_path / "apps.jsonl")
        record = _make_record()
        ledger.create(record)
        for state in [
            ApplicationState.QUALIFIED,
            ApplicationState.PACKET_DRAFTED,
            ApplicationState.READY_FOR_APPLICATION,
            ApplicationState.BEGIN_APPROVED,
            ApplicationState.FORM_IN_PROGRESS,
            ApplicationState.READY_TO_SUBMIT,
            ApplicationState.SUBMISSION_APPROVED,
            ApplicationState.SUBMITTING,
        ]:
            ledger.transition(record.application_id, state)

        ledger.transition(record.application_id, ApplicationState.SUBMISSION_INDETERMINATE)
        r = ledger.get(record.application_id)
        assert r.state == ApplicationState.SUBMISSION_INDETERMINATE


class TestLedgerDurability:
    def test_survives_restart(self, tmp_path: Path):
        path = tmp_path / "apps.jsonl"
        record = _make_record()

        ledger1 = DurableApplicationLedger(path)
        ledger1.create(record)
        ledger1.transition(record.application_id, ApplicationState.QUALIFIED)

        ledger2 = DurableApplicationLedger(path)
        r = ledger2.get(record.application_id)
        assert r is not None
        assert r.state == ApplicationState.QUALIFIED

    def test_in_progress_state_survives_restart(self, tmp_path: Path):
        path = tmp_path / "apps.jsonl"
        record = _make_record()

        ledger1 = DurableApplicationLedger(path)
        ledger1.create(record)
        for state in [
            ApplicationState.QUALIFIED,
            ApplicationState.PACKET_DRAFTED,
            ApplicationState.READY_FOR_APPLICATION,
            ApplicationState.BEGIN_APPROVED,
        ]:
            ledger1.transition(record.application_id, state)

        ledger2 = DurableApplicationLedger(path)
        r = ledger2.get(record.application_id)
        assert r.state == ApplicationState.BEGIN_APPROVED

    def test_update_fields_survives_restart(self, tmp_path: Path):
        path = tmp_path / "apps.jsonl"
        record = _make_record()

        ledger1 = DurableApplicationLedger(path)
        ledger1.create(record)
        ledger1.update_fields(record.application_id, packet_hash="pkt_xyz")

        ledger2 = DurableApplicationLedger(path)
        r = ledger2.get(record.application_id)
        assert r.packet_hash == "pkt_xyz"

    def test_duplicate_application_id_rejected(self, tmp_path: Path):
        ledger = DurableApplicationLedger(tmp_path / "apps.jsonl")
        record = _make_record()
        ledger.create(record)
        with pytest.raises(ValueError, match="already exists"):
            ledger.create(record)

    def test_empty_ledger_returns_empty_list(self, tmp_path: Path):
        ledger = DurableApplicationLedger(tmp_path / "apps.jsonl")
        assert ledger.load_all() == []


class TestLedgerCorruption:
    def test_missing_trailing_newline_detected(self, tmp_path: Path):
        path = tmp_path / "apps.jsonl"
        path.write_bytes(b'{"record_type":"create"}')
        ledger = DurableApplicationLedger(path)
        with pytest.raises(ApplicationLedgerCorruptionError):
            ledger.load_all()

    def test_malformed_json_detected(self, tmp_path: Path):
        path = tmp_path / "apps.jsonl"
        path.write_bytes(b'not json\n')
        ledger = DurableApplicationLedger(path)
        with pytest.raises(ApplicationLedgerCorruptionError):
            ledger.load_all()

    def test_invalid_transition_in_replay_detected(self, tmp_path: Path):
        path = tmp_path / "apps.jsonl"
        import json
        now = datetime.now(timezone.utc).isoformat()
        record = _make_record()
        line1 = json.dumps({"record_type": "create", "data": record.to_dict()}, sort_keys=True) + "\n"
        line2 = json.dumps({
            "record_type": "transition",
            "application_id": record.application_id,
            "patch": {"state": "submitting", "updated_at": now},
        }, sort_keys=True) + "\n"
        path.write_bytes((line1 + line2).encode("utf-8"))
        ledger = DurableApplicationLedger(path)
        with pytest.raises(ApplicationLedgerCorruptionError):
            ledger.load_all()
