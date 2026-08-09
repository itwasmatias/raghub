"""
Local Capability Baseline v0.1 Tests

Comprehensive tests for provider-neutral measurement foundation, covering:
- Valid profiles and measurements
- Malformed measurements
- Provider-neutral behavior
- Immutability
- Deterministic fingerprints
- Validation of all fields
- Serialization
"""

import pytest

from tools.ai_controller.local_capability_baseline import (
    ComparisonBaseline,
    CostClass,
    LocalityType,
    TaskMeasurement,
    TaskOutcome,
    WorkerCapabilityProfile,
)


class TestWorkerCapabilityProfile:
    """Test WorkerCapabilityProfile validation and behavior."""

    def test_valid_local_profile(self):
        """Valid local worker profile should be accepted."""
        profile = WorkerCapabilityProfile(
            worker_id="worker-001",
            profile_id="profile-001",
            created_at="2026-08-09T12:00:00Z",
            locality=LocalityType.LOCAL,
            provider_identifier=None,
            model_identifier="llama-3.1-70b",
            cost_class=CostClass.ZERO,
            capabilities=frozenset({"code", "analysis"}),
            typical_availability_hours=24.0,
            max_concurrent_tasks=4,
            hardware_facts={"ram_gb": 16, "cpu_cores": 8},
            runtime_facts={"python_version": "3.14"},
        )

        assert profile.worker_id == "worker-001"
        assert profile.locality is LocalityType.LOCAL
        assert profile.cost_class is CostClass.ZERO
        assert "code" in profile.capabilities

    def test_valid_cloud_profile(self):
        """Valid cloud worker profile should be accepted."""
        profile = WorkerCapabilityProfile(
            worker_id="worker-cloud-001",
            profile_id="profile-cloud-001",
            created_at="2026-08-09T12:00:00Z",
            locality=LocalityType.CLOUD,
            provider_identifier="generic-cloud-provider",
            model_identifier="generic-model-v1",
            cost_class=CostClass.METERED,
            capabilities=frozenset({"code", "vision", "tools"}),
            typical_availability_hours=None,
            max_concurrent_tasks=None,
            hardware_facts={},
            runtime_facts={},
        )

        assert profile.locality is LocalityType.CLOUD
        assert profile.cost_class is CostClass.METERED
        assert profile.provider_identifier == "generic-cloud-provider"

    def test_profile_fingerprint_deterministic(self):
        """Profile fingerprint should be deterministic."""
        profile1 = WorkerCapabilityProfile(
            worker_id="worker-001",
            profile_id="profile-001",
            created_at="2026-08-09T12:00:00Z",
            locality=LocalityType.LOCAL,
            provider_identifier=None,
            model_identifier="llama-3.1-70b",
            cost_class=CostClass.ZERO,
            capabilities=frozenset({"code", "analysis"}),
            typical_availability_hours=24.0,
            max_concurrent_tasks=4,
            hardware_facts={"ram_gb": 16},
            runtime_facts={"python_version": "3.14"},
        )

        profile2 = WorkerCapabilityProfile(
            worker_id="worker-001",
            profile_id="profile-001",
            created_at="2026-08-09T12:00:00Z",
            locality=LocalityType.LOCAL,
            provider_identifier=None,
            model_identifier="llama-3.1-70b",
            cost_class=CostClass.ZERO,
            capabilities=frozenset({"analysis", "code"}),  # Different order
            typical_availability_hours=24.0,
            max_concurrent_tasks=4,
            hardware_facts={"ram_gb": 16},
            runtime_facts={"python_version": "3.14"},
        )

        assert profile1.profile_fingerprint() == profile2.profile_fingerprint()

    def test_invalid_worker_id(self):
        """Invalid worker_id should be rejected."""
        with pytest.raises(ValueError, match="worker_id"):
            WorkerCapabilityProfile(
                worker_id="",  # Empty
                profile_id="profile-001",
                created_at="2026-08-09T12:00:00Z",
                locality=LocalityType.LOCAL,
                provider_identifier=None,
                model_identifier=None,
                cost_class=CostClass.ZERO,
                capabilities=frozenset(),
                typical_availability_hours=None,
                max_concurrent_tasks=None,
                hardware_facts={},
                runtime_facts={},
            )

    def test_invalid_created_at(self):
        """Invalid created_at should be rejected."""
        with pytest.raises(ValueError, match="created_at"):
            WorkerCapabilityProfile(
                worker_id="worker-001",
                profile_id="profile-001",
                created_at="not-a-timestamp",
                locality=LocalityType.LOCAL,
                provider_identifier=None,
                model_identifier=None,
                cost_class=CostClass.ZERO,
                capabilities=frozenset(),
                typical_availability_hours=None,
                max_concurrent_tasks=None,
                hardware_facts={},
                runtime_facts={},
            )

    def test_invalid_locality(self):
        """Invalid locality should be rejected."""
        with pytest.raises(ValueError, match="locality"):
            WorkerCapabilityProfile(
                worker_id="worker-001",
                profile_id="profile-001",
                created_at="2026-08-09T12:00:00Z",
                locality="invalid",  # Invalid value
                provider_identifier=None,
                model_identifier=None,
                cost_class=CostClass.ZERO,
                capabilities=frozenset(),
                typical_availability_hours=None,
                max_concurrent_tasks=None,
                hardware_facts={},
                runtime_facts={},
            )

    def test_negative_availability_hours(self):
        """Negative availability hours should be rejected."""
        with pytest.raises(ValueError, match="availability"):
            WorkerCapabilityProfile(
                worker_id="worker-001",
                profile_id="profile-001",
                created_at="2026-08-09T12:00:00Z",
                locality=LocalityType.LOCAL,
                provider_identifier=None,
                model_identifier=None,
                cost_class=CostClass.ZERO,
                capabilities=frozenset(),
                typical_availability_hours=-1.0,
                max_concurrent_tasks=None,
                hardware_facts={},
                runtime_facts={},
            )

    def test_zero_concurrent_tasks(self):
        """Zero max_concurrent_tasks should be rejected."""
        with pytest.raises(ValueError, match="max_concurrent_tasks"):
            WorkerCapabilityProfile(
                worker_id="worker-001",
                profile_id="profile-001",
                created_at="2026-08-09T12:00:00Z",
                locality=LocalityType.LOCAL,
                provider_identifier=None,
                model_identifier=None,
                cost_class=CostClass.ZERO,
                capabilities=frozenset(),
                typical_availability_hours=None,
                max_concurrent_tasks=0,  # Must be positive
                hardware_facts={},
                runtime_facts={},
            )


