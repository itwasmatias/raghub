from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sys

import pytest

from sports.personal.config import PersonalEditionSettings
from sports.personal.models import (
    DataMode,
    FeedHealth,
    MoneylineForecast,
    ModelMetadata,
)
from sports.personal.normalization import MoneylineNormalizer
from sports.personal.qualification import (
    QualificationReason,
    MoneylineQualificationService,
)
from sports.personal.repository import PersonalEditionRepository
from sports.personal.service import PersonalEditionService
from sports.data.sources.sports_game_odds_moneyline_source import (
    SportsGameOddsMoneylineSource,
)
from sports.personal.team_history import (
    PregameTeamFeatureBuilder,
    PublicTeamHistorySource,
    TeamGame,
)
from sports.personal.model import BaselineMoneylineModel


NOW = datetime(2026, 7, 25, 0, tzinfo=timezone.utc)


def _environment(**overrides):
    values = {
        "SIP_MODE": "live",
        "ODDS_FEED_ENABLED": "true",
        "ODDS_PROVIDERS": "odds_api_io,sportsgameodds",
        "ODDS_PROVIDER_PRIMARY": "odds_api_io",
        "ODDS_API_IO_API_KEY": "test-key",
        "SPORTSGAMEODDS_API_KEY": "sgo-test-key",
        "ODDS_API_KEY": "",
        "ODDS_REGIONS": "us",
        "ODDS_SPORTS": "basketball_wnba,baseball_mlb",
        "ODDS_MARKETS": "h2h",
        "ODDS_MAX_AGE_SECONDS": "600",
        "MIN_MODEL_EDGE": "0.03",
        "MIN_DATA_QUALITY": "0.75",
        "MIN_COMPLETE_BOOKS": "2",
        "FORECAST_MAX_AGE_SECONDS": "3600",
    }
    values.update(overrides)
    return values


def _rows(*, books=("DraftKings", "FanDuel"), age=30, period="full_game"):
    rows = []
    for index, book in enumerate(books):
        for selection, team, price in (
            ("home", "New York Liberty", -130 - index * 5),
            ("away", "Chicago Sky", 110 + index * 5),
        ):
            rows.append(
                {
                    "provider_event_id": f"provider-{index}",
                    "league": "WNBA",
                    "season": "2026",
                    "event_start": (NOW + timedelta(hours=3)).isoformat(),
                    "home_team": "NY Liberty",
                    "away_team": "Chicago Sky",
                    "sportsbook": book,
                    "market": "h2h",
                    "period": period,
                    "selection": selection,
                    "selection_team": team,
                    "american_price": price,
                    "observed_at": (NOW - timedelta(seconds=age)).isoformat(),
                    "source": "fixture-contract",
                    "source_url": "https://example.test/odds",
                    "data_mode": "live",
                    "is_live": False,
                }
            )
    return rows


def _forecast(event_id, selection="home", *, calibrated=True, age=60):
    metadata = ModelMetadata(
        model_name="sip-moneyline-baseline",
        version="1.0.0",
        training_date="2026-07-24T00:00:00+00:00",
        training_period=("2024-01-01", "2026-06-30"),
        validation_period=("2026-07-01", "2026-07-20"),
        feature_version="moneyline-v1",
        training_examples=500,
        calibration_method="platt",
        brier_score=0.21,
        log_loss=0.62,
        calibration_status="calibrated" if calibrated else "not_calibrated",
    )
    return MoneylineForecast(
        canonical_event_id=event_id,
        league="WNBA",
        market="moneyline",
        period="full_game",
        selection=selection,
        raw_probability=0.62,
        calibrated_probability=0.60,
        model_version="1.0.0",
        feature_version="moneyline-v1",
        feature_timestamp=(NOW - timedelta(minutes=10)).isoformat(),
        forecast_timestamp=(NOW - timedelta(seconds=age)).isoformat(),
        calibration_status=metadata.calibration_status,
        contributing_factors=("home advantage", "recent form"),
        missing_feature_warnings=(),
        metadata=metadata,
    )


def test_configuration_requires_key_only_for_enabled_live_feed():
    with pytest.raises(ValueError, match="provider key"):
        PersonalEditionSettings.from_mapping(
            _environment(ODDS_API_IO_API_KEY="", SPORTSGAMEODDS_API_KEY="")
        )
    disabled = PersonalEditionSettings.from_mapping(
        _environment(
            ODDS_FEED_ENABLED="false",
            ODDS_API_IO_API_KEY="",
            SPORTSGAMEODDS_API_KEY="",
        )
    )
    assert disabled.odds_feed_enabled is False
    replay = PersonalEditionSettings.from_mapping(
        _environment(
            SIP_MODE="replay",
            ODDS_API_IO_API_KEY="",
            SPORTSGAMEODDS_API_KEY="",
        )
    )
    assert replay.mode is DataMode.REPLAY


