"""
Edge case regression tests for mission ID generation and path safety.

Tests cover:
- Empty or minimal sanitized values
- Path separator injection
- Dot-only identifiers
- Null byte rejection
- Very long inputs
- Unicode normalization
- Control characters
- Collision resistance verification
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tools.ai_controller.mission import make_legacy_queue_task_id
from tools.ai_controller.mission.materializer import TaskMaterializer
from tools.ai_controller.queue import DurableQueue


@pytest.fixture
def temp_queue(tmp_path: Path) -> DurableQueue:
    return DurableQueue(tmp_path / "queue")


@pytest.fixture
def materializer(temp_queue: DurableQueue, tmp_path: Path) -> TaskMaterializer:
    return TaskMaterializer(temp_queue, tmp_path / "reports")


class TestPathSafety:
    """Verify queue IDs reject path traversal and unsafe characters."""

    def test_rejects_null_bytes_in_mission_id(self, materializer: TaskMaterializer):
        """Null bytes in mission_id should be sanitized."""
        mission_id = "mission\x00evil"
        task_id = "task-safe"

        queue_id = materializer.make_queue_task_id(mission_id, task_id)

        # Null bytes should be sanitized to safe characters
        assert "\x00" not in queue_id
        assert ".." not in queue_id
        assert "/" not in queue_id
        assert "\\" not in queue_id

    def test_rejects_null_bytes_in_task_id(self, materializer: TaskMaterializer):
        """Null bytes in task_id should be sanitized."""
        mission_id = "mission-safe"
        task_id = "task\x00evil"

        queue_id = materializer.make_queue_task_id(mission_id, task_id)

        assert "\x00" not in queue_id
        assert ".." not in queue_id

    def test_path_separators_are_sanitized(self, materializer: TaskMaterializer):
        """Path separators should be replaced with safe characters."""
        test_cases = [
            ("mission/path/traversal", "task-safe"),
            ("mission-safe", "task\\windows\\path"),
            ("mission/../escape", "task-safe"),
            ("mission-safe", "task/../../escape"),
        ]

        for mission_id, task_id in test_cases:
            queue_id = materializer.make_queue_task_id(mission_id, task_id)

            # No path separators allowed
            assert "/" not in queue_id
            assert "\\" not in queue_id
            assert ".." not in queue_id

            # Must start with m-
            assert queue_id.startswith("m-")

            # Must not be just dots
            assert queue_id not in (".", "..")

    def test_dot_only_identifiers(self, materializer: TaskMaterializer):
        """Dot-only mission/task IDs should produce safe queue IDs."""
        test_cases = [
            (".", "task"),
            ("..", "task"),
            ("...", "task"),
            ("mission", "."),
            ("mission", ".."),
            (".", "."),
            ("..", ".."),
        ]

        for mission_id, task_id in test_cases:
            queue_id = materializer.make_queue_task_id(mission_id, task_id)

            # Result must not be a relative path
            assert queue_id not in (".", "..")
            assert not queue_id.startswith("../")
            assert "/.." not in queue_id

            # Must be path-safe
            assert queue_id.startswith("m-")

    def test_control_characters_sanitized(self, materializer: TaskMaterializer):
        """Control characters should be sanitized."""
        # Various control characters
        mission_id = "mission\t\n\r\x0b\x0c"
        task_id = "task\x01\x02\x03"

        queue_id = materializer.make_queue_task_id(mission_id, task_id)

        # No control characters in result
        for char in queue_id:
            assert ord(char) >= 32 or char in ("-", "_", ".")


class TestEmptyAndMinimal:
    """Test behavior with empty or minimal inputs."""

    def test_empty_mission_id(self, materializer: TaskMaterializer):
        """Empty mission_id should still produce valid queue ID."""
        queue_id = materializer.make_queue_task_id("", "task-one")

        # Must still be valid
        assert queue_id.startswith("m-")
        assert len(queue_id) <= 128
        # Hash suffix should still be present (deterministic for empty string)
        assert len(queue_id) > 10  # At least "m-" + some content

    def test_empty_task_id(self, materializer: TaskMaterializer):
        """Empty task_id should still produce valid queue ID."""
        queue_id = materializer.make_queue_task_id("mission-one", "")

        assert queue_id.startswith("m-")
        assert len(queue_id) <= 128

    def test_both_empty(self, materializer: TaskMaterializer):
        """Both empty should still be valid and deterministic."""
        id1 = materializer.make_queue_task_id("", "")
        id2 = materializer.make_queue_task_id("", "")

        assert id1 == id2
        assert id1.startswith("m-")

    def test_single_character_inputs(self, materializer: TaskMaterializer):
        """Single-character mission/task should work."""
        queue_id = materializer.make_queue_task_id("a", "b")

        assert queue_id.startswith("m-")
        assert len(queue_id) <= 128
        # Should be deterministic
        assert queue_id == materializer.make_queue_task_id("a", "b")

    def test_all_spaces_mission_id(self, materializer: TaskMaterializer):
        """Mission ID of all spaces should be sanitized."""
        queue_id = materializer.make_queue_task_id("   ", "task")

        # Spaces should be replaced with hyphens
        assert queue_id.startswith("m-")
        assert "   " not in queue_id


class TestVeryLongInputs:
    """Test behavior with very long mission/task IDs."""

    def test_very_long_mission_id(self, materializer: TaskMaterializer):
        """Very long mission ID should be truncated safely."""
        long_mission = "mission-" * 50  # ~400 chars
        task_id = "task"

        queue_id = materializer.make_queue_task_id(long_mission, task_id)

        # Must fit in 128 chars
        assert len(queue_id) <= 128
        assert queue_id.startswith("m-")

        # Should be deterministic
        queue_id2 = materializer.make_queue_task_id(long_mission, task_id)
        assert queue_id == queue_id2

    def test_very_long_task_id(self, materializer: TaskMaterializer):
        """Very long task ID should be truncated safely."""
        mission_id = "mission"
        long_task = "task-" * 50  # ~300 chars

        queue_id = materializer.make_queue_task_id(mission_id, long_task)

        assert len(queue_id) <= 128
        assert queue_id.startswith("m-")

    def test_both_very_long(self, materializer: TaskMaterializer):
        """Both very long should still produce valid ID."""
        long_mission = "m" * 200
        long_task = "t" * 200

        queue_id = materializer.make_queue_task_id(long_mission, long_task)

        assert len(queue_id) <= 128
        assert queue_id.startswith("m-")

        # Deterministic
        assert queue_id == materializer.make_queue_task_id(long_mission, long_task)


class TestUnicodeHandling:
    """Test Unicode normalization and handling."""

    def test_unicode_mission_id(self, materializer: TaskMaterializer):
        """Unicode in mission_id should be handled deterministically."""
        mission_id = "mission-α-β-γ"
        task_id = "task"

        id1 = materializer.make_queue_task_id(mission_id, task_id)
        id2 = materializer.make_queue_task_id(mission_id, task_id)

        assert id1 == id2
        assert id1.startswith("m-")

    def test_unicode_task_id(self, materializer: TaskMaterializer):
        """Unicode in task_id should be handled deterministically."""
        mission_id = "mission"
        task_id = "задача-中文-日本語"

        id1 = materializer.make_queue_task_id(mission_id, task_id)
        id2 = materializer.make_queue_task_id(mission_id, task_id)

        assert id1 == id2

    def test_emoji_in_identifiers(self, materializer: TaskMaterializer):
        """Emoji should be handled (sanitized to safe chars)."""
        mission_id = "mission-🚀-rocket"
        task_id = "task-✨-sparkle"

        queue_id = materializer.make_queue_task_id(mission_id, task_id)

        # Should still be valid
        assert queue_id.startswith("m-")
        assert len(queue_id) <= 128

    def test_combining_characters(self, materializer: TaskMaterializer):
        """Combining Unicode characters should work."""
        mission_id = "café"  # é = e + combining accent
        task_id = "naïve"

        id1 = materializer.make_queue_task_id(mission_id, task_id)
        id2 = materializer.make_queue_task_id(mission_id, task_id)

        assert id1 == id2


class TestCollisionResistance:
    """Verify hash suffix provides collision resistance."""

    def test_similar_prefixes_different_hashes(self, materializer: TaskMaterializer):
        """Similar prefixes but different full names should have different hashes."""
        # These would collide if only using truncated prefixes
        id1 = materializer.make_queue_task_id("mission-x", "implement-authentication")
        id2 = materializer.make_queue_task_id("mission-x", "implement-authorization")

        assert id1 != id2

        # Extract hash suffixes
        hash1 = id1.split("-")[-1]
        hash2 = id2.split("-")[-1]

        assert hash1 != hash2
        assert len(hash1) == 12  # 48 bits
        assert len(hash2) == 12

    def test_transposed_mission_task(self, materializer: TaskMaterializer):
        """Transposing mission and task should produce different IDs."""
        id1 = materializer.make_queue_task_id("foo", "bar")
        id2 = materializer.make_queue_task_id("bar", "foo")

        assert id1 != id2

    def test_canonical_input_format_versioned(self, materializer: TaskMaterializer):
        """The canonical input should include version prefix."""
        queue_id = materializer.make_queue_task_id("mission-test", "task-test")

        # Hash should be based on "mission-task-v1:mission-test:task-test"
        expected_hash = hashlib.sha256(
            b"mission-task-v1:mission-test:task-test"
        ).hexdigest()[:12]

        assert expected_hash in queue_id


class TestLegacyFormatEdgeCases:
    """Test legacy ID format with edge cases."""

    def test_legacy_empty_mission(self):
        """Legacy format with empty mission_id."""
        legacy_id = make_legacy_queue_task_id("", "task")

        assert legacy_id.startswith("m-")
        assert len(legacy_id) <= 128

    def test_legacy_empty_task(self):
        """Legacy format with empty task_id."""
        legacy_id = make_legacy_queue_task_id("mission", "")

        assert legacy_id.startswith("m-")
        assert len(legacy_id) <= 128

    def test_legacy_path_separators(self):
        """Legacy format should sanitize path separators."""
        legacy_id = make_legacy_queue_task_id("mission/path", "task\\path")

        assert "/" not in legacy_id
        assert "\\" not in legacy_id

    def test_legacy_exact_truncation_lengths(self):
        """Legacy format should use exact 40/60 truncation."""
        # Mission exactly 40 chars
        mission_40 = "a" * 40
        # Task exactly 60 chars
        task_60 = "b" * 60

        legacy_id = make_legacy_queue_task_id(mission_40, task_60)

        # Format: m-{40 chars}-{60 chars} = 103 chars total
        assert len(legacy_id) == 103

        # Longer should be truncated
        mission_50 = "a" * 50
        task_70 = "b" * 70

        legacy_id_long = make_legacy_queue_task_id(mission_50, task_70)

        # Should be same as exact-length version
        assert legacy_id == legacy_id_long


class TestDeterminismAcrossInstances:
    """Verify IDs are deterministic across materializer instances."""

    def test_id_stable_across_materializer_instances(self, tmp_path: Path):
        """Same inputs produce same ID across different materializer instances."""
        queue1 = DurableQueue(tmp_path / "queue1")
        queue2 = DurableQueue(tmp_path / "queue2")

        mat1 = TaskMaterializer(queue1, tmp_path / "reports1")
        mat2 = TaskMaterializer(queue2, tmp_path / "reports2")

        id1 = mat1.make_queue_task_id("mission-stable", "task-stable")
        id2 = mat2.make_queue_task_id("mission-stable", "task-stable")

        assert id1 == id2

    def test_legacy_id_stable_across_calls(self):
        """Legacy ID generation is consistent."""
        id1 = make_legacy_queue_task_id("mission-legacy", "task-legacy")
        id2 = make_legacy_queue_task_id("mission-legacy", "task-legacy")

        assert id1 == id2


class TestMetadataValidation:
    """Test that queue task metadata is preserved correctly."""

    def test_metadata_identity_preserved(self, materializer: TaskMaterializer, temp_queue: DurableQueue):
        """Queue task metadata should preserve mission and task identity."""
        from tools.ai_controller.mission.models import (
            MissionState,
            MissionStatus,
            MissionTaskDefinition,
        )
        from tools.ai_controller.models import Task

        mission_id = "mission-metadata"
        task_id = "task-metadata"

        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Test Task",
            prompt="Do work",
        )

        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},
        )

        # Materialize
        queue_id = materializer.materialize(mission_id, task_def, mission_state)

        # Read back the queue task
        queue_file = temp_queue.pending / f"{queue_id}.json"
        assert queue_file.exists()

        import json
        task_data = json.loads(queue_file.read_text())

        # Verify metadata
        assert task_data["metadata"]["mission_id"] == mission_id
        assert task_data["metadata"]["mission_task_id"] == task_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
