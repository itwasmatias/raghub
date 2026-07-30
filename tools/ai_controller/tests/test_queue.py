from __future__ import annotations

import json
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.ai_controller.models import Task
from tools.ai_controller.queue import DurableQueue


def task(task_id="task-001"):
    return Task(
        id=task_id,
        title="Test task",
        prompt="Edit templates/sip_markets.html",
        base_ref="HEAD",
        tests=[],
    )


def _claim(queue_root, output):
    claimed = DurableQueue(Path(queue_root)).claim_next()
    output.put(claimed.id if claimed else None)


def test_task_moves_atomically_through_success(tmp_path):
    queue = DurableQueue(tmp_path / "queue")
    queue.enqueue(task())

    claimed = queue.claim_next()
    assert claimed.id == "task-001"
    assert not (queue.pending / "task-001.json").exists()
    assert (queue.running / "task-001.json").exists()

    queue.finish(claimed, succeeded=True)
    assert not (queue.running / "task-001.json").exists()
    assert (queue.succeeded / "task-001.json").exists()


def test_task_moves_atomically_to_failure(tmp_path):
    queue = DurableQueue(tmp_path / "queue")
    queue.enqueue(task())
    claimed = queue.claim_next()
    queue.finish(claimed, succeeded=False)
    assert (queue.failed / "task-001.json").exists()


def test_two_controllers_cannot_claim_same_task(tmp_path):
    queue = DurableQueue(tmp_path / "queue")
    queue.enqueue(task())
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    workers = [
        context.Process(target=_claim, args=(str(queue.root), output))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)

    results = sorted([output.get(timeout=2), output.get(timeout=2)], key=str)
    assert results.count("task-001") == 1
    assert results.count(None) == 1


def test_orphaned_running_task_is_recovered_without_mutation(tmp_path):
    queue = DurableQueue(tmp_path / "queue")
    original = task().to_dict()
    queue.enqueue(Task.from_dict(original))
    claimed = queue.claim_next()
    running = queue.running / f"{claimed.id}.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
    running.touch()
    running.chmod(0o600)
    import os

    os.utime(running, (old, old))

    recovered = queue.recover_orphans(older_than_seconds=60)

    assert recovered == ["task-001"]
    assert json.loads((queue.pending / "task-001.json").read_text()) == original


def test_corrupt_task_is_quarantined(tmp_path):
    queue = DurableQueue(tmp_path / "queue")
    (queue.pending / "bad.json").write_text("{broken", encoding="utf-8")

    assert queue.claim_next() is None
    assert (queue.invalid / "bad.json").exists()