def test_sportsgameodds_uses_shared_backend_configuration_only():
    settings = PersonalEditionSettings.from_mapping(
        _environment(
            ODDS_PROVIDERS="sportsgameodds",
            ODDS_PROVIDER_PRIMARY="sportsgameodds",
            SPORTSGAMEODDS_API_KEY="shared-secret",
            ODDS_BASE_URL="https://api.sportsgameodds.com/v2",
            ODDS_REQUEST_TIMEOUT_SECONDS="15",
            ODDS_MAX_EVENTS_PER_REQUEST="10",
            ODDS_MAX_QUOTE_AGE_SECONDS="600",
            ODDS_REFRESH_MINUTES="10",
        )
    )

    assert settings.odds_provider_primary == "sportsgameodds"
    assert settings.sportsgameodds_api_key == "shared-secret"
    assert settings.odds_base_url == "https://api.sportsgameodds.com/v2"
    assert settings.odds_request_timeout_seconds == 15
    assert settings.odds_max_events_per_request == 10
    assert settings.odds_max_age_seconds == 600
    assert settings.refresh_interval_seconds == 600
    assert "shared-secret" not in repr(settings)


def test_legacy_odds_api_key_maps_to_sportsgameodds_only():
    settings = PersonalEditionSettings.from_mapping(
        _environment(
            ODDS_PROVIDERS="sportsgameodds",
            ODDS_PROVIDER_PRIMARY="sportsgameodds",
            SPORTSGAMEODDS_API_KEY="",
            ODDS_API_KEY="legacy-secret",
        )
    )

    assert settings.sportsgameodds_api_key == "legacy-secret"
    assert settings.odds_api_io_api_key == "test-key"
    assert settings.legacy_odds_api_key_deprecated is True
    assert "legacy-secret" not in repr(settings)


def test_the_odds_api_enforces_conservative_refresh_floor():
    settings = PersonalEditionSettings.from_mapping(
        _environment(
            ODDS_PROVIDER_PRIMARY="odds_api_io",
            ODDS_REFRESH_MINUTES="1",
        )
    )

    assert settings.refresh_interval_seconds == 600


def test_missing_sportsgameodds_key_becomes_structured_service_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SIP_MODE", "live")
    monkeypatch.setenv("ODDS_FEED_ENABLED", "true")
    monkeypatch.setenv("ODDS_PROVIDERS", "sportsgameodds")
    monkeypatch.setenv("ODDS_PROVIDER_PRIMARY", "sportsgameodds")
    monkeypatch.setenv("ODDS_API_IO_API_KEY", "")
    monkeypatch.setenv("SPORTSGAMEODDS_API_KEY", "")
    monkeypatch.setenv("ODDS_API_KEY", "")
    monkeypatch.setenv("SIP_PERSONAL_DATABASE", str(tmp_path / "sip.db"))

    service = PersonalEditionService.from_environment()
    result = service.refresh()

    assert result["status"] == "unconfigured"
    assert result["health"]["configured"] is False
    assert "provider key" in result["health"]["error"]


def test_sportsgameodds_registration_and_status_never_serialize_key(tmp_path):
    settings = PersonalEditionSettings.from_mapping(
        _environment(
            ODDS_PROVIDERS="sportsgameodds",
            ODDS_PROVIDER_PRIMARY="sportsgameodds",
            SPORTSGAMEODDS_API_KEY="backend-only-secret",
            SIP_PERSONAL_DATABASE=str(tmp_path / "sip.db"),
            SIP_MODEL_PATH=str(tmp_path / "model.json"),
        )
    )

    service = PersonalEditionService(settings)
    status_text = str(service.snapshot())

    assert isinstance(service.source, SportsGameOddsMoneylineSource)
    assert "backend-only-secret" not in status_text
    assert "odds_api_key" not in status_text


def test_configuration_rejects_invalid_thresholds():
    with pytest.raises(ValueError, match="MIN_MODEL_EDGE"):
        PersonalEditionSettings.from_mapping(_environment(MIN_MODEL_EDGE="1.2"))


def test_normalization_pairs_distinct_provider_ids_and_complete_books():
    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(_rows(), as_of=NOW)
    assert len(result.events) == 1
    assert result.events[0].canonical_id.startswith("wnba:20260725:")
    assert len(result.complete_books) == 2
    assert {item.sportsbook for item in result.complete_books} == {
        "draftkings",
        "fanduel",
    }


