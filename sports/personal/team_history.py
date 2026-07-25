from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


@dataclass(frozen=True, slots=True)
class TeamGame:
    league: str
    event_id: str
    start_time: str
    season: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    neutral_site: bool
    source_url: str


class PublicTeamHistorySource:
    ESPN_URL = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/"
        "wnba/scoreboard?dates={season}&limit=1000"
    )
    MLB_URL = (
        "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        "&startDate={season}-03-01&endDate={season}-11-30&hydrate=team"
    )

    def __init__(self, session=None, *, timeout: float = 30.0) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch(self, league: str, seasons: tuple[int, ...]) -> list[TeamGame]:
        normalized = league.upper()
        games: list[TeamGame] = []
        for season in seasons:
            if normalized == "WNBA":
                games.extend(self._wnba(season))
            elif normalized == "MLB":
                games.extend(self._mlb(season))
            else:
                raise ValueError(f"unsupported league: {league}")
        unique = {game.event_id: game for game in games}
        return sorted(unique.values(), key=lambda game: game.start_time)

    def _get(self, url: str) -> dict[str, Any]:
        response = self.session.get(
            url,
            headers={"User-Agent": "RAGHub-SIP/1.0", "Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("sports history provider returned invalid JSON")
        return payload

    def _wnba(self, season: int) -> list[TeamGame]:
        url = self.ESPN_URL.format(season=season)
        payload = self._get(url)
        games = []
        for event in payload.get("events") or []:
            if event.get("season", {}).get("type") != 2:
                continue
            competition = (event.get("competitions") or [{}])[0]
            status = event.get("status", {}).get("type", {})
            if not status.get("completed"):
                continue
            sides = {
                item.get("homeAway"): item
                for item in competition.get("competitors") or []
            }
            if "home" not in sides or "away" not in sides:
                continue
            home, away = sides["home"], sides["away"]
            home_name = str(home.get("team", {}).get("displayName") or "")
            away_name = str(away.get("team", {}).get("displayName") or "")
            if home_name.lower().startswith("team ") or away_name.lower().startswith("team "):
                continue
            games.append(TeamGame(
                league="WNBA",
                event_id=f"espn:{event['id']}",
                start_time=str(event.get("date") or competition.get("date")),
                season=str(season),
                home_team=home_name,
                away_team=away_name,
                home_score=int(home["score"]),
                away_score=int(away["score"]),
                neutral_site=bool(competition.get("neutralSite")),
                source_url=url,
            ))
        return games

    def _mlb(self, season: int) -> list[TeamGame]:
        url = self.MLB_URL.format(season=season)
        payload = self._get(url)
        games = []
        for date_group in payload.get("dates") or []:
            for game in date_group.get("games") or []:
                if game.get("gameType") != "R":
                    continue
                if game.get("status", {}).get("abstractGameState") != "Final":
                    continue
                if game.get("isTie"):
                    continue
                teams = game.get("teams") or {}
                home, away = teams.get("home") or {}, teams.get("away") or {}
                if home.get("score") is None or away.get("score") is None:
                    continue
                games.append(TeamGame(
                    league="MLB",
                    event_id=f"mlb:{game['gamePk']}",
                    start_time=str(game.get("gameDate") or ""),
                    season=str(season),
                    home_team=str(home.get("team", {}).get("name") or ""),
                    away_team=str(away.get("team", {}).get("name") or ""),
                    home_score=int(home["score"]),
                    away_score=int(away["score"]),
                    neutral_site=False,
                    source_url=url,
                ))
        return games

    @staticmethod
    def save(games: list[TeamGame], path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps([asdict(game) for game in games], indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def load(path: str | Path) -> list[TeamGame]:
        return [
            TeamGame(**item)
            for item in json.loads(Path(path).read_text(encoding="utf-8"))
        ]


class PregameTeamFeatureBuilder:
    RECENT_GAMES = 10

    @classmethod
    def training_rows(cls, games: list[TeamGame]) -> list[dict[str, Any]]:
        records: dict[tuple[str, str], list[int]] = defaultdict(list)
        recent: dict[tuple[str, str], deque[int]] = defaultdict(
            lambda: deque(maxlen=cls.RECENT_GAMES)
        )
        score_diffs: dict[tuple[str, str], list[int]] = defaultdict(list)
        recent_score_diffs: dict[tuple[str, str], deque[int]] = defaultdict(
            lambda: deque(maxlen=cls.RECENT_GAMES)
        )
        last_played: dict[tuple[str, str], datetime] = {}
        rows = []
        for game in sorted(games, key=lambda item: item.start_time):
            start = cls._time(game.start_time)
            home_key = (game.season, cls.team_key(game.home_team))
            away_key = (game.season, cls.team_key(game.away_team))
            rows.append({
                "event_date": start.isoformat(),
                "home_win": int(game.home_score > game.away_score),
                "home_season_win_pct": cls._rate(records[home_key]),
                "away_season_win_pct": cls._rate(records[away_key]),
                "home_recent_win_pct": cls._rate(recent[home_key]),
                "away_recent_win_pct": cls._rate(recent[away_key]),
                "home_season_score_diff": cls._average(score_diffs[home_key]),
                "away_season_score_diff": cls._average(score_diffs[away_key]),
                "home_recent_score_diff": cls._average(
                    recent_score_diffs[home_key]
                ),
                "away_recent_score_diff": cls._average(
                    recent_score_diffs[away_key]
                ),
                "home_rest_days": cls._rest(last_played.get(home_key), start),
                "away_rest_days": cls._rest(last_played.get(away_key), start),
                "home_advantage": 0 if game.neutral_site else 1,
            })
            home_win = int(game.home_score > game.away_score)
            home_diff = game.home_score - game.away_score
            for key, won, score_diff in (
                (home_key, home_win, home_diff),
                (away_key, 1 - home_win, -home_diff),
            ):
                records[key].append(won)
                recent[key].append(won)
                score_diffs[key].append(score_diff)
                recent_score_diffs[key].append(score_diff)
                last_played[key] = start
        return rows

    @classmethod
    def upcoming_features(
        cls,
        games: list[TeamGame],
        *,
        season: str,
        home_team: str,
        away_team: str,
        event_start: str,
    ) -> dict[str, float]:
        start = cls._time(event_start)
        completed = [
            game for game in games
            if game.season == season and cls._time(game.start_time) < start
        ]
        records: dict[str, list[int]] = defaultdict(list)
        recent: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=cls.RECENT_GAMES)
        )
        score_diffs: dict[str, list[int]] = defaultdict(list)
        recent_score_diffs: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=cls.RECENT_GAMES)
        )
        last_played: dict[str, datetime] = {}
        for game in sorted(completed, key=lambda item: item.start_time):
            home, away = cls.team_key(game.home_team), cls.team_key(game.away_team)
            home_win = int(game.home_score > game.away_score)
            home_diff = game.home_score - game.away_score
            for key, won, score_diff in (
                (home, home_win, home_diff),
                (away, 1 - home_win, -home_diff),
            ):
                records[key].append(won)
                recent[key].append(won)
                score_diffs[key].append(score_diff)
                recent_score_diffs[key].append(score_diff)
                last_played[key] = cls._time(game.start_time)
        home, away = cls.team_key(home_team), cls.team_key(away_team)
        if not records[home] or not records[away]:
            raise ValueError("INSUFFICIENT_FEATURE_DATA: team history is missing")
        return {
            "home_season_win_pct": cls._rate(records[home]),
            "away_season_win_pct": cls._rate(records[away]),
            "home_recent_win_pct": cls._rate(recent[home]),
            "away_recent_win_pct": cls._rate(recent[away]),
            "home_season_score_diff": cls._average(score_diffs[home]),
            "away_season_score_diff": cls._average(score_diffs[away]),
            "home_recent_score_diff": cls._average(recent_score_diffs[home]),
            "away_recent_score_diff": cls._average(recent_score_diffs[away]),
            "home_rest_days": cls._rest(last_played.get(home), start),
            "away_rest_days": cls._rest(last_played.get(away), start),
            "home_advantage": 1.0,
        }

    @staticmethod
    def team_key(value: str) -> str:
        normalized = " ".join(
            "".join(character.lower() if character.isalnum() else " " for character in value).split()
        )
        return {
            "oakland athletics": "athletics",
        }.get(normalized, normalized)

    @staticmethod
    def _rate(values) -> float:
        return sum(values) / len(values) if values else 0.5

    @staticmethod
    def _average(values) -> float:
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _rest(previous: datetime | None, current: datetime) -> float:
        if previous is None:
            return 3.0
        return float(max(0, min(10, (current.date() - previous.date()).days - 1)))

    @staticmethod
    def _time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("game timestamps must be timezone-aware")
        return parsed.astimezone(timezone.utc)