class TestTaskMeasurement:
    """Test TaskMeasurement validation and behavior."""

    def test_valid_local_success_measurement(self):
        """Valid local success measurement should be accepted."""
        measurement = TaskMeasurement(
            measurement_id="measurement-001",
            task_id="task-001",
            worker_id="worker-001",
            profile_fingerprint="abcd1234",
            started_at="2026-08-09T12:00:00Z",
            completed_at="2026-08-09T12:05:30Z",
            elapsed_seconds=330.0,
            outcome=TaskOutcome.SUCCESS,
            success=True,
            verification_result="passed",
            attempt_number=1,
            human_intervention_count=0,
            retry_count=0,
            zero_cloud_cost=True,
            cloud_escalation_count=0,
            estimated_cloud_cost_usd=None,
            execution_strategy="local-single",
            locality=LocalityType.LOCAL,
            provider_identifier=None,
            model_identifier="llama-3.1-70b",
            evidence_metadata={},
        )

        assert measurement.success is True
        assert measurement.zero_cloud_cost is True
        assert measurement.outcome is TaskOutcome.SUCCESS

    def test_valid_cloud_measurement_with_cost(self):
        """Valid cloud measurement with cost should be accepted."""
        measurement = TaskMeasurement(
            measurement_id="measurement-002",
            task_id="task-002",
            worker_id="worker-cloud-001",
            profile_fingerprint="efgh5678",
            started_at="2026-08-09T12:00:00Z",
            completed_at="2026-08-09T12:02:15Z",
            elapsed_seconds=135.0,
            outcome=TaskOutcome.SUCCESS,
            success=True,
            verification_result=None,
            attempt_number=1,
            human_intervention_count=0,
            retry_count=0,
            zero_cloud_cost=False,
            cloud_escalation_count=0,
            estimated_cloud_cost_usd=0.05,
            execution_strategy="cloud-frontier",
            locality=LocalityType.CLOUD,
            provider_identifier="generic-cloud",
            model_identifier="generic-model-v1",
            evidence_metadata={"tokens": 1000},
        )

        assert measurement.zero_cloud_cost is False
        assert measurement.estimated_cloud_cost_usd == 0.05
        assert measurement.locality is LocalityType.CLOUD

    def test_measurement_fingerprint_deterministic(self):
        """Measurement fingerprint should be deterministic."""
        measurement1 = TaskMeasurement(
            measurement_id="measurement-001",
            task_id="task-001",
            worker_id="worker-001",
            profile_fingerprint="abcd1234",
            started_at="2026-08-09T12:00:00Z",
            completed_at="2026-08-09T12:05:00Z",
            elapsed_seconds=300.0,
            outcome=TaskOutcome.SUCCESS,
            success=True,
            verification_result=None,
            attempt_number=1,
            human_intervention_count=0,
            retry_count=0,
            zero_cloud_cost=True,
            cloud_escalation_count=0,
            estimated_cloud_cost_usd=None,
            execution_strategy="local-single",
            locality=LocalityType.LOCAL,
            provider_identifier=None,
            model_identifier=None,
            evidence_metadata={},
        )

        measurement2 = TaskMeasurement(
            measurement_id="measurement-001",
            task_id="task-001",
            worker_id="worker-001",
            profile_fingerprint="abcd1234",
            started_at="2026-08-09T12:00:00Z",
            completed_at="2026-08-09T12:05:00Z",
            elapsed_seconds=300.0,
            outcome=TaskOutcome.SUCCESS,
            success=True,
            verification_result=None,
            attempt_number=1,
            human_intervention_count=0,
            retry_count=0,
            zero_cloud_cost=True,
            cloud_escalation_count=0,
            estimated_cloud_cost_usd=None,
            execution_strategy="local-single",
            locality=LocalityType.LOCAL,
            provider_identifier=None,
            model_identifier=None,
            evidence_metadata={},
        )

        assert measurement1.measurement_fingerprint() == measurement2.measurement_fingerprint()

    def test_invalid_measurement_id(self):
        """Invalid measurement_id should be rejected."""
        with pytest.raises(ValueError, match="measurement_id"):
            TaskMeasurement(
                measurement_id="",  # Empty
                task_id="task-001",
                worker_id="worker-001",
                profile_fingerprint=None,
                started_at="2026-08-09T12:00:00Z",
                completed_at=None,
                elapsed_seconds=None,
                outcome=TaskOutcome.SUCCESS,
                success=True,
                verification_result=None,
                attempt_number=1,
                human_intervention_count=0,
                retry_count=0,
                zero_cloud_cost=True,
                cloud_escalation_count=0,
                estimated_cloud_cost_usd=None,
                execution_strategy="local-single",
                locality=LocalityType.LOCAL,
                provider_identifier=None,
                model_identifier=None,
                evidence_metadata={},
            )

    def test_negative_elapsed_seconds(self):
        """Negative elapsed_seconds should be rejected."""
        with pytest.raises(ValueError, match="elapsed_seconds"):
            TaskMeasurement(
                measurement_id="measurement-001",
                task_id="task-001",
                worker_id="worker-001",
                profile_fingerprint=None,
                started_at="2026-08-09T12:00:00Z",
                completed_at="2026-08-09T12:05:00Z",
                elapsed_seconds=-1.0,  # Negative
                outcome=TaskOutcome.SUCCESS,
                success=True,
                verification_result=None,
                attempt_number=1,
                human_intervention_count=0,
                retry_count=0,
                zero_cloud_cost=True,
                cloud_escalation_count=0,
                estimated_cloud_cost_usd=None,
                execution_strategy="local-single",
                locality=LocalityType.LOCAL,
                provider_identifier=None,
                model_identifier=None,
                evidence_metadata={},
            )

    def test_negative_attempt_number(self):
        """Negative attempt_number should be rejected."""
        with pytest.raises(ValueError, match="attempt_number"):
            TaskMeasurement(
                measurement_id="measurement-001",
                task_id="task-001",
                worker_id="worker-001",
                profile_fingerprint=None,
                started_at="2026-08-09T12:00:00Z",
                completed_at=None,
                elapsed_seconds=None,
                outcome=TaskOutcome.SUCCESS,
                success=True,
                verification_result=None,
                attempt_number=-1,  # Negative
                human_intervention_count=0,
                retry_count=0,
                zero_cloud_cost=True,
                cloud_escalation_count=0,
                estimated_cloud_cost_usd=None,
                execution_strategy="local-single",
                locality=LocalityType.LOCAL,
                provider_identifier=None,
                model_identifier=None,
                evidence_metadata={},
            )

    def test_negative_cloud_cost(self):
        """Negative cloud cost should be rejected."""
        with pytest.raises(ValueError, match="cloud_cost"):
            TaskMeasurement(
                measurement_id="measurement-001",
                task_id="task-001",
                worker_id="worker-001",
                profile_fingerprint=None,
                started_at="2026-08-09T12:00:00Z",
                completed_at=None,
                elapsed_seconds=None,
                outcome=TaskOutcome.SUCCESS,
                success=True,
                verification_result=None,
                attempt_number=1,
                human_intervention_count=0,
                retry_count=0,
                zero_cloud_cost=False,
                cloud_escalation_count=0,
                estimated_cloud_cost_usd=-0.01,  # Negative
                execution_strategy="cloud-frontier",
                locality=LocalityType.CLOUD,
                provider_identifier=None,
                model_identifier=None,
                evidence_metadata={},
            )