def test_normalization_rejects_stale_malformed_and_wrong_period_quotes():
    rows = _rows(age=700)
    rows.append({**_rows()[0], "american_price": 0})
    rows.extend(_rows(period="first_half"))
    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)
    assert not result.complete_books
    assert any(item.code == "STALE_QUOTE" for item in result.rejections)
    assert any(item.code == "INVALID_PRICE" for item in result.rejections)
    assert any(item.code == "UNSUPPORTED_PERIOD" for item in result.rejections)


def test_qualification_returns_exact_no_bet_diagnostics():
    normalized = MoneylineNormalizer(maximum_age_seconds=600).normalize(
        _rows(books=("DraftKings",)), as_of=NOW
    )
    event = normalized.events[0]
    result = MoneylineQualificationService(
        minimum_complete_books=2,
        minimum_edge=0.03,
        minimum_data_quality=0.75,
        forecast_max_age_seconds=3600,
    ).evaluate(
        event=event,
        complete_books=normalized.complete_books,
        forecasts=[_forecast(event.canonical_id)],
        feed_health=FeedHealth.configured_success(NOW.isoformat(), 1, 1),
        as_of=NOW,
    )
    assert result.status == "NO_BET"
    assert QualificationReason.INSUFFICIENT_COMPLETE_BOOKS in result.reason_codes
    assert result.diagnostics["complete_books"]["actual"] == 1
    assert result.diagnostics["complete_books"]["required"] == 2
    assert result.diagnostics["next_actions"]


def test_qualification_rejects_uncalibrated_mismatched_and_stale_forecasts():
    normalized = MoneylineNormalizer(maximum_age_seconds=600).normalize(
        _rows(), as_of=NOW
    )
    event = normalized.events[0]
    service = MoneylineQualificationService()
    health = FeedHealth.configured_success(NOW.isoformat(), 1, 2)

    uncalibrated = service.evaluate(
        event,
        normalized.complete_books,
        [_forecast(event.canonical_id, calibrated=False)],
        health,
        as_of=NOW,
    )
    mismatch = service.evaluate(
        event,
        normalized.complete_books,
        [_forecast("wnba:wrong:event")],
        health,
        as_of=NOW,
    )
    stale = service.evaluate(
        event,
        normalized.complete_books,
        [_forecast(event.canonical_id, age=7200)],
        health,
        as_of=NOW,
    )
    wrong_market = service.evaluate(
        event,
        normalized.complete_books,
        [replace(_forecast(event.canonical_id), market="spread")],
        health,
        as_of=NOW,
    )
    weak_calibration = _forecast(event.canonical_id)
    weak_calibration = replace(
        weak_calibration,
        metadata=replace(weak_calibration.metadata, brier_score=0.26),
    )
    weak = service.evaluate(
        event, normalized.complete_books, [weak_calibration], health, as_of=NOW
    )

    assert QualificationReason.FORECAST_NOT_CALIBRATED in uncalibrated.reason_codes
    assert QualificationReason.FORECAST_MISSING in mismatch.reason_codes
    assert QualificationReason.FORECAST_STALE in stale.reason_codes
    assert QualificationReason.FORECAST_MARKET_MISMATCH in wrong_market.reason_codes
    assert QualificationReason.FORECAST_NOT_CALIBRATED in weak.reason_codes


def test_fully_qualified_choice_has_reproducible_value_math():
    normalized = MoneylineNormalizer(maximum_age_seconds=600).normalize(
        _rows(), as_of=NOW
    )
    event = normalized.events[0]
    result = MoneylineQualificationService().evaluate(
        event,
        normalized.complete_books,
        [_forecast(event.canonical_id)],
        FeedHealth.configured_success(NOW.isoformat(), 1, 2),
        as_of=NOW,
    )
    assert result.status == "QUALIFIED"
    assert result.qualified is True
    assert result.best_sportsbook in {"draftkings", "fanduel"}
    assert result.model_probability == pytest.approx(0.60)
    assert result.market_probability is not None
    assert result.edge == pytest.approx(
        result.model_probability - result.market_probability
    )
    assert result.expected_value is not None


