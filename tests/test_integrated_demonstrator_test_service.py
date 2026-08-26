from __future__ import annotations

from http.client import RemoteDisconnected
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tools.integrated_demonstrator.test_service import (
    DemoServiceStore,
    create_server,
)


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(url: str, *, drop_response: bool = False):
    headers = {}
    if drop_response:
        headers["X-MissionaryX-Drop-Response"] = "1"

    request = Request(
        url,
        data=b"",
        method="POST",
        headers=headers,
    )
    return urlopen(request, timeout=2.0)


@pytest.fixture
def live_service(tmp_path):
    database_path = tmp_path / "external-service.sqlite3"
    server = create_server(database_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    try:
        yield base_url, database_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_initial_state_is_version_one_and_zero_operations(tmp_path):
    store = DemoServiceStore(tmp_path / "service.sqlite3")

    state = store.read_state()

    assert state.active_version == 1
    assert state.deployment_attempt_count == 0
    assert state.successful_transition_count == 0


def test_deploy_v2_changes_external_state_exactly_once(tmp_path):
    store = DemoServiceStore(tmp_path / "service.sqlite3")

    first, first_transitioned = store.deploy_v2()
    second, second_transitioned = store.deploy_v2()

    assert first_transitioned is True
    assert first.active_version == 2
    assert first.deployment_attempt_count == 1
    assert first.successful_transition_count == 1

    assert second_transitioned is False
    assert second.active_version == 2
    assert second.deployment_attempt_count == 2
    assert second.successful_transition_count == 1


def test_state_survives_store_reopen(tmp_path):
    database_path = tmp_path / "service.sqlite3"

    first_store = DemoServiceStore(database_path)
    first_store.deploy_v2()

    reopened = DemoServiceStore(database_path)
    state = reopened.read_state()

    assert state.active_version == 2
    assert state.deployment_attempt_count == 1
    assert state.successful_transition_count == 1


def test_http_state_and_deploy_endpoints(live_service):
    base_url, _ = live_service

    before = _get_json(f"{base_url}/state")
    assert before == {
        "active_version": 1,
        "deployment_attempt_count": 0,
        "successful_transition_count": 0,
    }

    with _post(f"{base_url}/deploy-v2") as response:
        result = json.loads(response.read().decode("utf-8"))

    assert result["transitioned"] is True
    assert result["active_version"] == 2
    assert result["deployment_attempt_count"] == 1
    assert result["successful_transition_count"] == 1

    after = _get_json(f"{base_url}/state")
    assert after["active_version"] == 2
    assert after["deployment_attempt_count"] == 1


def test_second_http_deployment_is_observable_as_duplicate_attempt(live_service):
    base_url, _ = live_service

    with _post(f"{base_url}/deploy-v2"):
        pass

    with pytest.raises(HTTPError) as exc_info:
        _post(f"{base_url}/deploy-v2")

    assert exc_info.value.code == 409

    state = _get_json(f"{base_url}/state")
    assert state["active_version"] == 2
    assert state["deployment_attempt_count"] == 2
    assert state["successful_transition_count"] == 1


def test_drop_response_commits_effect_before_confirmation_is_lost(live_service):
    base_url, database_path = live_service

    with pytest.raises(RemoteDisconnected):
        _post(
            f"{base_url}/deploy-v2",
            drop_response=True,
        )

    # Read using a fresh store instance to prove the committed external state
    # does not depend on the HTTP handler's in-memory objects.
    reopened = DemoServiceStore(database_path)
    state = reopened.read_state()

    assert state.active_version == 2
    assert state.deployment_attempt_count == 1
    assert state.successful_transition_count == 1


def test_service_refuses_non_loopback_binding(tmp_path):
    with pytest.raises(ValueError, match="127.0.0.1"):
        create_server(
            tmp_path / "service.sqlite3",
            host="0.0.0.0",
        )
