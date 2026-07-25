from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from sports.personal.models import DataMode


@dataclass(frozen=True, slots=True)
class PersonalEditionSettings:
    mode: DataMode
    odds_feed_enabled: bool
    odds_provider: str
    odds_api_key: str = field(repr=False)
    odds_regions: str
    odds_sports: tuple[str, ...]
    odds_markets: tuple[str, ...]
    odds_base_url: str
    odds_request_timeout_seconds: int
    odds_max_events_per_request: int
    odds_max_age_seconds: int
    minimum_model_edge: float
    minimum_data_quality: float
    minimum_complete_books: int
    forecast_max_age_seconds: int
    database_path: str
    model_path: str
    host: str
    port: int
    refresh_interval_seconds: int

    @classmethod
    def from_environment(cls) -> PersonalEditionSettings:
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, str]
    ) -> PersonalEditionSettings:
        mode_text = values.get("SIP_MODE", "live").strip().lower()
        try:
            mode = DataMode(mode_text)
        except ValueError as error:
            raise ValueError("SIP_MODE must be live or replay") from error
        enabled = cls._boolean(values.get("ODDS_FEED_ENABLED", "true"))
        key = values.get("ODDS_API_KEY", "").strip()
        provider = values.get("ODDS_PROVIDER", "the_odds_api").strip().lower()
        provider_aliases = {
            "the_odds_api": "the_odds_api",
            "sports_game_odds": "sportsgameodds",
            "sports_game_odds_api": "sportsgameodds",
            "sportsgameodds": "sportsgameodds",
        }
        if provider not in provider_aliases:
            raise ValueError(f"unsupported ODDS_PROVIDER: {provider}")
        provider = provider_aliases[provider]
        if mode is DataMode.LIVE and enabled and not key:
            raise ValueError(
                "ODDS_API_KEY is required when the live odds feed is enabled"
            )
        sports = tuple(
            item.strip().lower()
            for item in values.get(
                "ODDS_SPORTS", "basketball_nba"
            ).split(",")
            if item.strip()
        )
        unsupported = set(sports) - {
            "basketball_nba",
            "basketball_wnba",
            "baseball_mlb",
        }
        if unsupported:
            raise ValueError(
                f"ODDS_SPORTS contains unsupported values: {', '.join(sorted(unsupported))}"
            )
        markets = tuple(
            item.strip().lower()
            for item in values.get("ODDS_MARKETS", "h2h").split(",")
            if item.strip()
        )
        if markets != ("h2h",):
            raise ValueError("SIP v1 Personal Edition supports ODDS_MARKETS=h2h")
        edge = cls._unit(values, "MIN_MODEL_EDGE", 0.03)
        quality = cls._unit(values, "MIN_DATA_QUALITY", 0.75)
        return cls(
            mode=mode,
            odds_feed_enabled=enabled,
            odds_provider=provider,
            odds_api_key=key,
            odds_regions=values.get("ODDS_REGIONS", "us").strip(),
            odds_sports=sports,
            odds_markets=markets,
            odds_base_url=values.get(
                "ODDS_BASE_URL",
                (
                    "https://api.sportsgameodds.com/v2"
                    if provider == "sportsgameodds"
                    else "https://api.the-odds-api.com/v4"
                ),
            ).strip().rstrip("/"),
            odds_request_timeout_seconds=cls._positive_int(
                values, "ODDS_REQUEST_TIMEOUT_SECONDS", 15
            ),
            odds_max_events_per_request=cls._positive_int(
                values, "ODDS_MAX_EVENTS_PER_REQUEST", 10
            ),
            odds_max_age_seconds=cls._positive_int(
                values,
                "ODDS_MAX_QUOTE_AGE_SECONDS",
                int(values.get("ODDS_MAX_AGE_SECONDS", "600")),
            ),
            minimum_model_edge=edge,
            minimum_data_quality=quality,
            minimum_complete_books=cls._positive_int(
                values, "MIN_COMPLETE_BOOKS", 2
            ),
            forecast_max_age_seconds=cls._positive_int(
                values, "FORECAST_MAX_AGE_SECONDS", 3600
            ),
            database_path=values.get(
                "SIP_PERSONAL_DATABASE", "data/sip_personal.db"
            ),
            model_path=values.get(
                "SIP_MODEL_PATH", "data/models/moneyline-v1.json"
            ),
            host=values.get("SIP_HOST", "127.0.0.1"),
            port=cls._positive_int(values, "SIP_PORT", 5000),
            refresh_interval_seconds=(
                cls._positive_int(values, "ODDS_REFRESH_MINUTES", 10) * 60
                if "ODDS_REFRESH_MINUTES" in values
                else cls._positive_int(values, "SIP_REFRESH_INTERVAL_SECONDS", 900)
            ),
        )

    @staticmethod
    def _boolean(value: str) -> bool:
        normalized = value.strip().lower()
        if normalized not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("ODDS_FEED_ENABLED must be true or false")
        return normalized in {"true", "1", "yes"}

    @staticmethod
    def _unit(values: Mapping[str, str], name: str, default: float) -> float:
        value = float(values.get(name, str(default)))
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
        return value

    @staticmethod
    def _positive_int(
        values: Mapping[str, str], name: str, default: int
    ) -> int:
        value = int(values.get(name, str(default)))
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        return value
