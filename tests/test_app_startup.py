from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import app as app_module
import pytest

from sports.personal.config import PersonalEditionSettings


def _capture_main_application_run(settings: PersonalEditionSettings) -> dict[str, object]:
    personal = SimpleNamespace(settings=settings)
    with (
        patch.object(app_module, "load_dotenv"),
        patch.object(
            app_module.PersonalEditionService,
            "from_environment",
            return_value=personal,
        ),
        patch.object(app_module.app, "run") as run,
    ):
        app_module._run_main_application()

    run.assert_called_once()
    return run.call_args.kwargs


def test_main_application_honors_sip_host_wildcard_binding():
    with patch.dict(
        os.environ,
        {"ODDS_FEED_ENABLED": "false", "SIP_HOST": "0.0.0.0"},
        clear=True,
    ):
        settings = PersonalEditionSettings.from_environment()
        run_options = _capture_main_application_run(settings)

    assert run_options["host"] == "0.0.0.0"


def test_main_application_honors_existing_configured_host():
    settings = PersonalEditionSettings.from_mapping(
        {
            "ODDS_FEED_ENABLED": "false",
            "SIP_HOST": "192.168.50.12",
            "SIP_PORT": "6100",
        }
    )

    run_options = _capture_main_application_run(settings)

    assert run_options["host"] == "192.168.50.12"
    assert run_options["port"] == 6100


def test_controller_host_configuration_does_not_change_main_application_host():
    with patch.dict(
        os.environ,
        {
            "ODDS_FEED_ENABLED": "false",
            "SIP_HOST": "0.0.0.0",
            "RAGHUB_CONTROLLER_BIND_ADDRESS": "127.0.0.2",
            "RAGHUB_CONTROLLER_TAILSCALE_ADDRESS": "100.64.0.9",
        },
        clear=True,
    ):
        settings = PersonalEditionSettings.from_environment()
        run_options = _capture_main_application_run(settings)

    assert run_options["host"] == "0.0.0.0"


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.50.12", "100.64.0.10"],
)
def test_enabled_controller_validates_the_actual_shared_flask_bind(
    host: str,
):
    settings = PersonalEditionSettings.from_mapping(
        {
            "ODDS_FEED_ENABLED": "false",
            "SIP_HOST": host,
            "SIP_PORT": "6100",
        }
    )
    personal = SimpleNamespace(settings=settings)
    with (
        patch.dict(
            os.environ,
            {
                "RAGHUB_CONTROLLER_ENABLED": "true",
                "RAGHUB_CONTROLLER_TAILSCALE_ADDRESS": "100.64.0.9",
            },
            clear=False,
        ),
        patch.object(app_module, "load_dotenv"),
        patch.object(
            app_module.PersonalEditionService,
            "from_environment",
            return_value=personal,
        ),
        patch.object(app_module.app, "run") as run,
    ):
        with pytest.raises(ValueError):
            app_module._run_main_application()
    run.assert_not_called()


def test_enabled_controller_accepts_exact_tailscale_shared_bind_and_port():
    settings = PersonalEditionSettings.from_mapping(
        {
            "ODDS_FEED_ENABLED": "false",
            "SIP_HOST": "100.64.0.9",
            "SIP_PORT": "6100",
        }
    )
    with patch.dict(
        os.environ,
        {
            "RAGHUB_CONTROLLER_ENABLED": "true",
            "RAGHUB_CONTROLLER_TAILSCALE_ADDRESS": "100.64.0.9",
        },
        clear=False,
    ):
        run_options = _capture_main_application_run(settings)

    assert run_options["host"] == "100.64.0.9"
    assert run_options["port"] == 6100
