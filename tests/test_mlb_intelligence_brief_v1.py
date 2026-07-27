from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from sports.personal.models import (
    CanonicalEvent,
    ModelMetadata,
    MoneylineForecast,
    NormalizedMoneylineQuote,
)


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _event(
    canonical_id: str,
    *,
    start_time: str,
    home_team_name: str,
    away_team_name: str,
) -> dict[str, object]:
    return {
        **asdict(
            CanonicalEvent(
                canonical_id=canonical_id,
                league="MLB",
                season="2026",
                start_time=start_time,
                home_team_id=f"mlb:{home_team_name.lower().replace(' ', '-')}",
                home_team_name=home_team_name,
                away_team_id=f"mlb:{away_team_name.lower().replace(' ', '-')}",
                away_team_name=away_team_name,
                venue="Example Park",
                status="pregame",
                provider_event_ids=("provider-a",),
                source_urls=("https://example.test/game",),
            )
        ),
        "complete_books": 2,
        "quotes": [],
        "evaluations": [],
        "forecasts": [],
        "season_context": {},
    }


def _quote(
    *,
    canonical_event_id: str,
    sportsbook: str,
    selection: str,
    american_price: int,
    observed_at: str,
    provider: str,
    provider_event_id: str,
    provider_quote_id: str,
    source_url: str,
) -> dict[str, object]:
    return asdict(
        NormalizedMoneylineQuote(
            canonical_event_id=canonical_event_id,
            provider_event_id=provider_event_id,
            league="MLB",
            season="2026",
            event_start="2026-07-27T00:10:00+00:00",
            home_team_id="mlb:new-york-yankees",
            away_team_id="mlb:los-angeles-dodgers",
            sportsbook=sportsbook,
            market="moneyline",
            period="full_game",
            selection=selection,
            selection_team_id=(
                "mlb:new-york-yankees"
                if selection == "home"
                else "mlb:los-angeles-dodgers"
            ),
            line=None,
            american_price=american_price,
            observed_at=observed_at,
            source=provider,
            source_url=source_url,
            data_mode="live",
            canonical_sportsbook_id=sportsbook,
            canonical_market_id=f"{canonical_event_id}:market:moneyline:full_game:v1",
            canonical_outcome_id=(
                f"{canonical_event_id}:market:moneyline:full_game:v1:outcome:{selection}:v1"
            ),
            provider_quote_id=provider_quote_id,
        )
    )


def _forecast(canonical_event_id: str, *, probability: float) -> dict[str, object]:
    return asdict(
        MoneylineForecast(
            canonical_event_id=canonical_event_id,
            league="MLB",
            market="moneyline",
            period="full_game",
            selection="away",
            raw_probability=probability,
            calibrated_probability=probability,
            model_version="mlb-exp-v1",
            feature_version="moneyline-v1",
            feature_timestamp=(NOW - timedelta(minutes=20)).isoformat(),
            forecast_timestamp=(NOW - timedelta(minutes=5)).isoformat(),
            calibration_status="experimental",
            contributing_factors=("bullpen fatigue",),
            missing_feature_warnings=(),
            metadata=ModelMetadata(
                model_name="mlb-experimental",
                version="mlb-exp-v1",
                training_date="2026-07-20T00:00:00+00:00",
                training_period=("2024-01-01", "2026-07-20"),
                validation_period=("2026-07-21", "2026-07-25"),
                feature_version="moneyline-v1",
                training_examples=1200,
                calibration_method="platt",
                brier_score=0.21,
                log_loss=0.62,
                calibration_status="experimental",
            ),
        )
    )


def _evaluation(
    canonical_event_id: str,
    *,
    qualified: bool,
    status: str,
    selection: str = "away",
    market_probability: float | None = 0.53,
    model_probability: float | None = 0.59,
    confidence: float | None = 0.68,
    data_quality: float = 0.82,
    evidence: tuple[str, ...] = ("bullpen-rest-edge",),
    risks: tuple[str, ...] = ("weather-volatility",),
    contradictions: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "canonical_event_id": canonical_event_id,
        "selection": selection,
        "qualified": qualified,
        "status": status,
        "reason_codes": [] if qualified else ["FORECAST_MISSING"],
        "diagnostics": {
            "contradictions": list(contradictions),
            "forecast": {"model_version": "mlb-exp-v1"},
        },
        "best_sportsbook": "draftkings",
        "best_price": 120,
        "consensus_price": 118,
        "raw_implied_probability": market_probability,
        "market_probability": market_probability,
        "model_probability": model_probability,
        "edge": (
            model_probability - market_probability
            if model_probability is not None and market_probability is not None
            else None
        ),
        "expected_value": 0.04 if qualified else None,
        "confidence": confidence,
        "data_quality": data_quality,
        "evaluated_at": NOW.isoformat(),
        "evidence": list(evidence),
        "risks": list(risks),
    }


