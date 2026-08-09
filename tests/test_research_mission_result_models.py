"""Contract tests for domain-neutral research task results and evidence."""

from datetime import datetime, timedelta, timezone, tzinfo
from math import inf, nan

import pytest

from research_mission import (
    ChallengerAssessment,
    JudgeDecision,
    JudgeOutcome,
    ResearchEvidence,
    ResearchRole,
    ResearchTaskResult,
    ResearchTaskResultStatus,
)


PRODUCED_AT = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def make_result(**overrides) -> ResearchTaskResult:
    values = {
        "mission_id": "mission-1",
        "task_id": "mission-1-researcher-1",
        "role": ResearchRole.RESEARCHER,
        "status": ResearchTaskResultStatus.COMPLETED,
        "summary": "The supplied measurements support the finding.",
        "produced_at": PRODUCED_AT,
        "metadata": {"sample": {"count": 3, "labels": ["a", "b"]}},
    }
    values.update(overrides)
    return ResearchTaskResult(**values)


def make_evidence(**overrides) -> ResearchEvidence:
    values = {
        "evidence_id": "evidence-1",
        "mission_id": "mission-1",
        "task_id": "mission-1-researcher-1",
        "evidence_type": "measurement",
        "source": "supplied-dataset",
        "reference": "dataset://measurements/row-7",
        "summary": "A supplied measurement relevant to the task finding.",
        "observed_at": PRODUCED_AT - timedelta(hours=1),
        "metadata": {"units": "kWh", "values": [10, 11]},
    }
    values.update(overrides)
    return ResearchEvidence(**values)


def test_result_status_has_only_terminal_result_states():
    assert [status.value for status in ResearchTaskResultStatus] == [
        "completed",
        "failed",
        "blocked",
    ]


def test_challenger_assessment_preserves_structural_challenges_as_tuples():
    assessment = ChallengerAssessment(
        challenged_findings=["finding-1"],
        weaknesses=["The sample is small"],
        contradictions=["A second measurement differs"],
        missing_evidence=["No long-term measurement"],
        alternative_explanations=["Seasonality"],
    )

    assert assessment.challenged_findings == ("finding-1",)
    assert assessment.weaknesses == ("The sample is small",)
    assert assessment.contradictions == ("A second measurement differs",)
    assert assessment.missing_evidence == ("No long-term measurement",)
    assert assessment.alternative_explanations == ("Seasonality",)


def test_challenger_assessment_requires_substantive_structure():
    with pytest.raises(ValueError, match="at least one"):
        ChallengerAssessment()


@pytest.mark.parametrize(
    "value",
    ["one string is not a collection", [""], [object()]],
)
def test_challenger_assessment_rejects_malformed_collections(value):
    with pytest.raises((TypeError, ValueError)):
        ChallengerAssessment(weaknesses=value)


def test_judge_decision_preserves_governed_structural_evaluation():
    decision = JudgeDecision(
        outcome=JudgeOutcome.CONTESTED,
        rationale="The supplied record supports only part of the finding.",
        confidence=0.65,
        accepted_findings=["finding-1"],
        rejected_findings=["finding-2"],
        contested_findings=["finding-3"],
        unresolved_questions=["Does the effect persist?"],
    )

    assert decision.outcome is JudgeOutcome.CONTESTED
    assert decision.confidence == 0.65
    assert decision.accepted_findings == ("finding-1",)
    assert decision.rejected_findings == ("finding-2",)
    assert decision.contested_findings == ("finding-3",)
    assert decision.unresolved_questions == ("Does the effect persist?",)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, nan, inf, -inf, True])
def test_judge_decision_rejects_invalid_confidence(confidence):
    with pytest.raises((TypeError, ValueError), match="confidence"):
        JudgeDecision(
            outcome=JudgeOutcome.INCONCLUSIVE,
            rationale="Evidence is insufficient.",
            confidence=confidence,
        )


def test_judge_decision_requires_exact_outcome_and_non_empty_rationale():
    with pytest.raises(TypeError, match="outcome"):
        JudgeDecision(outcome="accepted", rationale="Reason")
    with pytest.raises(ValueError, match="rationale"):
        JudgeDecision(outcome=JudgeOutcome.ACCEPTED, rationale="  ")


def test_research_task_result_is_normalized_and_deeply_immutable():
    metadata = {"sample": {"count": 3, "labels": ["a", "b"]}}
    result = make_result(metadata=metadata)
    metadata["sample"]["labels"].append("caller mutation")

    assert result.produced_at.tzinfo is timezone.utc
    assert result.metadata["sample"]["labels"] == ("a", "b")
    with pytest.raises(TypeError):
        result.metadata["new"] = "mutation"
    with pytest.raises(TypeError):
        result.metadata["sample"]["count"] = 4


def test_research_task_result_normalizes_aware_timestamps_to_utc():
    local_time = datetime(
        2026,
        8,
        8,
        7,
        0,
        tzinfo=timezone(timedelta(hours=-5)),
    )

    assert make_result(produced_at=local_time).produced_at == PRODUCED_AT


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("mission_id", "", ValueError),
        ("mission_id", object(), TypeError),
        ("task_id", "  ", ValueError),
        ("role", "researcher", TypeError),
        ("status", "completed", TypeError),
        ("summary", "", ValueError),
        ("produced_at", "2026-08-08T12:00:00Z", TypeError),
        ("produced_at", datetime(2026, 8, 8, 12, 0), ValueError),
    ],
)
def test_research_task_result_rejects_invalid_contract_fields(field, value, error):
    with pytest.raises(error, match=field):
        make_result(**{field: value})


