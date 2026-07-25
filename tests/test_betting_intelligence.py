from datetime import datetime, timedelta, timezone

import pytest

from sports.betting.engine import BettingIntelligenceEngine
from sports.betting.models import (
    BettingPolicy,
    DataClassification,
    Decision,
    ModelPrediction,
    NoVigMethod,
    OddsQuote,
)
from sports.betting.no_vig import remove_vig
from sports.betting.repository import SQLiteBettingRepository
from sports.betting.pipeline import BettingIntelligencePipeline
from sports.betting.normalization import MarketNormalizer
from sports.data.repositories.sqlite_lifecycle_repository import (
    SQLiteLifecycleRepository,
)
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService


NOW = datetime(2026, 1, 15, 18, tzinfo=timezone.utc)


def quote(book: str, selection: str, price: int, *, age: int = 20, designation="current"):
    return OddsQuote(
        event_id="nba-1",
        market="player_points",
        selection=selection,
        sportsbook=book,
        line=22.5,
        american_price=price,
        event_start=NOW + timedelta(hours=2),
        fetched_at=NOW - timedelta(seconds=age),
        source="test-feed",
        designation=designation,
    )


def prediction(**overrides):
    values = dict(
        event_id="nba-1",
        market="player_points",
        selection="over",
        line=22.5,
        probability=0.58,
        uncertainty=0.04,
        model_probabilities=(0.56, 0.58, 0.60),
        model_version="sip-props-v1",
        generated_at=NOW,
        similar_bet_sample=150,
        similar_bet_brier_score=0.20,
        reasons=("minutes projection increased",),
        invalidators=("starter status changes",),
    )
    values.update(overrides)
    return ModelPrediction(**values)


@pytest.mark.parametrize("method", list(NoVigMethod))
def test_no_vig_methods_produce_valid_distribution(method):
    result = remove_vig((1 / 1.91, 1 / 1.91), method)
    assert sum(result) == pytest.approx(1.0)
    assert result == pytest.approx((0.5, 0.5), abs=1e-6)


def test_engine_finds_best_price_and_qualifies_material_edge():
    quotes = [
        quote("Book A", "over", -125), quote("Book A", "under", 105),
        quote("Book B", "over", -110), quote("Book B", "under", -110),
        quote("Book C", "over", 100), quote("Book C", "under", -120),
    ]
    assessment = BettingIntelligenceEngine().assess(prediction(), quotes, as_of=NOW)

    assert assessment.decision is Decision.QUALIFIED
    assert assessment.best_quote.sportsbook == "Book C"
    assert assessment.consensus_probability == pytest.approx(0.5036, abs=0.002)
    assert assessment.probability_edge == pytest.approx(0.0764, abs=0.002)
    assert assessment.expected_return == pytest.approx(0.16)
    assert assessment.confidence_adjusted_return == pytest.approx(0.08)
    assert assessment.confidence_interval == pytest.approx((0.54, 0.62))


def test_engine_abstains_and_excludes_stale_quote():
    quotes = [
        quote("Fresh", "over", -110), quote("Fresh", "under", -110),
        quote("Stale", "over", 120, age=600), quote("Stale", "under", -140, age=600),
    ]
    assessment = BettingIntelligenceEngine().assess(
        prediction(probability=0.52, uncertainty=0.12, similar_bet_sample=5),
        quotes,
        as_of=NOW,
    )

    assert assessment.decision is Decision.NO_BET
    assert assessment.best_quote.sportsbook == "Fresh"
    assert "insufficient fresh books" in assessment.rejection_reasons[0]
    assert any("stale quotes excluded: Stale" in warning for warning in assessment.warnings)


def test_repository_tracks_snapshot_recommendation_and_closing_value(tmp_path):
    repository = SQLiteBettingRepository(tmp_path / "betting.db")
    quotes = [
        quote("A", "over", 100), quote("A", "under", -120),
        quote("B", "over", -105), quote("B", "under", -115),
    ]
    repository.add_quotes(quotes)
    assessment = BettingIntelligenceEngine(
        BettingPolicy(minimum_edge=0.01, minimum_expected_return=0.01)
    ).assess(prediction(), quotes, as_of=NOW)
    recommendation_id = repository.save_recommendation(assessment)
    closing = quote("A", "over", -120, designation="closing")

    evaluation = repository.evaluate(recommendation_id, closing_quote=closing, won=False)

    assert evaluation.beat_closing_price is True
    assert evaluation.realized_return == -1.0
    assert evaluation.won is False
    history = repository.quote_history(
        event_id="nba-1",
        market="player_points",
        selection="over",
        sportsbook="A",
    )
    assert len(history) == 1
    assert history[0]["american_price"] == 100


def test_production_pipeline_rejects_replay_labeled_live_and_records_lifecycle(
    tmp_path,
):
    normalizer = MarketNormalizer.release_candidate_defaults(
        players={"jalen brunson": "nba:player:1"}
    )
    repository = SQLiteBettingRepository(tmp_path / "betting.db")
    lifecycle = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    pipeline = BettingIntelligencePipeline(
        normalizer=normalizer,
        repository=repository,
        lifecycle=lifecycle,
    )
    rows = []
    for book, over, under in (
        ("DraftKings", 100, -120),
        ("FanDuel", -105, -115),
    ):
        for outcome, price in (("over", over), ("under", under)):
            rows.append(
                {
                    "sportsbook": book,
                    "player": "Jalen Brunson",
                    "home_team": "Chicago Bulls",
                    "away_team": "New York Knicks",
                    "market": "player_points",
                    "outcome": outcome,
                    "line": 22.5,
                    "american_price": price,
                    "event_start": (NOW + timedelta(hours=2)).isoformat(),
                    "fetched_at": (NOW - timedelta(seconds=20)).isoformat(),
                    "source": "test licensed feed",
                    "source_url": "https://example.test/market",
                    "status": "active",
                    "data_classification": "live",
                }
            )

    result = pipeline.run(
        raw_quotes=rows,
        predictions=[
            prediction(
                event_id="nba:game:20260115:nyk:chi",
                canonical_player_id="nba:player:1",
                outcome_definition="over_under",
            )
        ],
        classification=DataClassification.LIVE,
        as_of=NOW,
    )

    assert result.classification is DataClassification.LIVE
    assert result.complete_book_count == 2
    assert result.situation_ids
    assert lifecycle.get_situation(result.situation_ids[0]).current_stage.value == "monitor"

    rows[0]["designation"] = "replay"
    with pytest.raises(ValueError, match="cannot contain replay"):
        pipeline.run(
            raw_quotes=rows,
            predictions=[prediction()],
            classification=DataClassification.LIVE,
            as_of=NOW,
        )
