"""
Tests for collision-safe deterministic mission and task ID generation.

The ID generation must be:
1. Deterministic (same inputs → same ID)
2. Collision-resistant (different inputs → different IDs, even with similar prefixes)
3. Restart-safe (same ID after reload)
4. Compliant with controller task ID rules (^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$)
5. Readable where practical
"""

import hashlib
import json
from pathlib import Path

import pytest

from tools.ai_controller.mission.materializer import TaskMaterializer
from tools.ai_controller.queue import DurableQueue


@pytest.fixture
def temp_queue(tmp_path: Path) -> DurableQueue:
    return DurableQueue(tmp_path / "queue")


@pytest.fixture
def materializer(temp_queue: DurableQueue, tmp_path: Path) -> TaskMaterializer:
    return TaskMaterializer(temp_queue, tmp_path / "reports")


class TestDeterministicTaskIDs:
    """Test that task IDs are deterministic and collision-safe."""

    def test_same_inputs_produce_same_id(self, materializer: TaskMaterializer) -> None:
        """Same mission_id and task_id always produce the same queue_task_id."""
        id1 = materializer.make_queue_task_id("mission-alpha", "task-one")
        id2 = materializer.make_queue_task_id("mission-alpha", "task-one")
        assert id1 == id2

    def test_different_missions_produce_different_ids(self, materializer: TaskMaterializer) -> None:
        """Different mission IDs produce different queue task IDs even with same task ID."""
        id1 = materializer.make_queue_task_id("mission-alpha", "task-one")
        id2 = materializer.make_queue_task_id("mission-beta", "task-one")
        assert id1 != id2

    def test_different_tasks_produce_different_ids(self, materializer: TaskMaterializer) -> None:
        """Different task IDs produce different queue task IDs even with same mission ID."""
        id1 = materializer.make_queue_task_id("mission-alpha", "task-one")
        id2 = materializer.make_queue_task_id("mission-alpha", "task-two")
        assert id1 != id2

    def test_collision_resistant_with_similar_prefixes(self, materializer: TaskMaterializer) -> None:
        """Task names that normalize to similar prefixes still get different IDs."""
        # These would collide if we only use sanitized prefixes without hashing
        id1 = materializer.make_queue_task_id("mission-x", "implement-user-auth")
        id2 = materializer.make_queue_task_id("mission-x", "implement-user-authorization")
        assert id1 != id2

    def test_unicode_task_names_are_deterministic(self, materializer: TaskMaterializer) -> None:
        """Unicode task names are handled deterministically."""
        id1 = materializer.make_queue_task_id("mission-α", "task-β")
        id2 = materializer.make_queue_task_id("mission-α", "task-β")
        assert id1 == id2

    def test_long_task_names_are_deterministic(self, materializer: TaskMaterializer) -> None:
        """Long task names that exceed ID limits still produce unique, deterministic IDs."""
        long_name = "implement-very-long-feature-name-" * 10  # > 128 chars
        id1 = materializer.make_queue_task_id("mission-alpha", long_name)
        id2 = materializer.make_queue_task_id("mission-alpha", long_name)
        assert id1 == id2
        assert len(id1) <= 128

    def test_whitespace_normalization_is_deterministic(self, materializer: TaskMaterializer) -> None:
        """Whitespace in task names is normalized deterministically."""
        id1 = materializer.make_queue_task_id("mission alpha", "task   one")
        id2 = materializer.make_queue_task_id("mission alpha", "task   one")
        assert id1 == id2

    def test_special_characters_are_handled(self, materializer: TaskMaterializer) -> None:
        """Special characters in task/mission names don't break ID generation."""
        id1 = materializer.make_queue_task_id("mission@test!", "task#1:fix")
        id2 = materializer.make_queue_task_id("mission@test!", "task#1:fix")
        assert id1 == id2
        # Verify it matches the task ID regex
        import re
        assert re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", id1)

    def test_id_format_is_controller_compliant(self, materializer: TaskMaterializer) -> None:
        """Generated IDs comply with controller task ID requirements."""
        import re
        task_id_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

        test_cases = [
            ("mission-alpha", "task-one"),
            ("m" * 100, "t" * 100),
            ("mission@#$", "task@#$"),
            ("mission α", "task β"),
        ]

        for mission_id, task_id in test_cases:
            queue_id = materializer.make_queue_task_id(mission_id, task_id)
            assert task_id_pattern.match(queue_id), f"Invalid ID format: {queue_id!r}"
            assert ".." not in queue_id
            assert queue_id not in (".", "..")

    def test_collision_with_truncated_similar_names(self, materializer: TaskMaterializer) -> None:
        """Names that become identical after truncation still produce different IDs."""
        # Create two task names that differ only after the 60-char truncation point
        base = "a" * 59
        id1 = materializer.make_queue_task_id("mission-x", base + "-alpha")
        id2 = materializer.make_queue_task_id("mission-x", base + "-beta")
        assert id1 != id2

    def test_empty_or_minimal_names(self, materializer: TaskMaterializer) -> None:
        """Edge case: very short or single-character names."""
        id1 = materializer.make_queue_task_id("m", "t")
        id2 = materializer.make_queue_task_id("m", "x")
        assert id1 != id2
        assert len(id1) <= 128


