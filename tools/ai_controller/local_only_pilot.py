"""Deterministic contracts for the small governed local-only capability pilot."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tools.ai_controller.local_capability_baseline import LocalityType
from tools.ai_controller.local_model_runtime import (
    LocalInferenceRequest, LocalInferenceResult, LocalInferenceStatus,
    LocalModelDescriptor,
)
from tools.ai_controller.local_equivalence_benchmark import (
    BenchmarkSuite, BenchmarkTaskSpec, CorrectnessType, TaskDifficulty,
)
from tools.ai_controller.local_equivalence_experiments import (
    ExperimentArm, ExperimentArmType, ExperimentDefinition, ExperimentTask,
)


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class PilotContractError(ValueError):
    pass


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PilotContractError(f"{name} must be a canonical identifier")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PilotContractError(f"{name} must be canonical non-empty text")
    return value


def _canonical(value: Any) -> bytes:
    def public(item):
        if hasattr(item, "to_dict"):
            return public(item.to_dict())
        if isinstance(item, dict):
            return {key: public(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [public(child) for child in item]
        if isinstance(item, StrEnum):
            return item.value
        return item
    try:
        return json.dumps(public(value), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise PilotContractError("pilot evidence must be finite JSON data") from exc


def _fp(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class PilotTaskFamily(StrEnum):
    EXACT_INSTRUCTION = "exact_instruction"
    COMMON_KNOWLEDGE = "common_knowledge"
    SUMMARIZATION = "summarization"
    EXTRACTION = "extraction"
    CLASSIFICATION = "classification"
    ARITHMETIC = "arithmetic"
    TRANSFORMATION = "transformation"
    CONSTRAINED_FORMAT = "constrained_format"


class PilotEvaluationKind(StrEnum):
    EXACT = "exact"
    CLASSIFICATION = "classification"
    EXTRACTION = "extraction"
    NUMERIC = "numeric"
    REQUIRED_FACTS = "required_facts"
    FIXED_FORMAT = "fixed_format"
    UNEVALUABLE = "unevaluable"


class PilotCorrectness(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNEVALUABLE = "unevaluable"


@dataclass(frozen=True, slots=True)
class PilotTask:
    task_id: str
    family: PilotTaskFamily
    prompt: str
    maximum_output_tokens: int
    evaluation_kind: PilotEvaluationKind
    expected_answer: str | None = None
    allowed_answers: tuple[str, ...] = ()
    required_facts: tuple[str, ...] = ()
    format_pattern: str | None = None

    def __post_init__(self):
        _id(self.task_id, "task_id")
        _text(self.prompt, "prompt")
        try:
            object.__setattr__(self, "family", PilotTaskFamily(self.family))
            object.__setattr__(self, "evaluation_kind", PilotEvaluationKind(self.evaluation_kind))
        except (TypeError, ValueError) as exc:
            raise PilotContractError("unsupported pilot task family or evaluation") from exc
        if (not isinstance(self.maximum_output_tokens, int)
                or isinstance(self.maximum_output_tokens, bool)
                or not 1 <= self.maximum_output_tokens <= 96):
            raise PilotContractError("maximum_output_tokens must be within [1, 96]")
        if self.expected_answer is not None:
            _text(self.expected_answer, "expected_answer")
        allowed = tuple(_text(item, "allowed_answer") for item in self.allowed_answers)
        facts = tuple(_text(item, "required_fact") for item in self.required_facts)
        if len(set(allowed)) != len(allowed) or len(set(facts)) != len(facts):
            raise PilotContractError("evaluation values must be unique")
        object.__setattr__(self, "allowed_answers", allowed)
        object.__setattr__(self, "required_facts", facts)
        if self.format_pattern is not None:
            _text(self.format_pattern, "format_pattern")
            try: re.compile(self.format_pattern)
            except re.error as exc: raise PilotContractError("invalid format_pattern") from exc
        if self.evaluation_kind in {PilotEvaluationKind.EXACT,
                PilotEvaluationKind.CLASSIFICATION, PilotEvaluationKind.NUMERIC} \
                and self.expected_answer is None:
            raise PilotContractError("evaluation requires expected_answer")
        if self.evaluation_kind is PilotEvaluationKind.CLASSIFICATION and (
                not allowed or self.expected_answer not in allowed):
            raise PilotContractError("classification answer must belong to allowed_answers")
        if self.evaluation_kind in {PilotEvaluationKind.EXTRACTION,
                PilotEvaluationKind.REQUIRED_FACTS} and not facts:
            raise PilotContractError("evaluation requires required_facts")
        if self.evaluation_kind is PilotEvaluationKind.FIXED_FORMAT and self.format_pattern is None:
            raise PilotContractError("fixed-format evaluation requires format_pattern")

    def to_dict(self):
        return {"task_id": self.task_id, "family": self.family.value, "prompt": self.prompt,
            "maximum_output_tokens": self.maximum_output_tokens,
            "evaluation_kind": self.evaluation_kind.value,
            "expected_answer": self.expected_answer, "allowed_answers": list(self.allowed_answers),
            "required_facts": list(self.required_facts), "format_pattern": self.format_pattern}

    def prompt_fingerprint(self): return hashlib.sha256(self.prompt.encode()).hexdigest()
    def task_fingerprint(self): return _fp(self.to_dict())


@dataclass(frozen=True, slots=True)
class PilotDefinition:
    pilot_id: str
    version: str
    tasks: tuple[PilotTask, ...]

    def __post_init__(self):
        _id(self.pilot_id, "pilot_id"); _id(self.version, "version")
        if not isinstance(self.tasks, tuple) or not self.tasks:
            raise PilotContractError("tasks must be a non-empty tuple")
        if any(type(task) is not PilotTask for task in self.tasks):
            raise PilotContractError("tasks must contain PilotTask values")
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise PilotContractError("task identities must be unique")
        if tuple(sorted(ids)) != tuple(ids):
            raise PilotContractError("tasks must use deterministic task-id ordering")

    def to_dict(self):
        return {"pilot_id": self.pilot_id, "version": self.version,
            "tasks": [task.to_dict() for task in self.tasks]}

    def definition_fingerprint(self): return _fp(self.to_dict())

    def benchmark_suite(self) -> BenchmarkSuite:
        """Project the pilot into the accepted benchmark task contract."""
        specs = tuple(BenchmarkTaskSpec(task.task_id, task.family.value, task.prompt,
            task.expected_answer or "Evaluated only by the declared pilot rubric.",
            TaskDifficulty.SIMPLE, frozenset({"text_generation"}), frozenset(), (),
            {"pilot_task_fingerprint": task.task_fingerprint(),
                "evaluation_kind": task.evaluation_kind.value},
            CorrectnessType.INSUFFICIENT_EVIDENCE if task.evaluation_kind is PilotEvaluationKind.UNEVALUABLE
                else CorrectnessType.OBJECTIVE) for task in self.tasks)
        return BenchmarkSuite(self.pilot_id, "Small Local-Only Capability Pilot", self.version,
            "2026-08-09T00:00:00Z", "A bounded local-only capability pilot.", specs,
            {"definition_fingerprint": self.definition_fingerprint(), "cloud_arms": 0})

    def experiment_definition(self) -> ExperimentDefinition:
        """Project the pilot into exactly one accepted SINGLE_LOCAL arm."""
        arm = ExperimentArm("single-local", ExperimentArmType.SINGLE_LOCAL,
            "bare_local_chat", LocalityType.LOCAL, "One governed local CHAT inference per task.",
            frozenset({"text_generation"}))
        tasks = tuple(ExperimentTask(self.pilot_id, task.task_id, task.prompt,
            task.expected_answer or "Declared pilot rubric result", frozenset({"text_generation"}),
            (task.task_fingerprint(),)) for task in self.tasks)
        return ExperimentDefinition(self.pilot_id, "2026-08-09T00:00:00Z",
            "Measure a small bare local-model task suite without cloud or tools.", (arm,), tasks)


@dataclass(frozen=True, slots=True)
class PilotEvaluation:
    correctness: PilotCorrectness
    reason: str
    matched_facts: tuple[str, ...]

    def __post_init__(self):
        try: object.__setattr__(self, "correctness", PilotCorrectness(self.correctness))
        except (TypeError, ValueError) as exc: raise PilotContractError("invalid correctness") from exc
        _id(self.reason, "reason")
        object.__setattr__(self, "matched_facts", tuple(self.matched_facts))

    def to_dict(self):
        return {"correctness": self.correctness.value, "reason": self.reason,
            "matched_facts": list(self.matched_facts)}


def _normalize(value: str) -> str:
    return " ".join(value.strip().split())


def evaluate_output(task: PilotTask, output: str) -> PilotEvaluation:
    if type(task) is not PilotTask or not isinstance(output, str):
        raise PilotContractError("evaluation requires exact pilot task and text output")
    normalized = _normalize(output)
    kind = task.evaluation_kind
    if kind is PilotEvaluationKind.UNEVALUABLE:
        return PilotEvaluation(PilotCorrectness.UNEVALUABLE, "no_objective_rubric", ())
    if kind is PilotEvaluationKind.EXACT:
        ok = normalized == _normalize(task.expected_answer or "")
    elif kind is PilotEvaluationKind.CLASSIFICATION:
        candidate = normalized.upper()
        ok = candidate in task.allowed_answers and candidate == task.expected_answer
    elif kind is PilotEvaluationKind.NUMERIC:
        try: ok = float(normalized) == float(task.expected_answer or "") and math.isfinite(float(normalized))
        except ValueError: ok = False
    elif kind in {PilotEvaluationKind.EXTRACTION, PilotEvaluationKind.REQUIRED_FACTS}:
        matched = tuple(fact for fact in task.required_facts if fact.casefold() in normalized.casefold())
        return PilotEvaluation(PilotCorrectness.CORRECT if len(matched) == len(task.required_facts)
            else PilotCorrectness.INCORRECT, "required_facts_match" if len(matched) == len(task.required_facts)
            else "required_facts_missing", matched)
    else:
        ok = re.fullmatch(task.format_pattern or r"(?!)", normalized) is not None
    return PilotEvaluation(PilotCorrectness.CORRECT if ok else PilotCorrectness.INCORRECT,
        "objective_match" if ok else "objective_mismatch", ())


@dataclass(frozen=True, slots=True)
class PilotTaskRecord:
    task_id: str; task_family: PilotTaskFamily; prompt_fingerprint: str
    model_descriptor_fingerprint: str; request_fingerprint: str; execution_fingerprint: str
    result_id: str; status: str; execution_succeeded: bool; evaluation: PilotEvaluation
    prompt_tokens: int | None; generated_tokens: int | None
    prompt_evaluated_tokens: int | None; generated_evaluated_tokens: int | None
    prompt_ms: float | None; generation_ms: float | None; elapsed_seconds: float
    termination_reason: str; locality: LocalityType; remote_execution: bool
    cloud_escalation_count: int; cloud_cost_usd: float; error_code: str | None

    def __post_init__(self):
        _id(self.task_id, "task_id")
        try:
            object.__setattr__(self, "task_family", PilotTaskFamily(self.task_family))
            object.__setattr__(self, "locality", LocalityType(self.locality))
        except (TypeError, ValueError) as exc:
            raise PilotContractError("invalid task family or locality") from exc
        for name in ("prompt_fingerprint", "model_descriptor_fingerprint",
                "request_fingerprint", "execution_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise PilotContractError(f"{name} must be lowercase SHA-256 hex")
        _id(self.result_id, "result_id"); _id(self.status, "status")
        if not isinstance(self.execution_succeeded, bool) or type(self.evaluation) is not PilotEvaluation:
            raise PilotContractError("record execution/evaluation state is malformed")
        if self.execution_succeeded != (self.status == LocalInferenceStatus.SUCCEEDED.value):
            raise PilotContractError("execution success contradicts status")
        for name in ("prompt_tokens", "generated_tokens", "prompt_evaluated_tokens",
                "generated_evaluated_tokens", "cloud_escalation_count"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise PilotContractError(f"{name} must be non-negative integer or None")
        for name in ("prompt_ms", "generation_ms", "elapsed_seconds", "cloud_cost_usd"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value < 0):
                raise PilotContractError(f"{name} must be finite and non-negative")
        _id(self.termination_reason, "termination_reason")
        if (self.locality is not LocalityType.LOCAL or self.remote_execution is not False
                or self.cloud_escalation_count != 0 or self.cloud_cost_usd != 0):
            raise PilotContractError("pilot record violates local-only policy")
        if self.error_code is not None: _id(self.error_code, "error_code")
        if self.execution_succeeded and self.error_code is not None:
            raise PilotContractError("successful execution cannot contain error")

    @classmethod
    def from_evidence(cls, task, request, result):
        if type(task) is not PilotTask or type(request) is not LocalInferenceRequest \
                or type(result) is not LocalInferenceResult:
            raise PilotContractError("authoritative task, request, and result contracts required")
        if request.task_id != task.task_id or request.input_fingerprint != task.prompt_fingerprint():
            raise PilotContractError("task/request linkage mismatch")
        if result.request_fingerprint != request.request_fingerprint():
            raise PilotContractError("request/result linkage mismatch")
        response = result.response
        if (response.locality is not LocalityType.LOCAL or response.remote_execution
                or response.cloud_escalation_count or (response.cloud_cost_usd or 0) != 0):
            raise PilotContractError("pilot evidence violates local-only policy")
        resources = response.resource_measurements
        prompt_ms = resources.get("prompt_ms")
        generation_ms = resources.get("predicted_ms", resources.get("generation_ms"))
        prompt_evaluated_tokens = resources.get("prompt_evaluated_tokens")
        generated_evaluated_tokens = resources.get("generated_evaluated_tokens")
        evaluation = (evaluate_output(task, response.output_text or "")
            if response.status is LocalInferenceStatus.SUCCEEDED
            else PilotEvaluation(PilotCorrectness.UNEVALUABLE, "execution_failed", ()))
        return cls(task.task_id, task.family, task.prompt_fingerprint(),
            result.descriptor_fingerprint, result.request_fingerprint, request.execution_fingerprint,
            result.result_id, response.status.value,
            response.status is LocalInferenceStatus.SUCCEEDED, evaluation,
            response.usage.prompt_tokens, response.usage.generated_tokens,
            prompt_evaluated_tokens, generated_evaluated_tokens,
            prompt_ms, generation_ms, response.elapsed_seconds, response.termination_reason,
            response.locality, response.remote_execution, response.cloud_escalation_count,
            float(response.cloud_cost_usd or 0), response.error_code)

    def to_dict(self):
        return {name: (value.value if isinstance(value, StrEnum) else value.to_dict()
            if isinstance(value, PilotEvaluation) else value)
            for name, value in ((slot, getattr(self, slot)) for slot in self.__slots__)}


@dataclass(frozen=True, slots=True)
class PilotAggregateMetrics:
    tasks_attempted: int; tasks_successfully_executed: int
    objectively_correct: int; objectively_incorrect: int; unevaluable: int
    execution_failures: int; mean_elapsed_seconds: float; median_elapsed_seconds: float
    mean_prompt_throughput: float | None; mean_generation_throughput: float | None
    total_cloud_cost_usd: float; cloud_escalation_count: int

    def __post_init__(self):
        for name in ("tasks_attempted", "tasks_successfully_executed", "objectively_correct",
                "objectively_incorrect", "unevaluable", "execution_failures",
                "cloud_escalation_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PilotContractError(f"{name} must be non-negative integer")
        if self.tasks_successfully_executed + self.execution_failures != self.tasks_attempted:
            raise PilotContractError("execution aggregates are contradictory")
        if self.objectively_correct + self.objectively_incorrect + self.unevaluable != self.tasks_attempted:
            raise PilotContractError("correctness aggregates are contradictory")
        for name in ("mean_elapsed_seconds", "median_elapsed_seconds",
                "mean_prompt_throughput", "mean_generation_throughput", "total_cloud_cost_usd"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value < 0):
                raise PilotContractError(f"{name} must be finite and non-negative")
        if self.total_cloud_cost_usd != 0 or self.cloud_escalation_count != 0:
            raise PilotContractError("pilot aggregate violates local-only policy")

    def to_dict(self): return {slot: getattr(self, slot) for slot in self.__slots__}


@dataclass(frozen=True, slots=True)
class PilotReport:
    run_id: str; definition_fingerprint: str; model_descriptor_fingerprint: str
    records: tuple[PilotTaskRecord, ...]; metrics: PilotAggregateMetrics
    report_fingerprint: str

    def __post_init__(self):
        _id(self.run_id, "run_id")
        for name in ("definition_fingerprint", "model_descriptor_fingerprint", "report_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise PilotContractError(f"{name} must be lowercase SHA-256 hex")
        if not isinstance(self.records, tuple) or any(type(item) is not PilotTaskRecord for item in self.records):
            raise PilotContractError("records must be immutable PilotTaskRecord values")
        if type(self.metrics) is not PilotAggregateMetrics:
            raise PilotContractError("metrics must use PilotAggregateMetrics")
        records = self.records
        if (self.metrics.tasks_attempted != len(records)
                or self.metrics.tasks_successfully_executed != sum(r.execution_succeeded for r in records)
                or self.metrics.objectively_correct != sum(
                    r.evaluation.correctness is PilotCorrectness.CORRECT for r in records)
                or self.metrics.objectively_incorrect != sum(
                    r.evaluation.correctness is PilotCorrectness.INCORRECT for r in records)
                or self.metrics.unevaluable != sum(
                    r.evaluation.correctness is PilotCorrectness.UNEVALUABLE for r in records)):
            raise PilotContractError("report metrics do not bind exact task records")
        payload = {"run_id": self.run_id, "definition_fingerprint": self.definition_fingerprint,
            "model_descriptor_fingerprint": self.model_descriptor_fingerprint,
            "records": [record.to_dict() for record in records], "metrics": self.metrics.to_dict()}
        if self.report_fingerprint != _fp(payload):
            raise PilotContractError("report fingerprint does not bind exact report evidence")

    @classmethod
    def create(cls, run_id, definition, descriptor, records):
        _id(run_id, "run_id")
        if type(definition) is not PilotDefinition or type(descriptor) is not LocalModelDescriptor:
            raise PilotContractError("pilot definition and model descriptor required")
        records = tuple(sorted(records, key=lambda item: item.task_id))
        if any(type(item) is not PilotTaskRecord for item in records):
            raise PilotContractError("records must contain PilotTaskRecord values")
        if len({item.task_id for item in records}) != len(records):
            raise PilotContractError("duplicate task records")
        known = {task.task_id for task in definition.tasks}
        descriptor_fp = descriptor.descriptor_fingerprint()
        if any(item.task_id not in known or item.model_descriptor_fingerprint != descriptor_fp
                for item in records):
            raise PilotContractError("foreign task or model evidence")
        elapsed = [item.elapsed_seconds for item in records]
        prompt_rates = [item.prompt_evaluated_tokens / (item.prompt_ms / 1000)
            for item in records if item.prompt_evaluated_tokens is not None
            and item.prompt_ms not in (None, 0)]
        generation_rates = [item.generated_evaluated_tokens / (item.generation_ms / 1000)
            for item in records if item.generated_evaluated_tokens is not None
            and item.generation_ms not in (None, 0)]
        metrics = PilotAggregateMetrics(len(records), sum(r.execution_succeeded for r in records),
            sum(r.evaluation.correctness is PilotCorrectness.CORRECT for r in records),
            sum(r.evaluation.correctness is PilotCorrectness.INCORRECT for r in records),
            sum(r.evaluation.correctness is PilotCorrectness.UNEVALUABLE for r in records),
            sum(not r.execution_succeeded for r in records), statistics.fmean(elapsed) if elapsed else 0,
            statistics.median(elapsed) if elapsed else 0,
            statistics.fmean(prompt_rates) if prompt_rates else None,
            statistics.fmean(generation_rates) if generation_rates else None,
            math.fsum(r.cloud_cost_usd for r in records), sum(r.cloud_escalation_count for r in records))
        payload = {"run_id": run_id, "definition_fingerprint": definition.definition_fingerprint(),
            "model_descriptor_fingerprint": descriptor_fp,
            "records": [record.to_dict() for record in records], "metrics": metrics.to_dict()}
        return cls(run_id, definition.definition_fingerprint(), descriptor_fp, records, metrics, _fp(payload))

    def to_dict(self):
        return {"run_id": self.run_id, "definition_fingerprint": self.definition_fingerprint,
            "model_descriptor_fingerprint": self.model_descriptor_fingerprint,
            "records": [record.to_dict() for record in self.records], "metrics": self.metrics.to_dict(),
            "report_fingerprint": self.report_fingerprint}


def build_default_pilot_definition() -> PilotDefinition:
    tasks = (
        PilotTask("pilot-01-exact", PilotTaskFamily.EXACT_INSTRUCTION, "Reply with exactly LOCAL_OK and nothing else.", 8, PilotEvaluationKind.EXACT, "LOCAL_OK"),
        PilotTask("pilot-02-fact", PilotTaskFamily.COMMON_KNOWLEDGE, "What planet is known as the Red Planet? Reply with only its name.", 8, PilotEvaluationKind.EXACT, "Mars"),
        PilotTask("pilot-03-summary", PilotTaskFamily.SUMMARIZATION, "Summarize in one sentence: Ada wrote notes about Charles Babbage's Analytical Engine.", 32, PilotEvaluationKind.REQUIRED_FACTS, required_facts=("Ada", "Analytical Engine")),
        PilotTask("pilot-04-extract", PilotTaskFamily.EXTRACTION, "Extract the city and temperature: Chicago recorded 21 C. Reply: CITY=<city>; TEMP=<number>", 24, PilotEvaluationKind.EXTRACTION, required_facts=("CITY=Chicago", "TEMP=21")),
        PilotTask("pilot-05-classify", PilotTaskFamily.CLASSIFICATION, "Classify water at 20 C as SOLID, LIQUID, or GAS. Reply one label.", 8, PilotEvaluationKind.CLASSIFICATION, "LIQUID", ("SOLID", "LIQUID", "GAS")),
        PilotTask("pilot-06-arithmetic", PilotTaskFamily.ARITHMETIC, "Compute 17 + 25. Reply only with the number.", 8, PilotEvaluationKind.NUMERIC, "42"),
        PilotTask("pilot-07-rewrite", PilotTaskFamily.TRANSFORMATION, "Rewrite 'the cat is small' in uppercase only.", 16, PilotEvaluationKind.EXACT, "THE CAT IS SMALL"),
        PilotTask("pilot-08-format", PilotTaskFamily.CONSTRAINED_FORMAT, "Reply with exactly two lines: NAME=Ada then ROLE=Mathematician.", 24, PilotEvaluationKind.FIXED_FORMAT, format_pattern=r"NAME=Ada\s+ROLE=Mathematician"),
        PilotTask("pilot-09-exact", PilotTaskFamily.EXACT_INSTRUCTION, "Reply with exactly YES.", 8, PilotEvaluationKind.EXACT, "YES"),
        PilotTask("pilot-10-extract", PilotTaskFamily.EXTRACTION, "From 'Order 7 contains 3 apples', reply with ORDER=7; COUNT=3", 16, PilotEvaluationKind.EXTRACTION, required_facts=("ORDER=7", "COUNT=3")),
        PilotTask("pilot-11-arithmetic", PilotTaskFamily.ARITHMETIC, "Compute 9 times 6. Reply only with the number.", 8, PilotEvaluationKind.NUMERIC, "54"),
        PilotTask("pilot-12-summary", PilotTaskFamily.SUMMARIZATION, "Summarize: The battery was charged, then the device started. Mention both facts.", 32, PilotEvaluationKind.REQUIRED_FACTS, required_facts=("battery", "device")),
    )
    return PilotDefinition("small-local-only-pilot-v0.1", "v0.1", tasks)