def _snapshot() -> dict[str, object]:
    early_id = "mlb:game:v1:away:los-angeles-dodgers:home:new-york-yankees:instance:start:20260727T001000Z"
    late_id = "mlb:game:v1:away:atlanta-braves:home:new-york-mets:instance:start:20260727T021000Z"
    early_event = _event(
        early_id,
        start_time="2026-07-27T00:10:00+00:00",
        home_team_name="New York Yankees",
        away_team_name="Los Angeles Dodgers",
    )
    late_event = _event(
        late_id,
        start_time="2026-07-27T02:10:00+00:00",
        home_team_name="New York Mets",
        away_team_name="Atlanta Braves",
    )
    early_event["quotes"] = [
        _quote(
            canonical_event_id=early_id,
            sportsbook="draftkings",
            selection="away",
            american_price=120,
            observed_at=(NOW - timedelta(minutes=1)).isoformat(),
            provider="odds_api_io",
            provider_event_id="provider-a-101",
            provider_quote_id="quote:v1:early-away-dk",
            source_url="https://example.test/early/dk/away",
        ),
        _quote(
            canonical_event_id=early_id,
            sportsbook="draftkings",
            selection="home",
            american_price=-140,
            observed_at=(NOW - timedelta(minutes=1)).isoformat(),
            provider="odds_api_io",
            provider_event_id="provider-a-101",
            provider_quote_id="quote:v1:early-home-dk",
            source_url="https://example.test/early/dk/home",
        ),
        _quote(
            canonical_event_id=early_id,
            sportsbook="fanduel",
            selection="away",
            american_price=118,
            observed_at=(NOW - timedelta(minutes=2)).isoformat(),
            provider="sportsgameodds",
            provider_event_id="provider-b-888",
            provider_quote_id="quote:v1:early-away-fd",
            source_url="https://example.test/early/fd/away",
        ),
        _quote(
            canonical_event_id=early_id,
            sportsbook="fanduel",
            selection="home",
            american_price=-138,
            observed_at=(NOW - timedelta(minutes=2)).isoformat(),
            provider="sportsgameodds",
            provider_event_id="provider-b-888",
            provider_quote_id="quote:v1:early-home-fd",
            source_url="https://example.test/early/fd/home",
        ),
    ]
    early_event["forecasts"] = [_forecast(early_id, probability=0.59)]
    early_event["evaluations"] = [
        _evaluation(
            early_id,
            qualified=True,
            status="QUALIFIED",
            contradictions=("Pitching note conflicts with public lineup report",),
        )
    ]

    late_event["quotes"] = [
        _quote(
            canonical_event_id=late_id,
            sportsbook="draftkings",
            selection="away",
            american_price=110,
            observed_at=(NOW - timedelta(minutes=45)).isoformat(),
            provider="odds_api_io",
            provider_event_id="provider-c-301",
            provider_quote_id="quote:v1:late-away-dk",
            source_url="https://example.test/late/dk/away",
        ),
        _quote(
            canonical_event_id=late_id,
            sportsbook="draftkings",
            selection="home",
            american_price=-130,
            observed_at=(NOW - timedelta(minutes=45)).isoformat(),
            provider="odds_api_io",
            provider_event_id="provider-c-301",
            provider_quote_id="quote:v1:late-home-dk",
            source_url="https://example.test/late/dk/home",
        ),
    ]
    late_event["complete_books"] = 0
    late_event["evaluations"] = [
        _evaluation(
            late_id,
            qualified=False,
            status="NO_BET",
            model_probability=None,
            confidence=None,
            data_quality=0.41,
            evidence=(),
            risks=("market-thin", "lineup-uncertain"),
        )
    ]

    return {
        "generated_at": NOW.isoformat(),
        "feed": {
            "freshness": "fresh",
            "provider": "odds_api_io",
            "status": "healthy",
            "last_attempt_at": NOW.isoformat(),
            "last_successful_refresh": NOW.isoformat(),
        },
        "events": [late_event, early_event],
        "evaluations": early_event["evaluations"] + late_event["evaluations"],
        "resolved_predictions": [],
    }