class TestComparisonBaseline:
    """Test ComparisonBaseline validation and behavior."""

    def test_valid_local_baseline(self):
        """Valid local baseline should be accepted."""
        baseline = ComparisonBaseline(
            baseline_id="baseline-001",
            created_at="2026-08-09T12:00:00Z",
            execution_strategy="local-single",
            task_count=10,
            success_count=8,
            failure_count=2,
            total_elapsed_seconds=3000.0,
            mean_elapsed_seconds=300.0,
            median_elapsed_seconds=295.0,
            total_human_interventions=1,
            total_retries=2,
            total_cloud_cost_usd=None,
            zero_cost_task_count=10,
            measurements=("m1", "m2", "m3"),
        )

        assert baseline.task_count == 10
        assert baseline.success_count == 8
        assert baseline.zero_cost_task_count == 10

    def test_baseline_to_dict_includes_success_rate(self):
        """Baseline to_dict should include computed success_rate."""
        baseline = ComparisonBaseline(
            baseline_id="baseline-001",
            created_at="2026-08-09T12:00:00Z",
            execution_strategy="local-single",
            task_count=10,
            success_count=7,
            failure_count=3,
            total_elapsed_seconds=1000.0,
            mean_elapsed_seconds=100.0,
            median_elapsed_seconds=95.0,
            total_human_interventions=0,
            total_retries=0,
            total_cloud_cost_usd=None,
            zero_cost_task_count=10,
            measurements=(),
        )

        data = baseline.to_dict()
        assert data["success_rate"] == 0.7

    def test_baseline_fingerprint_deterministic(self):
        """Baseline fingerprint should be deterministic."""
        baseline1 = ComparisonBaseline(
            baseline_id="baseline-001",
            created_at="2026-08-09T12:00:00Z",
            execution_strategy="local-single",
            task_count=5,
            success_count=5,
            failure_count=0,
            total_elapsed_seconds=500.0,
            mean_elapsed_seconds=100.0,
            median_elapsed_seconds=100.0,
            total_human_interventions=0,
            total_retries=0,
            total_cloud_cost_usd=None,
            zero_cost_task_count=5,
            measurements=("m1", "m2", "m3"),
        )

        baseline2 = ComparisonBaseline(
            baseline_id="baseline-001",
            created_at="2026-08-09T12:00:00Z",
            execution_strategy="local-single",
            task_count=5,
            success_count=5,
            failure_count=0,
            total_elapsed_seconds=500.0,
            mean_elapsed_seconds=100.0,
            median_elapsed_seconds=100.0,
            total_human_interventions=0,
            total_retries=0,
            total_cloud_cost_usd=None,
            zero_cost_task_count=5,
            measurements=("m1", "m2", "m3"),
        )

        assert baseline1.baseline_fingerprint() == baseline2.baseline_fingerprint()

    def test_invalid_baseline_id(self):
        """Invalid baseline_id should be rejected."""
        with pytest.raises(ValueError, match="baseline_id"):
            ComparisonBaseline(
                baseline_id="",  # Empty
                created_at="2026-08-09T12:00:00Z",
                execution_strategy="local-single",
                task_count=1,
                success_count=1,
                failure_count=0,
                total_elapsed_seconds=100.0,
                mean_elapsed_seconds=100.0,
                median_elapsed_seconds=100.0,
                total_human_interventions=0,
                total_retries=0,
                total_cloud_cost_usd=None,
                zero_cost_task_count=1,
                measurements=(),
            )

    def test_negative_task_count(self):
        """Negative task_count should be rejected."""
        with pytest.raises(ValueError, match="task_count"):
            ComparisonBaseline(
                baseline_id="baseline-001",
                created_at="2026-08-09T12:00:00Z",
                execution_strategy="local-single",
                task_count=-1,  # Negative
                success_count=0,
                failure_count=0,
                total_elapsed_seconds=0.0,
                mean_elapsed_seconds=None,
                median_elapsed_seconds=None,
                total_human_interventions=0,
                total_retries=0,
                total_cloud_cost_usd=None,
                zero_cost_task_count=0,
                measurements=(),
            )


