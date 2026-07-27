from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from sports.personal.normalization import (
    MoneylineNormalizer,
    canonical_game_identity_v1,
    canonical_moneyline_market_id_v1,
)
from sports.personal.repository import PersonalEditionRepository

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
EVENT_START = datetime(2026, 7, 27, 0, 10, tzinfo=timezone.utc)


def _row(
    *,
    source: str,
    provider_event_id: str,
    sportsbook: str,
    selection: str,
    selection_team: str,
    american_price: int,
    observed_at: datetime,
    home_team: str = "NY Yankees",
    away_team: str = "LA Dodgers",
    source_url: str = "https://example.test/quote",
    event_start: datetime | None = EVENT_START,
    official_game_number: int | None = None,
):
    row = {
        "provider_event_id": provider_event_id,
        "league": "MLB",
        "season": "2026",
        "home_team": home_team,
        "away_team": away_team,
        "sportsbook": sportsbook,
        "market": "h2h",
        "period": "full_game",
        "selection": selection,
        "selection_team": selection_team,
        "american_price": american_price,
        "observed_at": observed_at.isoformat(),
        "source": source,
        "source_url": source_url,
        "data_mode": "live",
        "is_live": False,
    }
    if event_start is not None:
        row["event_start"] = event_start.isoformat()
    if official_game_number is not None:
        row["official_game_number"] = official_game_number
    return row


def _provider_pair(
    *,
    source: str,
    provider_event_id: str,
    sportsbook: str = "DraftKings",
    observed_at: datetime = NOW - timedelta(seconds=30),
    home_team: str = "NY Yankees",
    away_team: str = "LA Dodgers",
    home_price: int = -140,
    away_price: int = 120,
    event_start: datetime | None = EVENT_START,
    official_game_number: int | None = None,
):
    return [
        _row(
            source=source,
            provider_event_id=provider_event_id,
            sportsbook=sportsbook,
            selection="home",
            selection_team=home_team,
            american_price=home_price,
            observed_at=observed_at,
            home_team=home_team,
            away_team=away_team,
            event_start=event_start,
            official_game_number=official_game_number,
        ),
        _row(
            source=source,
            provider_event_id=provider_event_id,
            sportsbook=sportsbook,
            selection="away",
            selection_team=away_team,
            american_price=away_price,
            observed_at=observed_at,
            home_team=home_team,
            away_team=away_team,
            event_start=event_start,
            official_game_number=official_game_number,
        ),
    ]


def test_same_mlb_game_from_two_providers_resolves_one_canonical_game_id():
    rows = [
        *_provider_pair(source="odds_api_io", provider_event_id="provider-a-101"),
        *_provider_pair(source="sportsgameodds", provider_event_id="provider-b-888"),
    ]

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)

    assert len(result.events) == 1
    assert len({quote.canonical_event_id for quote in result.quotes}) == 1


def test_doubleheader_official_game_numbers_produce_distinct_game_ids():
    rows = [
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-game-1",
            official_game_number=1,
        ),
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-game-2",
            official_game_number=2,
        ),
    ]

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)

    assert len(result.events) == 2
    assert len({quote.canonical_event_id for quote in result.quotes}) == 2


def test_same_official_game_number_from_two_providers_converges():
    rows = [
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-game-1",
            official_game_number=1,
        ),
        *_provider_pair(
            source="sportsgameodds",
            provider_event_id="provider-b-game-1",
            official_game_number=1,
        ),
    ]

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)

    assert len(result.events) == 1
    assert len({quote.canonical_event_id for quote in result.quotes}) == 1


def test_scheduled_start_fallback_distinguishes_same_team_games():
    rows = [
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-early",
            event_start=EVENT_START,
        ),
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-late",
            event_start=EVENT_START + timedelta(hours=5),
        ),
    ]

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)

    assert len(result.events) == 2
    assert len({quote.canonical_event_id for quote in result.quotes}) == 2


def test_time_zone_equivalent_scheduled_starts_converge():
    utc_start = datetime(2026, 7, 27, 0, 10, tzinfo=timezone.utc)
    eastern_start = datetime.fromisoformat("2026-07-26T20:10:00-04:00")
    rows = [
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-101",
            event_start=utc_start,
        ),
        *_provider_pair(
            source="sportsgameodds",
            provider_event_id="provider-b-888",
            event_start=eastern_start,
        ),
    ]

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)

    assert len(result.events) == 1
    assert len({quote.canonical_event_id for quote in result.quotes}) == 1


def test_adjacent_local_dates_with_same_utc_date_do_not_collide():
    evening_game = datetime.fromisoformat("2026-07-26T23:30:00-04:00")
    next_local_day_game = datetime.fromisoformat("2026-07-27T00:30:00-04:00")
    rows = [
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-evening",
            event_start=evening_game,
        ),
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-next-day",
            event_start=next_local_day_game,
        ),
    ]

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)

    assert len(result.events) == 2
    assert len({quote.canonical_event_id for quote in result.quotes}) == 2


