"""Evidence spine normalization and fail-closed regression tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from federation import (
    AuthorizationLevel,
    WorkerExecutionAttempt,
    WorkerExecutionRequest,
    WorkerExecutionResultEnvelope,
    WorkerExecutionStatus,
)
from research_mission import (
    EvidenceCorrelationKey,
    EvidenceRecord,
    EvidenceReference,
    EvidenceSpine,
    EvidenceSpineConflictError,
    EvidenceSpineCorruptionError,
    MissionCheckpoint,
    MissionRecoveryEvidence,
    RecoveryOutcome,
    ResumeClassification,
    ResearchEvidence,
    TaskCheckpoint,
    TaskRecoveryEvidence,
    approval_records,
    checkpoint_record,
    event_records,
    recovery_record,
    research_evidence_record,
    worker_attempt_record,
)
from tools.ai_controller.mission.events import EventSnapshot
from tools.ai_controller.mission.models import MissionEvent
from tools.ai_controller.operations_api.approvals import ApprovalSnapshot


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
MISSION_ID = "mission-1"
TASK_ID = "task-1"
DOMAIN_ID = "control-domain-1"
SOURCE_REVISIONS = {
    "approval_log": "approval-revision-1",
    "mission_checkpoint": "checkpoint-revision-1",
    "mission_event": "event-revision-1",
    "mission_recovery": "recovery-revision-1",
    "research_evidence": "research-revision-1",
    "worker_execution": (NOW + timedelta(minutes=6)).isoformat(),
}


def _source_revisions_for(spine: EvidenceSpine, source: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            revision
            for recorded_source, revision in spine.source_revisions
            if recorded_source == source
        )
    )


def make_checkpoint() -> MissionCheckpoint:
    task = TaskCheckpoint(
        task_id=TASK_ID,
        sequence=1,
        classification=ResumeClassification.SAFE_TO_RESUME,
        assignment_id="assignment-1",
        dispatch_offer_id="offer-1",
        execution_attempt_id="attempt-1",
        execution_status=WorkerExecutionStatus.RUNNING,
        worker_node_id="worker-1",
        coordinator_node_id="coordinator-1",
        approval_required=False,
        execution_fingerprint="a" * 64,
        result_reference="result-1",
        evidence_references=("evidence-1",),
    )
    return MissionCheckpoint(
        checkpoint_id="checkpoint-1",
        mission_id=MISSION_ID,
        plan_fingerprint="b" * 64,
        revision=1,
        mission_status="running",
        tasks=(task,),
        created_at=NOW,
        state_fingerprint="c" * 64,
    )


def make_recovery() -> MissionRecoveryEvidence:
    task = TaskRecoveryEvidence(
        task_id=TASK_ID,
        outcomes=(RecoveryOutcome.RESUMED_SAFELY,),
        checkpoint_classification=ResumeClassification.SAFE_TO_RESUME,
        checkpoint_attempt_id="attempt-1",
        checkpoint_worker_node_id="worker-1",
        current_attempt_id="attempt-2",
        current_execution_status=WorkerExecutionStatus.CLAIMED,
        selected_worker_node_id="worker-2",
        assignment_id="assignment-1",
        dispatch_offer_id="offer-1",
        execution_fingerprint="d" * 64,
        result_reference="result-1",
        evidence_references=("evidence-2",),
        policy_reasons=("policy-1",),
        budget_evidence_fingerprint="e" * 64,
    )
    return MissionRecoveryEvidence(
        recovery_id="recovery-1",
        mission_id=MISSION_ID,
        checkpoint_id="checkpoint-1",
        checkpoint_revision=1,
        plan_fingerprint="f" * 64,
        tasks=(task,),
        source_result_task_ids=(TASK_ID,),
        replication_id=None,
        replication_claim_statuses=(("claim-1", "replicated"),),
        replication_evidence_ids=("replication-1",),
        created_at=NOW + timedelta(minutes=1),
    )


def make_research_evidence() -> ResearchEvidence:
    return ResearchEvidence(
        "evidence-1",
        MISSION_ID,
        TASK_ID,
        "observation",
        "source-system",
        "summary text",
        NOW + timedelta(minutes=2),
        reference="reference-1",
        metadata={"nested": {"ok": True, "count": 2}},
    )


def make_event_snapshot() -> EventSnapshot:
    events = (
        MissionEvent(
            "mission_started",
            NOW.isoformat(),
            MISSION_ID,
            metadata={"phase": "start"},
        ),
        MissionEvent(
            "task_running",
            (NOW + timedelta(minutes=3)).isoformat(),
            MISSION_ID,
            task_id=TASK_ID,
            queue_task_id="queue-1",
            metadata={"attempt": 1},
        ),
    )
    return EventSnapshot(events, SOURCE_REVISIONS["mission_event"], b"raw-event-snapshot")


def make_approval_snapshot() -> ApprovalSnapshot:
    proposal = {
        "record_type": "proposal",
        "proposal": {
            "action_id": "action-1",
            "mission_id": MISSION_ID,
            "mission_task_id": TASK_ID,
            "created_at": NOW.isoformat(),
            "proposal_revision": "proposal-revision-1",
        },
    }
    decision = {
        "record_type": "decision",
        "decision": {
            "action_id": "action-1",
            "decision": "approve",
            "created_at": (NOW + timedelta(minutes=4)).isoformat(),
        },
    }
    return ApprovalSnapshot((proposal, decision), SOURCE_REVISIONS["approval_log"])


def make_worker_attempt() -> WorkerExecutionAttempt:
    request = WorkerExecutionRequest(
        execution_attempt_id="execution-1",
        mission_id=MISSION_ID,
        task_id=TASK_ID,
        assignment_id="assignment-1",
        dispatch_offer_id="offer-1",
        worker_node_id="worker-1",
        coordinator_node_id="coordinator-1",
        authorization_level=AuthorizationLevel.INTERNAL,
        approval_required=True,
        expected_result="result-1",
        execution_fingerprint=None,
        created_at=NOW + timedelta(minutes=5),
    )
    return WorkerExecutionAttempt(
        request=request,
        status=WorkerExecutionStatus.SUCCEEDED,
        claimed_at=NOW + timedelta(minutes=5),
        started_at=NOW + timedelta(minutes=5, seconds=30),
        terminal_at=NOW + timedelta(minutes=6),
        result=WorkerExecutionResultEnvelope("done", ("evidence-3",)),
    )


def make_records() -> tuple[EvidenceRecord, ...]:
    approval = approval_records(make_approval_snapshot(), domain_id=DOMAIN_ID)
    event = event_records(make_event_snapshot(), domain_id=DOMAIN_ID)
    return (
        worker_attempt_record(make_worker_attempt(), domain_id=DOMAIN_ID),
        approval[1],
        checkpoint_record(make_checkpoint(), source_revision=SOURCE_REVISIONS["mission_checkpoint"], domain_id=DOMAIN_ID),
        research_evidence_record(
            make_research_evidence(),
            source_revision=SOURCE_REVISIONS["research_evidence"],
            domain_id=DOMAIN_ID,
        ),
        event[0],
        recovery_record(
            make_recovery(),
            source_revision=SOURCE_REVISIONS["mission_recovery"],
            domain_id=DOMAIN_ID,
        ),
        approval[0],
        event[1],
    )


def test_spine_normalizes_and_orders_records_deterministically() -> None:
    records = make_records()
    spine = EvidenceSpine.from_records(records)
    reverse_spine = EvidenceSpine.from_records(tuple(reversed(records)))

    assert spine.records == reverse_spine.records
    assert spine.chain_fingerprint == reverse_spine.chain_fingerprint
    assert tuple(spine) == spine.records
    assert len(spine) == 8
    assert spine.records_for_mission(MISSION_ID) == spine.records
    assert len(spine.records_for_task(MISSION_ID, TASK_ID)) == 5
    assert len(spine.records_for_source("approval_log")) == 2
    assert len(spine.records_for_source("mission_event")) == 2
    assert _source_revisions_for(spine, "mission_checkpoint") == (SOURCE_REVISIONS["mission_checkpoint"],)
    assert _source_revisions_for(spine, "mission_recovery") == (SOURCE_REVISIONS["mission_recovery"],)
    assert _source_revisions_for(spine, "research_evidence") == (SOURCE_REVISIONS["research_evidence"],)
    assert _source_revisions_for(spine, "worker_execution") == (SOURCE_REVISIONS["worker_execution"],)
    assert _source_revisions_for(spine, "mission_event") == tuple(
        sorted(
            record.reference.source_revision
            for record in event_records(make_event_snapshot(), domain_id=DOMAIN_ID)
        )
    )
    assert _source_revisions_for(spine, "approval_log") == tuple(
        sorted(
            record.reference.source_revision
            for record in approval_records(make_approval_snapshot(), domain_id=DOMAIN_ID)
        )
    )

    export = spine.export(generated_at=NOW)
    exported = export.to_dict()
    assert export.summary == "8 records from 6 sources across 1 missions and 1 task scopes"
    assert exported["generated_at"] == NOW.isoformat()
    assert exported["schema_version"] == "raghub.evidence-spine.v0.1"
    assert exported["chain"]["chain_fingerprint"] == spine.chain_fingerprint
    assert exported["records"][0]["record_fingerprint"] == spine.records[0].record_fingerprint
    assert json.loads(json.dumps(exported)) == exported


def test_event_records_ignore_enclosing_snapshot_revision() -> None:
    original_snapshot = make_event_snapshot()
    shifted_snapshot = replace(original_snapshot, revision="event-revision-2")

    original_records = event_records(original_snapshot, domain_id=DOMAIN_ID)
    shifted_records = event_records(shifted_snapshot, domain_id=DOMAIN_ID)

    assert original_records == shifted_records

    spine = EvidenceSpine.from_records((*original_records, *shifted_records))
    assert len(spine) == len(original_records)
    assert len(spine.records_for_source("mission_event")) == len(original_records)


def test_approval_records_ignore_enclosing_snapshot_revision() -> None:
    original_snapshot = make_approval_snapshot()
    shifted_snapshot = replace(original_snapshot, revision="approval-revision-2")

    original_records = approval_records(original_snapshot, domain_id=DOMAIN_ID)
    shifted_records = approval_records(shifted_snapshot, domain_id=DOMAIN_ID)

    assert original_records == shifted_records

    spine = EvidenceSpine.from_records((*original_records, *shifted_records))
    assert len(spine) == len(original_records)
    assert len(spine.records_for_source("approval_log")) == len(original_records)


def test_exact_duplicate_record_is_idempotent() -> None:
    record = research_evidence_record(
        make_research_evidence(),
        source_revision=SOURCE_REVISIONS["research_evidence"],
        domain_id=DOMAIN_ID,
    )
    spine = EvidenceSpine.from_records((record, replace(record)))

    assert len(spine) == 1
    assert spine.records[0] == record


def test_conflicting_same_key_rejects_ambiguous_correlation() -> None:
    key = EvidenceCorrelationKey(
        source="manual",
        record_id="record-1",
        mission_id=MISSION_ID,
        task_id=TASK_ID,
        domain_id=DOMAIN_ID,
    )
    reference = EvidenceReference(
        source_revision="manual-revision-1",
        fingerprint="f" * 64,
        observed_at=NOW,
        summary="manual reference",
        reference="ref-1",
    )
    first = EvidenceRecord(key, reference, payload={"value": 1})
    second = EvidenceRecord(key, reference, payload={"value": 2})

    with pytest.raises(EvidenceSpineConflictError):
        EvidenceSpine.from_records((first, second))


def test_payload_tampering_is_detected() -> None:
    record = research_evidence_record(
        make_research_evidence(),
        source_revision=SOURCE_REVISIONS["research_evidence"],
        domain_id=DOMAIN_ID,
    )
    record.payload["summary"] = "tampered"

    with pytest.raises(EvidenceSpineCorruptionError):
        EvidenceSpine.from_records((record,))


def test_adapter_inputs_fail_closed_for_invalid_context() -> None:
    with pytest.raises(ValueError):
        checkpoint_record(make_checkpoint(), source_revision=" ", domain_id=DOMAIN_ID)

    with pytest.raises(ValueError):
        recovery_record(make_recovery(), source_revision=SOURCE_REVISIONS["mission_recovery"], domain_id=" ")

    with pytest.raises(ValueError):
        research_evidence_record(make_research_evidence(), source_revision=" ", domain_id=DOMAIN_ID)


def test_worker_attempt_history_keeps_distinct_lifecycle_snapshots() -> None:
    completed_attempt = make_worker_attempt()
    claimed_attempt = replace(
        completed_attempt,
        status=WorkerExecutionStatus.CLAIMED,
        started_at=None,
        terminal_at=None,
        result=None,
        failure_reason=None,
    )
    running_attempt = replace(
        completed_attempt,
        status=WorkerExecutionStatus.RUNNING,
        terminal_at=None,
        result=None,
        failure_reason=None,
    )

    records = (
        worker_attempt_record(claimed_attempt, domain_id=DOMAIN_ID),
        worker_attempt_record(running_attempt, domain_id=DOMAIN_ID),
        worker_attempt_record(completed_attempt, domain_id=DOMAIN_ID),
    )

    spine = EvidenceSpine.from_records(records)

    assert len(spine) == 3
    assert len(spine.records_for_source("worker_execution")) == 3
    assert len({record.key.record_id for record in records}) == 3

    duplicate_spine = EvidenceSpine.from_records((records[-1], replace(records[-1])))
    assert len(duplicate_spine) == 1


def test_non_spine_inputs_fail_closed() -> None:
    with pytest.raises(TypeError):
        event_records(object())

    with pytest.raises(TypeError):
        approval_records(object())

    with pytest.raises(TypeError):
        worker_attempt_record(object())