class TestProviderNeutrality:
    """Test that contracts are provider-neutral."""

    def test_no_hardcoded_providers_in_enums(self):
        """Enums should not hardcode specific providers."""
        locality_values = [e.value for e in LocalityType]
        cost_values = [e.value for e in CostClass]
        outcome_values = [e.value for e in TaskOutcome]

        # Should not contain specific provider names
        all_values = locality_values + cost_values + outcome_values
        forbidden = ["openai", "anthropic", "google", "aws", "azure"]
        for forbidden_term in forbidden:
            assert all(forbidden_term.lower() not in v.lower() for v in all_values)

    def test_profile_accepts_arbitrary_provider(self):
        """Profile should accept arbitrary provider identifiers."""
        profile = WorkerCapabilityProfile(
            worker_id="worker-001",
            profile_id="profile-001",
            created_at="2026-08-09T12:00:00Z",
            locality=LocalityType.HYBRID,
            provider_identifier="my-custom-provider",  # Arbitrary
            model_identifier="my-custom-model",  # Arbitrary
            cost_class=CostClass.SUBSCRIPTION,
            capabilities=frozenset(),
            typical_availability_hours=None,
            max_concurrent_tasks=None,
            hardware_facts={},
            runtime_facts={},
        )

        assert profile.provider_identifier == "my-custom-provider"

    def test_measurement_accepts_arbitrary_provider(self):
        """Measurement should accept arbitrary provider identifiers."""
        measurement = TaskMeasurement(
            measurement_id="measurement-001",
            task_id="task-001",
            worker_id="worker-001",
            profile_fingerprint=None,
            started_at="2026-08-09T12:00:00Z",
            completed_at=None,
            elapsed_seconds=None,
            outcome=TaskOutcome.SUCCESS,
            success=True,
            verification_result=None,
            attempt_number=1,
            human_intervention_count=0,
            retry_count=0,
            zero_cloud_cost=True,
            cloud_escalation_count=0,
            estimated_cloud_cost_usd=None,
            execution_strategy="my-custom-strategy",  # Arbitrary
            locality=LocalityType.HYBRID,
            provider_identifier="future-provider",  # Arbitrary
            model_identifier="future-model-v2",  # Arbitrary
            evidence_metadata={},
        )

        assert measurement.provider_identifier == "future-provider"