def test_single_game_fallback_identity_is_stable_and_explicit():
    first = MoneylineNormalizer(maximum_age_seconds=600).normalize(
        _provider_pair(source="odds_api_io", provider_event_id="provider-a-101"),
        as_of=NOW,
    )
    second = MoneylineNormalizer(maximum_age_seconds=600).normalize(
        _provider_pair(
            source="sportsgameodds",
            provider_event_id="provider-b-888",
            home_price=-135,
            away_price=115,
        ),
        as_of=NOW,
    )

    expected = (
        "mlb:game:v1:away:los-angeles-dodgers:home:new-york-yankees:"
        "instance:start:20260727T001000Z"
    )
    assert {quote.canonical_event_id for quote in first.quotes} == {expected}
    assert {quote.canonical_event_id for quote in second.quotes} == {expected}


def test_missing_game_number_and_scheduled_start_raises_validation_error():
    with pytest.raises(
        ValueError,
        match="official game number or timezone-aware scheduled start is required",
    ):
        canonical_game_identity_v1(
            league="MLB",
            start=None,
            away_team_name="los angeles dodgers",
            home_team_name="new york yankees",
        )


def test_team_name_formatting_differences_do_not_split_game_identity():
    rows = [
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-101",
            home_team="N.Y. Yankees",
            away_team="L.A. Dodgers",
        ),
        *_provider_pair(
            source="sportsgameodds",
            provider_event_id="provider-b-888",
            home_team="New York Yankees",
            away_team="Los Angeles Dodgers",
        ),
    ]

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)

    assert len(result.events) == 1
    assert len({quote.canonical_event_id for quote in result.quotes}) == 1


def test_canonical_market_id_is_deterministic_from_game_id():
    rows = _provider_pair(source="odds_api_io", provider_event_id="provider-a-101")

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)
    quote = result.quotes[0]

    assert quote.canonical_market_id == canonical_moneyline_market_id_v1(
        quote.canonical_event_id
    )


def test_home_and_away_outcome_ids_are_stable_and_do_not_swap():
    rows = _provider_pair(source="odds_api_io", provider_event_id="provider-a-101")

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)
    quotes_by_selection = {quote.selection: quote for quote in result.quotes}

    assert set(quotes_by_selection) == {"home", "away"}
    assert quotes_by_selection["home"].canonical_outcome_id.endswith(":outcome:home:v1")
    assert quotes_by_selection["away"].canonical_outcome_id.endswith(":outcome:away:v1")
    assert (
        quotes_by_selection["home"].canonical_outcome_id
        != quotes_by_selection["away"].canonical_outcome_id
    )


def test_sportsbook_aliases_map_to_one_canonical_sportsbook_id():
    rows = [
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-101",
            sportsbook="DraftKings",
        ),
        *_provider_pair(
            source="sportsgameodds",
            provider_event_id="provider-b-888",
            sportsbook="Draft Kings",
        ),
    ]

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)

    assert {quote.canonical_sportsbook_id for quote in result.quotes} == {"draftkings"}


def test_repeated_refreshes_preserve_canonical_entity_ids():
    normalizer = MoneylineNormalizer(maximum_age_seconds=600)
    first = normalizer.normalize(
        _provider_pair(source="odds_api_io", provider_event_id="provider-a-101"),
        as_of=NOW,
    )
    second = normalizer.normalize(
        _provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-202",
            observed_at=NOW - timedelta(seconds=10),
            home_price=-135,
            away_price=115,
        ),
        as_of=NOW,
    )

    assert {quote.canonical_event_id for quote in first.quotes} == {
        quote.canonical_event_id for quote in second.quotes
    }
    assert {quote.canonical_market_id for quote in first.quotes} == {
        quote.canonical_market_id for quote in second.quotes
    }
    assert {
        (quote.selection, quote.canonical_outcome_id) for quote in first.quotes
    } == {(quote.selection, quote.canonical_outcome_id) for quote in second.quotes}


def test_quote_provenance_retains_provider_and_canonical_references():
    row = _row(
        source="odds_api_io",
        provider_event_id="provider-a-101",
        sportsbook="DraftKings",
        selection="home",
        selection_team="NY Yankees",
        american_price=-140,
        observed_at=NOW - timedelta(seconds=30),
        source_url="https://example.test/event/provider-a-101",
    )

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize([row], as_of=NOW)
    quote = result.quotes[0]
    provenance = quote.provenance_v1

    assert provenance.provider == "odds_api_io"
    assert provenance.provider_event_id == "provider-a-101"
    assert provenance.source_url == "https://example.test/event/provider-a-101"
    assert provenance.observed_at == quote.observed_at
    assert provenance.canonical_sportsbook_id == "draftkings"
    assert provenance.canonical_game_id == quote.canonical_event_id
    assert provenance.canonical_market_id == quote.canonical_market_id
    assert provenance.canonical_outcome_id == quote.canonical_outcome_id


