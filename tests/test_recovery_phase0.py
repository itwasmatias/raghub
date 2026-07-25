from __future__ import annotations

import subprocess
import sys

from sports.application.production_runtime import (
    ProductionBasketballRuntime,
    build_production_runtime,
)
from sports.personal.config import PersonalEditionSettings


def test_web_ui_imports_in_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", "import web_ui"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_production_runtime_is_not_a_demo_runtime():
    from sports.application.nba_demo_runtime import BasketballDemoRuntime

    assert not issubclass(ProductionBasketballRuntime, BasketballDemoRuntime)
    assert not isinstance(build_production_runtime(), BasketballDemoRuntime)


def test_nba_is_the_default_and_supported_personal_sport():
    settings = PersonalEditionSettings.from_mapping(
        {
            "SIP_MODE": "live",
            "ODDS_FEED_ENABLED": "false",
        }
    )

    assert settings.odds_sports == ("basketball_nba",)
    assert PersonalEditionSettings.from_mapping(
        {
            "SIP_MODE": "live",
            "ODDS_FEED_ENABLED": "false",
            "ODDS_SPORTS": "basketball_nba,basketball_wnba,baseball_mlb",
        }
    ).odds_sports == (
        "basketball_nba",
        "basketball_wnba",
        "baseball_mlb",
    )