def test_research_task_result_rejects_datetime_subclasses():
    class UntrustedDateTime(datetime):
        pass

    malformed = UntrustedDateTime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(TypeError, match="produced_at"):
        make_result(produced_at=malformed)


def test_research_task_result_rejects_stateful_custom_timezone_objects():
    class StatefulTimezone(tzinfo):
        def __init__(self):
            self.calls = 0

        def utcoffset(self, value):
            self.calls += 1
            return timedelta(hours=self.calls)

        def dst(self, value):
            return timedelta(0)

    malformed = datetime(2026, 8, 8, 12, 0, tzinfo=StatefulTimezone())

    with pytest.raises(TypeError, match="produced_at.*timezone"):
        make_result(produced_at=malformed)


@pytest.mark.parametrize(
    "metadata",
    [
        {1: "non-string key"},
        {"value": nan},
        {"value": inf},
        {"value": {"sets are not canonical"}},
        {"value": object()},
    ],
)
def test_result_metadata_rejects_values_that_cannot_preserve_integrity(metadata):
    with pytest.raises((TypeError, ValueError), match="metadata"):
        make_result(metadata=metadata)


def test_result_metadata_rejects_cycles():
    metadata = {}
    metadata["self"] = metadata

    with pytest.raises(ValueError, match="metadata.*cyclic"):
        make_result(metadata=metadata)


def test_result_metadata_rejects_excessive_nesting():
    nested = "leaf"
    for _ in range(70):
        nested = [nested]

    with pytest.raises(ValueError, match="nesting exceeds maximum depth"):
        make_result(metadata={"nested": nested})


def test_completed_challenger_result_requires_challenger_structure():
    with pytest.raises(ValueError, match="challenger_assessment"):
        make_result(
            task_id="mission-1-challenger-1",
            role=ResearchRole.CHALLENGER,
        )


def test_completed_judge_result_requires_judge_decision():
    with pytest.raises(ValueError, match="judge_decision"):
        make_result(
            task_id="mission-1-judge-1",
            role=ResearchRole.JUDGE,
        )


def test_result_rejects_role_payload_confusion():
    challenge = ChallengerAssessment(weaknesses=["weakness"])
    decision = JudgeDecision(
        outcome=JudgeOutcome.ACCEPTED,
        rationale="The result is supported.",
    )

    with pytest.raises(ValueError, match="researcher"):
        make_result(challenger_assessment=challenge)
    with pytest.raises(ValueError, match="challenger"):
        make_result(
            task_id="mission-1-challenger-1",
            role=ResearchRole.CHALLENGER,
            challenger_assessment=challenge,
            judge_decision=decision,
        )
    with pytest.raises(ValueError, match="judge"):
        make_result(
            task_id="mission-1-judge-1",
            role=ResearchRole.JUDGE,
            challenger_assessment=challenge,
            judge_decision=decision,
        )


def test_failed_and_blocked_results_do_not_require_completion_payloads():
    failed = make_result(
        task_id="mission-1-challenger-1",
        role=ResearchRole.CHALLENGER,
        status=ResearchTaskResultStatus.FAILED,
        summary="The task failed before producing an assessment.",
    )
    blocked = make_result(
        task_id="mission-1-judge-1",
        role=ResearchRole.JUDGE,
        status=ResearchTaskResultStatus.BLOCKED,
        summary="The task was blocked before a decision.",
    )

    assert failed.challenger_assessment is None
    assert blocked.judge_decision is None


def test_research_evidence_preserves_provenance_and_immutable_metadata():
    metadata = {"values": [10, 11]}
    evidence = make_evidence(metadata=metadata)
    metadata["values"].append(12)

    assert evidence.mission_id == "mission-1"
    assert evidence.task_id == "mission-1-researcher-1"
    assert evidence.category == "measurement"
    assert evidence.metadata["values"] == (10, 11)
    with pytest.raises(TypeError):
        evidence.metadata["new"] = "mutation"


def test_research_evidence_normalizes_aware_timestamps_to_utc():
    local_time = datetime(
        2026,
        8,
        8,
        6,
        0,
        tzinfo=timezone(timedelta(hours=-5)),
    )

    assert make_evidence(observed_at=local_time).observed_at == (
        PRODUCED_AT - timedelta(hours=1)
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {1: "non-string key"},
        {"value": nan},
        {"value": inf},
        {"value": object()},
    ],
)
def test_evidence_metadata_rejects_values_that_cannot_preserve_integrity(metadata):
    with pytest.raises((TypeError, ValueError), match="metadata"):
        make_evidence(metadata=metadata)


def test_evidence_metadata_rejects_cycles():
    metadata = {}
    metadata["self"] = metadata

    with pytest.raises(ValueError, match="metadata.*cyclic"):
        make_evidence(metadata=metadata)


def test_result_and_evidence_equality_preserve_metadata_value_types():
    assert make_result(metadata={"value": True}) != make_result(
        metadata={"value": 1}
    )
    assert make_evidence(metadata={"value": True}) != make_evidence(
        metadata={"value": 1}
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("evidence_id", "", ValueError),
        ("mission_id", "", ValueError),
        ("task_id", object(), TypeError),
        ("evidence_type", "  ", ValueError),
        ("source", "", ValueError),
        ("summary", "", ValueError),
        ("reference", "", ValueError),
        ("reference", object(), TypeError),
        ("observed_at", object(), TypeError),
        ("observed_at", datetime(2026, 8, 8, 12, 0), ValueError),
    ],
)
def test_research_evidence_rejects_invalid_contract_fields(field, value, error):
    with pytest.raises(error, match=field):
        make_evidence(**{field: value})