def test_provider_quote_ids_can_differ_without_duplicate_canonical_game_or_outcomes():
    rows = [
        *_provider_pair(source="odds_api_io", provider_event_id="provider-a-101"),
        *_provider_pair(
            source="sportsgameodds",
            provider_event_id="provider-b-888",
            observed_at=NOW - timedelta(seconds=20),
            home_price=-138,
            away_price=118,
        ),
    ]

    result = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)

    home_quotes = sorted(
        [quote for quote in result.quotes if quote.selection == "home"],
        key=lambda item: item.provider_quote_id,
    )
    away_quotes = sorted(
        [quote for quote in result.quotes if quote.selection == "away"],
        key=lambda item: item.provider_quote_id,
    )

    assert len({quote.provider_quote_id for quote in home_quotes}) == 2
    assert len({quote.provider_quote_id for quote in away_quotes}) == 2
    assert len({quote.canonical_event_id for quote in result.quotes}) == 1
    assert len({quote.canonical_market_id for quote in result.quotes}) == 1
    assert (
        len(
            {
                quote.canonical_outcome_id
                for quote in result.quotes
                if quote.selection == "home"
            }
        )
        == 1
    )
    assert (
        len(
            {
                quote.canonical_outcome_id
                for quote in result.quotes
                if quote.selection == "away"
            }
        )
        == 1
    )


def test_repository_persists_distinct_provider_observations_at_same_timestamp(tmp_path):
    rows = [
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-101",
            sportsbook="DraftKings",
        ),
        *_provider_pair(
            source="sportsgameodds",
            provider_event_id="provider-b-888",
            sportsbook="DraftKings",
        ),
    ]
    normalized = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)
    repository = PersonalEditionRepository(tmp_path / "sip.db")

    repository.save_market_snapshot(
        events=normalized.events,
        quotes=normalized.quotes,
        retrieved_at=NOW.isoformat(),
    )

    persisted = repository.list_quotes()
    assert len(persisted) == 4
    assert len({quote.provider_quote_id for quote in persisted}) == 4


def test_repository_persists_distinct_price_observations_at_same_timestamp(tmp_path):
    rows = [
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-101",
            sportsbook="DraftKings",
            home_price=-140,
            away_price=120,
        ),
        *_provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-101",
            sportsbook="DraftKings",
            home_price=-135,
            away_price=115,
        ),
    ]
    normalized = MoneylineNormalizer(maximum_age_seconds=600).normalize(rows, as_of=NOW)
    repository = PersonalEditionRepository(tmp_path / "sip.db")

    repository.save_market_snapshot(
        events=normalized.events,
        quotes=normalized.quotes,
        retrieved_at=NOW.isoformat(),
    )

    persisted = repository.list_quotes()
    assert len(persisted) == 4
    assert len({quote.provider_quote_id for quote in persisted}) == 4


def test_repository_provider_quote_id_is_idempotent(tmp_path):
    normalized = MoneylineNormalizer(maximum_age_seconds=600).normalize(
        _provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-101",
            sportsbook="DraftKings",
        ),
        as_of=NOW,
    )
    repository = PersonalEditionRepository(tmp_path / "sip.db")

    repository.save_market_snapshot(
        events=normalized.events,
        quotes=normalized.quotes,
        retrieved_at=NOW.isoformat(),
    )
    repository.save_market_snapshot(
        events=normalized.events,
        quotes=normalized.quotes,
        retrieved_at=NOW.isoformat(),
    )

    persisted = repository.list_quotes()
    assert len(persisted) == 2
    assert len({quote.provider_quote_id for quote in persisted}) == 2


def test_repository_hydrates_legacy_quote_rows_without_provider_quote_id(tmp_path):
    normalized = MoneylineNormalizer(maximum_age_seconds=600).normalize(
        _provider_pair(
            source="odds_api_io",
            provider_event_id="provider-a-101",
            sportsbook="DraftKings",
        ),
        as_of=NOW,
    )
    quote_payload = asdict(normalized.quotes[0])
    quote_payload.pop("provider_quote_id")
    quote_payload.pop("canonical_sportsbook_id")
    quote_payload.pop("canonical_market_id")
    quote_payload.pop("canonical_outcome_id")

    repository = PersonalEditionRepository(tmp_path / "sip.db")
    repository.migrate()
    with repository.connect() as connection:
        connection.execute(
            """
            INSERT INTO sip_quotes (
                canonical_event_id, sportsbook, market, period,
                selection, observed_at, provider_quote_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized.quotes[0].canonical_event_id,
                normalized.quotes[0].sportsbook,
                normalized.quotes[0].market,
                normalized.quotes[0].period,
                normalized.quotes[0].selection,
                normalized.quotes[0].observed_at,
                "legacy:row-1",
                json.dumps(quote_payload, sort_keys=True),
            ),
        )

    persisted = repository.list_quotes()
    assert len(persisted) == 1
    assert persisted[0].provider_quote_id == ""