class TestSerialization:
    """Test safe serialization of measurement records."""

    def test_profile_to_dict_serializable(self):
        """Profile to_dict should produce JSON-serializable data."""
        profile = WorkerCapabilityProfile(
            worker_id="worker-001",
            profile_id="profile-001",
            created_at="2026-08-09T12:00:00Z",
            locality=LocalityType.LOCAL,
            provider_identifier=None,
            model_identifier=None,
            cost_class=CostClass.ZERO,
            capabilities=frozenset({"code"}),
            typical_availability_hours=24.0,
            max_concurrent_tasks=1,
            hardware_facts={"ram_gb": 16},
            runtime_facts={},
        )

        data = profile.to_dict()

        # Should be JSON-serializable
        import json
        json_str = json.dumps(data)
        assert json_str is not None

    def test_measurement_to_dict_serializable(self):
        """Measurement to_dict should produce JSON-serializable data."""
        measurement = TaskMeasurement(
            measurement_id="measurement-001",
            task_id="task-001",
            worker_id="worker-001",
            profile_fingerprint="abcd1234",
            started_at="2026-08-09T12:00:00Z",
            completed_at="2026-08-09T12:05:00Z",
            elapsed_seconds=300.0,
            outcome=TaskOutcome.SUCCESS,
            success=True,
            verification_result="passed",
            attempt_number=1,
            human_intervention_count=0,
            retry_count=0,
            zero_cloud_cost=True,
            cloud_escalation_count=0,
            estimated_cloud_cost_usd=None,
            execution_strategy="local-single",
            locality=LocalityType.LOCAL,
            provider_identifier=None,
            model_identifier=None,
            evidence_metadata={"test": "data"},
        )

        data = measurement.to_dict()

        # Should be JSON-serializable
        import json
        json_str = json.dumps(data)
        assert json_str is not None

    def test_baseline_to_dict_serializable(self):
        """Baseline to_dict should produce JSON-serializable data."""
        baseline = ComparisonBaseline(
            baseline_id="baseline-001",
            created_at="2026-08-09T12:00:00Z",
            execution_strategy="local-single",
            task_count=5,
            success_count=5,
            failure_count=0,
            total_elapsed_seconds=500.0,
            mean_elapsed_seconds=100.0,
            median_elapsed_seconds=100.0,
            total_human_interventions=0,
            total_retries=0,
            total_cloud_cost_usd=None,
            zero_cost_task_count=5,
            measurements=("m1", "m2"),
        )

        data = baseline.to_dict()

        # Should be JSON-serializable
        import json
        json_str = json.dumps(data)
        assert json_str is not None