def test_clean_migration_and_disabled_live_mode_never_returns_fixtures(tmp_path):
    settings = PersonalEditionSettings.from_mapping(
        _environment(
            ODDS_FEED_ENABLED="false",
            ODDS_API_IO_API_KEY="",
            SPORTSGAMEODDS_API_KEY="",
            SIP_PERSONAL_DATABASE=str(tmp_path / "sip.db"),
            SIP_MODEL_PATH=str(tmp_path / "missing-model.json"),
        )
    )
    repository = PersonalEditionRepository(settings.database_path)
    assert repository.migrate() == [1, 2]
    assert repository.migrate() == []
    service = PersonalEditionService(settings, repository=repository, clock=lambda: NOW)

    refresh = service.refresh()
    snapshot = service.snapshot()

    assert refresh["status"] == "unconfigured"
    assert snapshot["events"] == []
    assert snapshot["qualified_choices"] == []
    assert snapshot["feed"]["status"] == "unconfigured"
    assert snapshot["model"]["status"] == "MODEL_NOT_TRAINED"


def test_refresh_persists_real_normalized_quotes_and_specific_no_bet(tmp_path):
    class Source:
        last_quota = {"remaining": 450, "used": 50, "last": 2}
        last_errors = {}

        def fetch(self, **_kwargs):
            return _rows()

    settings = PersonalEditionSettings.from_mapping(
        _environment(
            SIP_PERSONAL_DATABASE=str(tmp_path / "sip.db"),
            SIP_MODEL_PATH=str(tmp_path / "missing-model.json"),
        )
    )
    service = PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        sources={"odds_api_io": Source()},
        clock=lambda: NOW,
    )

    refresh = service.refresh()
    snapshot = service.snapshot()

    assert refresh["status"] == "healthy"
    assert refresh["events"] == 1
    assert refresh["complete_books"] == 2
    assert refresh["distinct_sportsbooks"] == 2
    assert refresh["provider_health"]["odds_api_io"]["quotes"] == 4
    assert len(snapshot["events"]) == 1
    assert len(snapshot["events"][0]["quotes"]) == 4
    assert snapshot["feed"]["provider"] == "odds_api_io"
    assert snapshot["feed"]["quota_remaining"] == 450
    assert snapshot["feed"]["quota_used"] == 50
    assert snapshot["feed"]["quota_last"] == 2
    assert len(snapshot["no_bet_results"]) == 2
    assert all(
        "FORECAST_MISSING" in item["reason_codes"]
        for item in snapshot["no_bet_results"]
    )
    assert snapshot["qualified_choices"] == []


def test_refresh_reports_separate_provider_health_and_merges_on_canonical_event(
    tmp_path,
):
    class PrimarySource:
        last_quota = {"remaining": 1000, "used": 12, "last": 2}
        last_errors = {}

        def fetch(self, **_kwargs):
            return _rows(books=("DraftKings",))

    class SecondarySource:
        last_quota = {}
        last_errors = {"WNBA": "subscription tier unavailable"}

        def fetch(self, **_kwargs):
            return _rows(books=("FanDuel",))

    settings = PersonalEditionSettings.from_mapping(
        _environment(
            SIP_PERSONAL_DATABASE=str(tmp_path / "sip.db"),
            SIP_MODEL_PATH=str(tmp_path / "missing-model.json"),
            ODDS_PROVIDERS="odds_api_io,sportsgameodds",
            ODDS_PROVIDER_PRIMARY="odds_api_io",
        )
    )
    service = PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        sources={
            "odds_api_io": PrimarySource(),
            "sportsgameodds": SecondarySource(),
        },
        clock=lambda: NOW,
    )

    refresh = service.refresh()

    assert refresh["status"] == "healthy"
    assert refresh["events"] == 1
    assert refresh["complete_books"] == 2
    assert refresh["distinct_sportsbooks"] == 2
    assert refresh["provider_health"]["odds_api_io"]["events"] == 1
    assert refresh["provider_health"]["odds_api_io"]["quotes"] == 2
    assert refresh["provider_health"]["sportsgameodds"]["events"] == 1
    assert refresh["provider_health"]["sportsgameodds"]["quotes"] == 2
    assert refresh["provider_health"]["sportsgameodds"]["errors"]