def test_complete_game_renders_required_sections():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    markdown = MlbIntelligenceBriefService().build_markdown(_snapshot())

    assert "# MLB Intelligence Brief" in markdown
    assert "## Report Metadata" in markdown
    assert "## Slate Summary" in markdown
    assert "## Game Cards" in markdown
    assert "## Audit And Disclosure" in markdown
    assert "New York Yankees vs Los Angeles Dodgers" in markdown
    assert "Consensus no-vig market probability" in markdown
    assert "Experimental model probability" in markdown
    assert "Verdict" in markdown


def test_missing_model_probability_is_shown_as_unavailable():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    markdown = MlbIntelligenceBriefService().build_markdown(_snapshot())

    assert "Experimental model probability: Unavailable" in markdown


def test_stale_quotes_produce_a_warning():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    markdown = MlbIntelligenceBriefService(odds_max_age_seconds=600).build_markdown(
        _snapshot()
    )

    assert "Stale quote warning" in markdown
    assert "45.0 minutes old" in markdown


def test_contradictory_evidence_is_displayed():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    markdown = MlbIntelligenceBriefService().build_markdown(_snapshot())

    assert "Contradictions" in markdown
    assert "Pitching note conflicts with public lineup report" in markdown


def test_no_bet_avoid_status_is_preserved():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    markdown = MlbIntelligenceBriefService().build_markdown(_snapshot())

    assert "Verdict: Avoid" in markdown
    assert "No-bet status preserved from evaluation: NO_BET" in markdown


def test_source_timestamps_and_provenance_appear():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    markdown = MlbIntelligenceBriefService().build_markdown(_snapshot())

    assert "quote:v1:early-away-dk" in markdown
    assert "provider-a-101" in markdown
    assert "https://example.test/early/dk/away" in markdown
    assert NOW.isoformat() in markdown


def test_output_is_deterministic():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    service = MlbIntelligenceBriefService()
    first = service.build_markdown(_snapshot())
    second = service.build_markdown(_snapshot())

    assert first == second


def test_report_contains_no_guaranteed_win_claims():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    markdown = MlbIntelligenceBriefService().build_markdown(_snapshot()).lower()

    assert "guaranteed win" not in markdown
    assert "guaranteed pick" not in markdown
    assert "not a guarantee" in markdown
    assert "no automated execution" in markdown


def test_two_games_are_ordered_by_scheduled_start():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    markdown = MlbIntelligenceBriefService().build_markdown(_snapshot())

    early_index = markdown.index("New York Yankees vs Los Angeles Dodgers")
    late_index = markdown.index("New York Mets vs Atlanta Braves")
    assert early_index < late_index


def test_empty_slate_produces_valid_transparent_report():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    markdown = MlbIntelligenceBriefService().build_markdown(
        {
            "generated_at": NOW.isoformat(),
            "feed": {
                "freshness": "unavailable",
                "provider": "odds_api_io",
                "status": "unconfigured",
                "last_attempt_at": NOW.isoformat(),
                "last_successful_refresh": None,
            },
            "events": [],
            "evaluations": [],
            "resolved_predictions": [],
        }
    )

    assert "# MLB Intelligence Brief" in markdown
    assert "Games analyzed: 0" in markdown
    assert "No MLB games were available in the persisted snapshot." in markdown
    assert "Unavailable: no MLB events were available for this slate." in markdown


def test_html_export_contains_required_sections():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    html = MlbIntelligenceBriefService().build_html(_snapshot())

    assert "<html" in html.lower()
    assert "MLB Intelligence Brief" in html
    assert "Report Metadata" in html
    assert "Slate Summary" in html
    assert "Game Cards" in html
    assert "Audit and Disclosure" in html
    assert "This report is research and analysis" in html


def test_pdf_export_starts_with_pdf_header():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    pdf_bytes = MlbIntelligenceBriefService().build_pdf(_snapshot())

    assert pdf_bytes.startswith(b"%PDF-")
    assert b"MLB Intelligence Brief" in pdf_bytes