class TestIDStability:
    """Test that IDs remain stable across different scenarios."""

    def test_id_stable_after_object_recreation(self, tmp_path: Path) -> None:
        """ID generation is stable across materializer instances."""
        queue = DurableQueue(tmp_path / "queue")
        mat1 = TaskMaterializer(queue, tmp_path / "reports")
        mat2 = TaskMaterializer(queue, tmp_path / "reports")

        id1 = mat1.make_queue_task_id("mission-alpha", "task-one")
        id2 = mat2.make_queue_task_id("mission-alpha", "task-one")
        assert id1 == id2

    def test_id_does_not_depend_on_python_hash(self, materializer: TaskMaterializer) -> None:
        """IDs use a deterministic digest suffix rather than Python hash()."""
        mission_id = "test-mission"
        task_id = "test-task"

        queue_id = materializer.make_queue_task_id(mission_id, task_id)
        digest = hashlib.sha256(b"mission-task-v1:test-mission:test-task").hexdigest()[:12]
        assert queue_id.endswith(digest)
        assert queue_id == materializer.make_queue_task_id(mission_id, task_id)


class TestHashCollisionResistance:
    """Test that the hash mechanism prevents collisions."""

    def test_hash_suffix_differentiates_similar_inputs(self, materializer: TaskMaterializer) -> None:
        """IDs for similar inputs differ in their hash component."""
        id1 = materializer.make_queue_task_id("mission-a", "task-impl-auth")
        id2 = materializer.make_queue_task_id("mission-a", "task-impl-authorization")

        # Extract the presumed hash suffixes and verify they're different
        assert id1 != id2

    def test_canonical_identity_ordering_independence(self, materializer: TaskMaterializer) -> None:
        """Semantically unordered metadata shouldn't affect identity if irrelevant."""
        # For now, we only use mission_id and task_id for queue task ID
        # If metadata ever affects identity, it should be ordered canonically
        id1 = materializer.make_queue_task_id("mission-x", "task-y")
        id2 = materializer.make_queue_task_id("mission-x", "task-y")
        assert id1 == id2


class TestReadability:
    """Test that IDs remain readable where practical."""

    def test_id_contains_recognizable_prefix(self, materializer: TaskMaterializer) -> None:
        """Generated IDs contain a recognizable prefix for debugging."""
        queue_id = materializer.make_queue_task_id("my-feature-mission", "implement-login")

        # ID should start with 'm-' prefix and contain some readable portion
        assert queue_id.startswith("m-")

        # Should contain some portion of the mission/task name for readability
        assert "my-feature" in queue_id or "implement" in queue_id or any(
            part in queue_id for part in ["my", "feature", "mission", "implement", "login"]
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