def test_refresh_generates_exact_event_forecasts_from_league_model_and_history(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    class Source:
        def fetch(self, **_kwargs):
            return _rows()

    games = []
    for index in range(80):
        start = NOW - timedelta(days=81 - index)
        home_wins = index % 3 != 0
        games.append(
            TeamGame(
                league="WNBA",
                event_id=f"history-{index}",
                start_time=start.isoformat(),
                season="2026",
                home_team="New York Liberty",
                away_team="Chicago Sky",
                home_score=90 if home_wins else 75,
                away_score=80 if home_wins else 85,
                neutral_site=False,
                source_url="https://example.test/history",
            )
        )
    settings = PersonalEditionSettings.from_mapping(
        _environment(
            SIP_PERSONAL_DATABASE=str(tmp_path / "sip.db"),
            SIP_MODEL_PATH=str(tmp_path / "models" / "moneyline-v1.json"),
        )
    )
    service = PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        sources={"odds_api_io": Source()},
        clock=lambda: NOW,
    )
    PublicTeamHistorySource.save(
        games,
        service.history_path_for_league("WNBA"),
    )
    model = BaselineMoneylineModel.train(
        PregameTeamFeatureBuilder.training_rows(games),
        trained_at=NOW,
    )
    model.save(service.model_path_for_league("WNBA"))

    refresh = service.refresh()

    assert refresh["forecasts_generated"] == 2
    forecasts = service.repository.list_forecasts()
    assert {item.canonical_event_id for item in forecasts} == {
        service.repository.list_events()[0].canonical_id
    }
    assert {item.selection for item in forecasts} == {"home", "away"}
    assert {item.model_version for item in forecasts} == {
        PersonalEditionService.WNBA_MODEL_VERSION
    }
    assert {item.metadata.version for item in forecasts} == {
        PersonalEditionService.WNBA_MODEL_VERSION
    }
    assert {item.metadata.model_name for item in forecasts} == {
        PersonalEditionService.WNBA_MODEL_VERSION
    }
    assert all(
        "FORECAST_MISSING" not in item["reason_codes"]
        for item in refresh["evaluations"]
    )


def test_completed_wnba_predictions_are_resolved_and_scored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    event_start = NOW - timedelta(hours=5)

    class Source:
        def fetch(self, **_kwargs):
            rows = _rows()
            for row in rows:
                row["event_start"] = event_start.isoformat()
            return rows

    class CompletedHistory:
        def fetch(self, league, seasons):
            assert league == "WNBA"
            assert 2026 in seasons
            return [
                TeamGame(
                    league="WNBA",
                    event_id="resolved-1",
                    start_time=event_start.isoformat(),
                    season="2026",
                    home_team="New York Liberty",
                    away_team="Chicago Sky",
                    home_score=88,
                    away_score=80,
                    neutral_site=False,
                    source_url="https://example.test/final",
                )
            ]

    training_games = []
    for index in range(90):
        start = NOW - timedelta(days=95 - index)
        home_wins = index % 4 != 0
        training_games.append(
            TeamGame(
                league="WNBA",
                event_id=f"history-{index}",
                start_time=start.isoformat(),
                season="2026",
                home_team="New York Liberty",
                away_team="Chicago Sky",
                home_score=91 if home_wins else 76,
                away_score=82 if home_wins else 87,
                neutral_site=False,
                source_url="https://example.test/history",
            )
        )

    settings = PersonalEditionSettings.from_mapping(
        _environment(
            SIP_PERSONAL_DATABASE=str(tmp_path / "sip.db"),
            SIP_MODEL_PATH=str(tmp_path / "models" / "moneyline-v1.json"),
        )
    )
    service = PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        sources={"odds_api_io": Source()},
        history_source=CompletedHistory(),
        clock=lambda: NOW,
    )

    PublicTeamHistorySource.save(
        training_games,
        service.history_path_for_league("WNBA"),
    )
    model = BaselineMoneylineModel.train(
        PregameTeamFeatureBuilder.training_rows(training_games),
        trained_at=NOW,
    )
    model.save(service.model_path_for_league("WNBA"))

    refresh = service.refresh()
    assert refresh["resolved_predictions"]["resolved_count"] == 2

    resolved = service.repository.list_resolved_predictions()
    assert len(resolved) == 2
    assert {item.winner_selection for item in resolved} == {"home"}
    assert {item.correct for item in resolved} == {True, False}
    assert all(item.brier_contribution >= 0 for item in resolved)
    assert all(item.log_loss_contribution >= 0 for item in resolved)
    assert {item.model_version for item in resolved} == {
        PersonalEditionService.WNBA_MODEL_VERSION
    }

    snapshot = service.snapshot()
    assert snapshot["wnba_performance"]["resolved_predictions"] == 2
    assert snapshot["league_summary"]["WNBA"]["resolved_predictions"] == 2


def test_fedora_startup_path_does_not_import_numpy_or_pandas(tmp_path):
    class Source:
        def fetch(self, **_kwargs):
            return _rows()

    settings = PersonalEditionSettings.from_mapping(
        _environment(
            SIP_PERSONAL_DATABASE=str(tmp_path / "sip.db"),
            SIP_MODEL_PATH=str(tmp_path / "missing-model.json"),
        )
    )
    service = PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        sources={"odds_api_io": Source()},
        clock=lambda: NOW,
    )
    service.refresh()

    assert "numpy" not in sys.modules
    assert "pandas" not in sys.modules