def test_delivery_package_writes_markdown_html_pdf_and_manifest(tmp_path):
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    package = MlbIntelligenceBriefService().create_delivery_package(
        snapshot=_snapshot(),
        output_dir=tmp_path,
        client_slug="pilot-client",
    )

    markdown_path = package["markdown_path"]
    html_path = package["html_path"]
    pdf_path = package["pdf_path"]
    manifest_path = package["manifest_path"]

    assert markdown_path.exists()
    assert html_path.exists()
    assert pdf_path.exists()
    assert manifest_path.exists()
    assert markdown_path.read_text(encoding="utf-8").startswith(
        "# MLB Intelligence Brief"
    )
    assert "<html" in html_path.read_text(encoding="utf-8").lower()
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    manifest = manifest_path.read_text(encoding="utf-8")
    assert "pilot-client" in manifest
    assert "methodology_version" in manifest


class _PersonalServiceDouble:
    """Static service double that returns a controlled snapshot."""

    def __init__(self, snapshot: dict[str, object]) -> None:
        self._snapshot = snapshot
        self.snapshot_call_count = 0

    def snapshot(self) -> dict[str, object]:
        self.snapshot_call_count += 1
        return self._snapshot


def _make_mlb_event(
    canonical_id: str,
    start_time: str,
    *,
    home: str = "Home Team",
    away: str = "Away Team",
) -> dict[str, object]:
    return {
        "canonical_id": canonical_id,
        "league": "MLB",
        "season": "2026",
        "start_time": start_time,
        "home_team_name": home,
        "away_team_name": away,
        "venue": "Test Park",
        "status": "pregame",
        "provider_event_ids": ("test-provider",),
        "source_urls": ("https://test.test/game",),
        "complete_books": 0,
        "quotes": [],
        "evaluations": [],
        "forecasts": [],
        "season_context": {},
    }


def _make_nba_event(
    canonical_id: str,
    start_time: str,
) -> dict[str, object]:
    return {
        "canonical_id": canonical_id,
        "league": "NBA",
        "season": "2026",
        "start_time": start_time,
        "home_team_name": "Home NBA",
        "away_team_name": "Away NBA",
        "venue": "Test Arena",
        "status": "pregame",
        "provider_event_ids": ("test-provider",),
        "source_urls": ("https://test.test/nba",),
        "complete_books": 0,
        "quotes": [],
        "evaluations": [],
        "forecasts": [],
        "season_context": {},
    }


def test_build_from_personal_service_preserves_matching_persisted_data():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    snapshot = _snapshot()
    original_snapshot = deepcopy(snapshot)
    double = _PersonalServiceDouble(snapshot)
    service = MlbIntelligenceBriefService()
    report = service.build_from_personal_service(double, "2026-07-27")

    assert double.snapshot_call_count == 1
    assert snapshot == original_snapshot
    assert report.slate_summary.games_analyzed == 2

    card = report.game_cards[0]
    quote = next(
        item
        for item in card.available_sportsbook_quotes
        if item.provider_quote_id == "quote:v1:early-away-dk"
    )
    assert card.canonical_game_id == (
        "mlb:game:v1:away:los-angeles-dodgers:home:new-york-yankees:"
        "instance:start:20260727T001000Z"
    )
    assert card.scheduled_start == "2026-07-27T00:10:00+00:00"
    assert quote.provider == "odds_api_io"
    assert quote.sportsbook == "draftkings"
    assert quote.provider_event_id == "provider-a-101"
    assert quote.provider_quote_id == "quote:v1:early-away-dk"
    assert quote.observed_at == (NOW - timedelta(minutes=1)).isoformat()
    assert quote.source_url == "https://example.test/early/dk/away"
    assert card.probabilities.market_probability == 0.53
    assert card.probabilities.model_probability == 0.59
    assert card.evidence_references == ("bullpen-rest-edge",)
    assert card.risk_flags == ("weather-volatility",)
    assert card.contradictions == (
        "Pitching note conflicts with public lineup report",
    )


