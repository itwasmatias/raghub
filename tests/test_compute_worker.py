from raghub_worker.worker import HANDLERS, WorkerSettings


def test_worker_settings_require_separate_server_and_token(monkeypatch):
    monkeypatch.delenv("RAGHUB_SERVER_URL", raising=False)
    monkeypatch.delenv("RAGHUB_WORKER_TOKEN", raising=False)
    try:
        WorkerSettings.from_environment()
    except ValueError as error:
        assert "RAGHUB_WORKER_TOKEN" in str(error)
    else:
        raise AssertionError("worker started without authentication settings")


def test_worker_executes_only_allow_listed_structured_operation():
    assert set(HANDLERS) == {"RUN_HEAVY_FEATURE_PIPELINE"}
    result = HANDLERS["RUN_HEAVY_FEATURE_PIPELINE"](
        {
            "job_type": "RUN_HEAVY_FEATURE_PIPELINE",
            "input_snapshot_id": "snapshot-1",
            "feature_version": "summary-v1",
            "payload": {
                "rows": [{"value": 1}, {"value": 2}, {"value": 3}],
                "numeric_fields": ["value"],
            },
        }
    )
    assert result["execution_node"] == "windows-worker"
    assert result["summaries"]["value"] == {
        "count": 3,
        "mean": 2.0,
        "minimum": 1.0,
        "maximum": 3.0,
    }