def test_build_from_personal_service_filters_non_mlb_and_wrong_date_events():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    mlb_today = _make_mlb_event(
        "mlb:test:today",
        "2026-07-27T00:10:00+00:00",
        home="Yankees",
        away="Dodgers",
    )
    mlb_wrong_date = _make_mlb_event(
        "mlb:test:wrong-date",
        "2026-07-28T00:10:00+00:00",
        home="Mets",
        away="Braves",
    )
    nba_event = _make_nba_event("nba:test:game", "2026-07-27T19:00:00+00:00")
    snapshot = {
        "generated_at": "2026-07-27T12:00:00+00:00",
        "feed": {"freshness": "fresh", "status": "healthy"},
        "events": [mlb_today, mlb_wrong_date, nba_event],
        "evaluations": [],
        "resolved_predictions": [],
    }
    double = _PersonalServiceDouble(snapshot)
    service = MlbIntelligenceBriefService()
    report = service.build_from_personal_service(double, "2026-07-27")

    assert report.slate_summary.games_analyzed == 1
    assert report.game_cards[0].canonical_game_id == "mlb:test:today"


def test_build_from_personal_service_empty_variants_are_transparent():
    from sports.intelligence.briefs.markdown import render_brief
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    required = "No MLB events available for the requested slate."
    snapshots = (
        {
            "generated_at": "2026-07-27T12:00:00+00:00",
            "feed": {"freshness": "fresh", "status": "healthy"},
            "events": [],
            "evaluations": [],
            "resolved_predictions": [],
        },
        {
            "generated_at": "2026-07-27T12:00:00+00:00",
            "feed": {"freshness": "fresh", "status": "healthy"},
            "events": [
                _make_nba_event("nba:test:game", "2026-07-27T19:00:00+00:00")
            ],
            "evaluations": [],
            "resolved_predictions": [],
        },
        {
            "generated_at": "2026-07-27T12:00:00+00:00",
            "feed": {"freshness": "fresh", "status": "healthy"},
            "events": [
                _make_mlb_event(
                    "mlb:test:wrong-date",
                    "2026-07-28T00:10:00+00:00",
                )
            ],
            "evaluations": [],
            "resolved_predictions": [],
        },
    )

    for snapshot in snapshots:
        original_snapshot = deepcopy(snapshot)
        double = _PersonalServiceDouble(snapshot)
        report = MlbIntelligenceBriefService().build_from_personal_service(
            double, "2026-07-27"
        )
        markdown = render_brief(report)

        assert double.snapshot_call_count == 1
        assert snapshot == original_snapshot
        assert report.game_cards == ()
        assert report.unavailable_reason == required
        assert report.slate_summary.highest_priority_research_items == (required,)
        assert required in markdown
        assert "\n### " not in markdown


def test_build_from_personal_service_missing_forecast_stays_unavailable():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    snapshot = _snapshot()
    snapshot["events"] = [snapshot["events"][0]]
    snapshot["evaluations"] = snapshot["events"][0]["evaluations"]
    report = MlbIntelligenceBriefService().build_from_personal_service(
        _PersonalServiceDouble(snapshot), "2026-07-27"
    )

    assert report.slate_summary.games_analyzed == 1
    assert report.game_cards[0].probabilities.model_probability is None
    assert report.game_cards[0].probabilities.model_probability_reason == (
        "No experimental model probability was persisted."
    )


def test_build_from_personal_service_output_is_deterministic():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    service = MlbIntelligenceBriefService()
    first = service.build_from_personal_service(
        _PersonalServiceDouble(_snapshot()), "2026-07-27"
    )
    second = service.build_from_personal_service(
        _PersonalServiceDouble(_snapshot()), "2026-07-27"
    )

    assert first == second


def test_build_from_personal_service_existing_build_report_still_works():
    from sports.intelligence.briefs.service import MlbIntelligenceBriefService

    service = MlbIntelligenceBriefService()
    markdown = service.build_markdown(_snapshot())
    empty_report = service.build_report(
        {
            "generated_at": NOW.isoformat(),
            "events": [],
            "evaluations": [],
            "resolved_predictions": [],
        }
    )

    assert "# MLB Intelligence Brief" in markdown
    assert "New York Yankees vs Los Angeles Dodgers" in markdown
    assert "New York Mets vs Atlanta Braves" in markdown
    assert (
        empty_report.unavailable_reason
        == "Unavailable: no MLB events were available for this slate."
    )
    assert empty_report.slate_summary.highest_priority_research_items == ()
