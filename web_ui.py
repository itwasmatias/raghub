from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import re
from textwrap import dedent
from urllib.parse import parse_qs, urlparse
from typing import Any

from models.sports.player import Player
from sports.application.nba_demo_runtime import BasketballDemoRuntime
from sports.features.builders.trend_builder import TrendFeatureBuilder
from sports.features.facade import FeatureFacade
from sports.features.registry import FeatureRegistry


def _first_value(candidate: Any, *names: str, default: Any = None) -> Any:
    if candidate is None:
        return default

    if isinstance(candidate, dict):
        for name in names:
            if name in candidate and candidate[name] is not None:
                return candidate[name]

    for name in names:
        if hasattr(candidate, name):
            value = getattr(candidate, name)
            if value is not None:
                return value

    return default


def _normalize_trending_player(player: Any) -> dict[str, Any]:
    player_details = _first_value(player, "player", default=player)
    team_details = _first_value(player, "team", default=player)

    badge = _first_value(
        player,
        "badge",
        "trend",
        "trend_label",
        "trend_state",
        default="stable",
    )

    return {
        "player_id": _first_value(player_details, "player_id", "id", default=""),
        "name": _first_value(
            player_details,
            "player_name",
            "name",
            "full_name",
            default="Unknown Player",
        ),
        "team": _first_value(
            team_details,
            "latest_team",
            "team_name",
            "name",
            "abbreviation",
            default="Unknown Team",
        ),
        "badge": str(badge),
        "recent_five_average": _first_value(
            player,
            "recent_five_ppg",
            "recent_five_average",
            "recent_5_game_average",
            "recent_average",
            default=0.0,
        ),
        "current_season_average": _first_value(
            player,
            "current_season_ppg",
            "current_season_average",
            "season_average",
            default=0.0,
        ),
        "previous_season_average": _first_value(
            player,
            "previous_season_ppg",
            "previous_season_average",
            default=0.0,
        ),
        "weighted_two_season_average": _first_value(
            player,
            "weighted_two_season_ppg",
            "weighted_two_season_average",
            "weighted_average",
            "two_season_average",
            default=0.0,
        ),
        "why": _first_value(
            player,
            "why",
            "explanation",
            "reason",
            default="SIP noticed a strong pattern in this player's recent games.",
        ),
        "last_refreshed_at": _first_value(
            player,
            "last_refreshed_at",
            "updated_at",
            "last_refresh",
            default=datetime.now(timezone.utc),
        ),
    }


def default_trending_player_provider() -> list[Any]:
    return []


def default_refresh_callback() -> None:
    return None


def default_load_history_callback() -> None:
    return None


def default_historical_load_status_provider() -> str | None:
    return None


def default_historical_load_status_details_provider() -> dict[str, Any] | None:
    return None


def default_player_detail_provider(player_id: str) -> dict[str, Any] | None:
    return None


def default_data_health_provider() -> dict[str, Any] | None:
    return None


def default_learning_summary_provider() -> dict[str, Any] | None:
    return None


def default_calibration_detail_provider() -> dict[str, Any] | None:
    return None


def default_top_hypothesis_provider(player_id: str) -> dict[str, Any] | None:
    return None


class FeatureUIHandler(BaseHTTPRequestHandler):
    trending_player_provider = staticmethod(default_trending_player_provider)
    refresh_callback = staticmethod(default_refresh_callback)
    historical_load_callback = staticmethod(default_load_history_callback)
    load_history_callback = staticmethod(default_load_history_callback)
    historical_load_status_provider = staticmethod(
        default_historical_load_status_provider
    )
    historical_load_status_details_provider = staticmethod(
        default_historical_load_status_details_provider
    )
    player_detail_provider = staticmethod(default_player_detail_provider)
    data_health_provider = staticmethod(default_data_health_provider)
    learning_summary_provider = staticmethod(default_learning_summary_provider)
    calibration_detail_provider = staticmethod(default_calibration_detail_provider)
    top_hypothesis_provider = staticmethod(default_top_hypothesis_provider)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self.selected_league = query.get("league", [""])[0]
        self.selected_competition = query.get("competition", [""])[0]
        self.explanation_mode = query.get("mode", ["adult"])[0]
        if parsed.path == "/":
            self.send_html(self.render_form())
            return
        if parsed.path == "/player":
            self.send_html(self.render_player_detail(query.get("player_id", [""])[0]))
            return
        if parsed.path == "/calibration":
            self.send_html(self.render_calibration_detail())
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def _handle_refresh(self) -> None:
        self.refresh_callback()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _handle_load_history(self) -> None:
        callback = getattr(self, "load_history_callback", None)
        if "load_history_callback" not in self.__dict__:
            callback = getattr(self, "historical_load_callback", callback)

        if callback is not None:
            callback()

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def send_html(self, html: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self) -> None:
        if self.path == "/build":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            form_data = parse_qs(body)

            name = form_data.get("name", [""])[0].strip()
            team = form_data.get("team", [""])[0].strip()
            history_text = form_data.get("performance_history", [""])[0].strip()

            try:
                history = [
                    int(item.strip())
                    for item in history_text.split(",")
                    if item.strip()
                ]
            except ValueError:
                history = []

            player = Player(
                name=name or "Demo Player",
                team=team or "Demo Team",
                performance_history=history,
            )

            registry = FeatureRegistry()
            registry.register_builder(TrendFeatureBuilder())

            facade = FeatureFacade(registry)
            view = facade.build(player, ["trend"])

            self.send_html(self.render_result(player, view))
            return

        if self.path == "/refresh":
            self._handle_refresh()
            return

        if self.path == "/load-history":
            self._handle_load_history()
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def render_form(self) -> str:
        self._runtime_provider_error = None
        load_status_message = self._get_historical_load_status_message()
        load_status_details = self._get_historical_load_status_details()
        trending_players = list(self._get_trending_players())
        if not load_status_message:
            runtime_provider_error = getattr(self, "_runtime_provider_error", None)
            if runtime_provider_error:
                load_status_message = runtime_provider_error

        status_html = ""
        if load_status_message:
            status_html = dedent(
                f"""
                <p class="why" style="margin-top: 12px; border-color: #f3b7b7; background: #fff1f1;">
                    <strong>Load status:</strong> {escape(load_status_message)}
                </p>
                """
            )

        health_html = self._render_data_health()
        learning_html = self._render_learning_summary()
        if trending_players:
            games_loaded, players_loaded = self._extract_loaded_counts(
                load_status_details,
                load_status_message,
                trending_players,
            )
            source_label = self._resolve_source_label(
                load_status_details,
                load_status_message,
            )
            cards = [
                self.render_trending_card(
                    player,
                    source_label,
                )
                for player in trending_players
            ]
            cards_html = "".join(cards)
            first_player = _normalize_trending_player(trending_players[0])
            league = escape(
                str(
                    _first_value(
                        trending_players[0],
                        "league",
                        default="Basketball",
                    )
                )
            )
            competition = escape(
                str(
                    _first_value(
                        trending_players[0],
                        "competition",
                        default="Professional",
                    )
                )
            )
            metadata_html = dedent(
                f"""
                <div class="refresh" style="margin-top: 12px;">
                    League: {league} · Competition: {competition} · Last refresh: {escape(self._format_refresh_time(first_player["last_refreshed_at"]))}
                </div>
                <div class="refresh" style="margin-top: 6px;">
                    Games loaded: {games_loaded} · Players loaded: {players_loaded}
                </div>
                """
            )
            action_form_html = dedent(
                """
                <form action="/refresh" method="post" style="margin-top: 16px;">
                    <button type="submit">Refresh NBA Data</button>
                </form>
                """
            )
        else:
            cards_html = ""
            source_label = "Basketball data not loaded"
            cache_action_label = "Load Historical Data"
            if self._is_legacy_schema_error(load_status_message):
                cache_action_label = "Rebuild Historical Data Cache"
            metadata_html = dedent(
                """
                <p class="section-note" style="margin-top: 12px;">
                    Historical NBA/WNBA data must be loaded before trending players can be shown.
                </p>
                """
            )
            action_form_html = dedent(
                f"""
                <form action="/load-history" method="post" style="margin-top: 16px;">
                    <button type="submit">{escape(cache_action_label)}</button>
                </form>
                """
            )

        refresh_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        return dedent(
            f"""
            <!doctype html>
            <html lang="en">
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <title>SIP Trending Players Demo</title>
                <style>
                    :root {{
                        color-scheme: light;
                        --sky: #76d6ff;
                        --sun: #ffd86b;
                        --mint: #7be6b2;
                        --ink: #1f2937;
                        --soft: #5b6472;
                        --panel: rgba(255, 255, 255, 0.92);
                        --border: #d7e3ef;
                        --shadow: 0 18px 45px rgba(20, 31, 53, 0.12);
                    }}
                    * {{ box-sizing: border-box; }}
                    body {{
                        margin: 0;
                        min-height: 100vh;
                        font-family: Arial, Helvetica, sans-serif;
                        color: var(--ink);
                        background:
                            radial-gradient(circle at top left, rgba(118, 214, 255, 0.30), transparent 28%),
                            radial-gradient(circle at top right, rgba(255, 216, 107, 0.30), transparent 24%),
                            linear-gradient(180deg, #f7fbff 0%, #eef6ff 100%);
                    }}
                    .page {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
                    .hero {{
                        background: var(--panel);
                        border: 2px solid var(--border);
                        border-radius: 28px;
                        box-shadow: var(--shadow);
                        padding: 24px;
                        overflow: hidden;
                        position: relative;
                    }}
                    .hero::after {{
                        content: "";
                        position: absolute;
                        inset: auto -40px -50px auto;
                        width: 180px;
                        height: 180px;
                        background: radial-gradient(circle, rgba(123, 230, 178, 0.40), transparent 70%);
                        border-radius: 50%;
                    }}
                    .eyebrow {{
                        display: inline-flex;
                        align-items: center;
                        gap: 8px;
                        padding: 8px 14px;
                        border-radius: 999px;
                        background: #e8f7ff;
                        color: #05658c;
                        font-weight: 700;
                        letter-spacing: 0.04em;
                        text-transform: uppercase;
                        font-size: 0.78rem;
                    }}
                    .title {{ margin: 16px 0 8px; font-size: clamp(2rem, 3vw, 3.25rem); line-height: 1.05; }}
                    .subtitle {{ margin: 0; max-width: 800px; color: var(--soft); font-size: 1.05rem; line-height: 1.55; }}
                    .demo-label {{
                        display: inline-flex;
                        margin-top: 16px;
                        padding: 8px 12px;
                        border-radius: 999px;
                        background: #fff4c7;
                        border: 1px solid #f3d56d;
                        font-weight: 700;
                    }}
                    .cards {{
                        display: grid;
                        grid-template-columns: repeat(3, minmax(0, 1fr));
                        gap: 18px;
                        margin-top: 22px;
                    }}
                    .player-card {{
                        background: white;
                        border: 2px solid var(--border);
                        border-radius: 24px;
                        box-shadow: 0 14px 30px rgba(20, 31, 53, 0.10);
                        padding: 18px;
                        display: flex;
                        flex-direction: column;
                        gap: 12px;
                        position: relative;
                        min-height: 100%;
                    }}
                    .player-card h2 {{ margin: 0; font-size: 1.35rem; }}
                    .team {{ color: var(--soft); font-weight: 700; margin-top: -4px; }}
                    .badge {{
                        display: inline-flex;
                        align-items: center;
                        gap: 8px;
                        align-self: flex-start;
                        padding: 7px 12px;
                        border-radius: 999px;
                        font-size: 0.84rem;
                        font-weight: 700;
                        text-transform: capitalize;
                    }}
                    .badge::before {{
                        content: "";
                        width: 10px;
                        height: 10px;
                        border-radius: 50%;
                        background: currentColor;
                    }}
                    .rising {{ background: #e7fff2; color: #0d7d46; }}
                    .stable {{ background: #eef4ff; color: #2352c6; }}
                    .declining {{ background: #fff0f0; color: #b11d1d; }}
                    .stat-grid {{ display: grid; gap: 8px; }}
                    .stat {{
                        display: flex;
                        justify-content: space-between;
                        gap: 12px;
                        padding: 10px 12px;
                        border-radius: 14px;
                        background: #f8fbff;
                        border: 1px solid #e4edf7;
                    }}
                    .stat span:first-child {{ color: var(--soft); }}
                    .stat span:last-child {{ font-weight: 700; }}
                    .why {{
                        margin: 0;
                        padding: 12px;
                        border-radius: 16px;
                        background: #fff9e8;
                        border: 1px solid #f4dd8c;
                        line-height: 1.45;
                    }}
                    .refresh {{ font-size: 0.88rem; color: var(--soft); }}
                    .form-shell {{
                        margin-top: 22px;
                        background: var(--panel);
                        border: 2px solid var(--border);
                        border-radius: 28px;
                        box-shadow: var(--shadow);
                        padding: 24px;
                    }}
                    .form-shell h2 {{ margin-top: 0; }}
                    form {{ display: grid; gap: 14px; max-width: 760px; }}
                    label {{ font-weight: 700; }}
                    input, button {{
                        width: 100%;
                        padding: 0.9rem 1rem;
                        border-radius: 14px;
                        border: 1px solid #cfd8e3;
                        font: inherit;
                    }}
                    input {{ background: white; }}
                    select {{
                        width: 100%;
                        padding: 0.75rem;
                        border-radius: 12px;
                        border: 1px solid #cfd8e3;
                        background: white;
                    }}
                    .filter-bar {{
                        max-width: none;
                        grid-template-columns: repeat(4, minmax(0, 1fr));
                        align-items: end;
                    }}
                    .health-grid {{
                        display: grid;
                        grid-template-columns: repeat(4, minmax(0, 1fr));
                        gap: 8px;
                        margin-top: 14px;
                    }}
                    button {{
                        background: linear-gradient(135deg, #2563eb, #0ea5e9);
                        color: white;
                        border: none;
                        font-weight: 700;
                        cursor: pointer;
                    }}
                    button:hover {{ filter: brightness(1.04); }}
                    .hint {{ color: var(--soft); font-size: 0.92rem; margin-top: -6px; }}
                    .two-col {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
                    .section-note {{ margin-top: 10px; color: var(--soft); }}
                    @media (max-width: 980px) {{
                        .cards {{ grid-template-columns: 1fr; }}
                        .two-col {{ grid-template-columns: 1fr; }}
                        .filter-bar, .health-grid {{ grid-template-columns: 1fr; }}
                    }}
                </style>
            </head>
            <body>
                <main class="page">
                    <section class="hero">
                        <div class="eyebrow">SIP Trending Players</div>
                        <h1 class="title">Kid-friendly player radar for curious fans</h1>
                        <p class="subtitle">
                            SIP turns player trends into a bright, easy-to-read story.
                            When cached live data is available, it appears here automatically.
                        </p>
                        <div class="demo-label">{escape(source_label)}</div>
                        {status_html}
                        {health_html}
                        <form action="/" method="get" class="filter-bar" style="margin-top: 16px;">
                            <label>League<select name="league"><option value="">All</option><option value="NBA">NBA</option><option value="WNBA">WNBA</option></select></label>
                            <label>Competition<select name="competition"><option value="">All</option><option value="regular">Regular season</option><option value="playoffs">Playoffs</option><option value="summer_league">Summer League</option></select></label>
                            <label>Explanation<select name="mode"><option value="adult">Standard</option><option value="kids">Explain for kids</option></select></label>
                            <button type="submit">Apply filters</button>
                        </form>
                        {metadata_html}
                        {learning_html}
                        <div class="cards">{cards_html}</div>
                        <div class="refresh">Last refreshed: {escape(refresh_time)}</div>
                        {action_form_html}
                    </section>

                    <section class="form-shell">
                        <h2>Manual Sandbox — user-entered values</h2>
                        <p class="section-note">Keep trying your own player here after the demo cards.</p>

                        <form action="/build" method="post">
                            <div>
                                <label for="name">Player Name</label>
                                <input id="name" name="name" value="Demo Player">
                            </div>

                            <div>
                                <label for="team">Team</label>
                                <input id="team" name="team" value="Demo Team">
                            </div>

                            <div>
                                <label for="performance_history">Performance History</label>
                                <input id="performance_history" name="performance_history" value="10,12,14,16">
                                <div class="hint">Enter numbers separated by commas.</div>
                            </div>

                            <button type="submit">Build Features</button>
                        </form>
                    </section>
                </main>
            </body>
            </html>
            """
        )

    def _get_trending_players(self) -> list[Any]:
        provider = self.trending_player_provider
        if provider is None:
            return []

        try:
            trending_players = provider(
                league=getattr(self, "selected_league", "") or None,
                competition=getattr(
                    self,
                    "selected_competition",
                    "",
                )
                or None,
            )
        except TypeError:
            try:
                trending_players = provider()
            except Exception as error:  # pragma: no cover - integration guard
                self._runtime_provider_error = (
                    f"Could not load historical basketball data. Details: {error}"
                )
                return []
            except TypeError:
                try:
                    trending_players = provider(self)
                except Exception as error:  # pragma: no cover - integration guard
                    self._runtime_provider_error = (
                        f"Could not load historical basketball data. Details: {error}"
                    )
                    return []
        except Exception as error:  # pragma: no cover - integration guard
            self._runtime_provider_error = (
                f"Could not load historical basketball data. Details: {error}"
            )
            return []

        if not trending_players:
            return []

        return list(trending_players)

    @staticmethod
    def _is_legacy_schema_error(message: str | None) -> bool:
        if not message:
            return False
        lowered = message.lower()
        return "no such column: league" in lowered

    def _get_historical_load_status_message(self) -> str | None:
        provider = self.historical_load_status_provider
        if provider is None:
            return None

        try:
            message = provider()
        except TypeError:
            message = provider(self)

        if message is None:
            return None

        text = str(message).strip()
        return text or None

    def _get_historical_load_status_details(self) -> dict[str, Any] | None:
        provider = self.historical_load_status_details_provider
        if provider is None:
            return None

        try:
            details = provider()
        except TypeError:
            details = provider(self)

        if not isinstance(details, dict):
            return None

        return details

    @staticmethod
    def _resolve_source_label(
        load_status_details: dict[str, Any] | None,
        load_status_message: str | None,
    ) -> str:
        if load_status_details and load_status_details.get("success"):
            source_label = str(load_status_details.get("source_label") or "").strip()
            if source_label:
                return source_label

        if load_status_message:
            if "official nba cdn" in load_status_message.lower():
                return "Official NBA CDN"
            if "espn" in load_status_message.lower():
                return "ESPN public web data"

        return "Basketball data loaded"

    def _extract_loaded_counts(
        self,
        load_status_details: dict[str, Any] | None,
        load_status_message: str | None,
        trending_players: list[Any],
    ) -> tuple[int, int]:
        if load_status_details:
            try:
                parsed_games = int(load_status_details.get("games_loaded") or 0)
            except (TypeError, ValueError):
                parsed_games = 0
            try:
                parsed_players = int(
                    load_status_details.get("players_loaded") or len(trending_players)
                )
            except (TypeError, ValueError):
                parsed_players = len(trending_players)

            return parsed_games, parsed_players

        players_loaded = _first_value(
            trending_players[0],
            "players_loaded",
            "loaded_players",
            "player_count",
            default=len(trending_players),
        )
        games_loaded = _first_value(
            trending_players[0],
            "games_loaded",
            "loaded_games",
            "game_count",
            "games_count",
            default=None,
        )

        if games_loaded is None and load_status_message:
            games_match = re.search(
                r"(\d+)\s+games?",
                load_status_message,
                flags=re.IGNORECASE,
            )
            if games_match is not None:
                games_loaded = games_match.group(1)

        if load_status_message:
            players_match = re.search(
                r"(\d+)\s+players?",
                load_status_message,
                flags=re.IGNORECASE,
            )
            if players_match is not None:
                players_loaded = players_match.group(1)

        try:
            parsed_games = int(games_loaded) if games_loaded is not None else 0
        except (TypeError, ValueError):
            parsed_games = 0

        try:
            parsed_players = int(players_loaded)
        except (TypeError, ValueError):
            parsed_players = len(trending_players)

        return parsed_games, parsed_players

    def render_trending_card(self, player: Any, source_label: str) -> str:
        details = _normalize_trending_player(player)
        badge = str(details["badge"])
        player_id = str(details["player_id"])
        title = escape(str(details["name"]))
        if player_id:
            title = f'<a href="/player?player_id={escape(player_id)}">{title}</a>'
        explanation = str(details["why"])
        if getattr(self, "explanation_mode", "adult") == "kids":
            explanation = (
                "SIP compared the newest games with the player's usual "
                f"games. {explanation}"
            )
        return dedent(
            f"""
            <article class="player-card">
                <div>
                    <h2>{title}</h2>
                    <div class="team">{escape(str(details["team"]))}</div>
                </div>
                <div class="demo-label" style="margin-top: 0;">{escape(source_label)}</div>
                <div class="badge {escape(badge)}">{escape(badge)}</div>
                <div class="stat-grid">
                    <div class="stat"><span>Recent 5-game average</span><span>{details["recent_five_average"]}</span></div>
                    <div class="stat"><span>Current-season average</span><span>{details["current_season_average"]}</span></div>
                    <div class="stat"><span>Previous-season average</span><span>{details["previous_season_average"]}</span></div>
                    <div class="stat"><span>Weighted two-season average</span><span>{details["weighted_two_season_average"]}</span></div>
                </div>
                <p class="why"><strong>What SIP discovered:</strong> {escape(explanation)}</p>
                <div class="refresh">Last refreshed: {escape(self._format_refresh_time(details["last_refreshed_at"]))}</div>
            </article>
            """
        )

    def _render_data_health(self) -> str:
        provider = self.data_health_provider
        try:
            health = provider()
        except TypeError:
            health = provider(self)
        if not isinstance(health, dict):
            return ""
        missing = health.get("missing_datasets") or []
        failures = len(health.get("failed_sources") or []) + len(
            health.get("timed_out_sources") or []
        )
        missing_labels = (
            ", ".join(
                f"{item.get('league', '')} {item.get('season', '')} "
                f"{item.get('competition', '')}".strip()
                for item in missing
                if isinstance(item, dict)
            )
            or "None"
        )
        source_issues = (
            ", ".join(
                [
                    *[str(value) for value in health.get("failed_sources") or []],
                    *[
                        f"{value} timed out"
                        for value in health.get("timed_out_sources") or []
                    ],
                ]
            )
            or "None"
        )
        loaded_count = int(health.get("loaded_datasets") or 0)
        expected_count = loaded_count + len(missing)
        readiness = (
            "Complete enough for confident comparisons"
            if health.get("confidence_ready")
            else "Incomplete — conclusions are provisional"
        )
        sources = ", ".join(health.get("sources") or []) or "None"
        return dedent(
            f"""
            <section aria-label="Data health" class="why" style="margin-top: 14px;">
                <strong>Data health: {escape(readiness)}</strong>
                <div class="health-grid">
                    <div class="stat"><span>Datasets loaded</span><span>{loaded_count}</span></div>
                    <div class="stat"><span>Missing datasets</span><span>{len(missing)}</span></div>
                    <div class="stat"><span>Games / Players</span><span>{int(health.get("games") or 0)} / {int(health.get("players") or 0)}</span></div>
                    <div class="stat"><span>Source failures</span><span>{failures}</span></div>
                </div>
                <progress value="{loaded_count}" max="{max(1, expected_count)}" style="width: 100%; margin-top: 10px;">{loaded_count}/{expected_count}</progress>
                <p>Sources: {escape(sources)} · Last successful refresh: {escape(str(health.get("last_successful_refresh") or "Never"))}</p>
                <p>Missing: {escape(missing_labels)}</p>
                <p>Failed or timed-out sources: {escape(source_issues)}</p>
            </section>
            """
        )

    def _render_learning_summary(self) -> str:
        provider = self.learning_summary_provider
        try:
            summary = provider()
        except TypeError:
            summary = provider(self)

        if not isinstance(summary, dict):
            return ""

        evaluated = int(summary.get("evaluated_alerts") or 0)
        if evaluated <= 0:
            return ""

        accuracy = float(summary.get("accuracy") or 0.0) * 100
        avg_confidence = float(summary.get("avg_confidence") or 0.0) * 100
        conf_error = float(summary.get("avg_confidence_error") or 0.0) * 100
        calibration_error = float(summary.get("calibration_error") or 0.0)

        recommendation = "No active recommendation."
        updates = summary.get("hypothesis_updates") or []
        if updates and isinstance(updates[0], dict):
            recommendation = str(
                updates[0].get("recommendation")
                or updates[0].get("status")
                or recommendation
            )

        top_knowledge = ""
        knowledge = summary.get("reusable_knowledge") or []
        if knowledge:
            top_knowledge = str(knowledge[0])

        return dedent(
            f"""
            <section aria-label="Learning intelligence" class="why" style="margin-top: 14px;">
                <strong>Learning intelligence:</strong>
                <div class="health-grid">
                    <div class="stat"><span>Evaluated alerts</span><span>{evaluated}</span></div>
                    <div class="stat"><span>Outcome accuracy</span><span>{accuracy:.0f}%</span></div>
                    <div class="stat"><span>Avg confidence</span><span>{avg_confidence:.0f}%</span></div>
                    <div class="stat"><span>Avg confidence error</span><span>{conf_error:.0f}%</span></div>
                </div>
                <p>Calibration error: {calibration_error:.3f}</p>
                <p><a href="/calibration">View full calibration bucket details →</a></p>
                <p>Top recommendation: {escape(recommendation)}</p>
                <p>Top reusable insight: {escape(top_knowledge or "None yet")}</p>
            </section>
            """
        )

    def render_calibration_detail(self) -> str:
        provider = getattr(
            self,
            "calibration_detail_provider",
            default_calibration_detail_provider,
        )
        try:
            calibration = provider()
        except TypeError:
            calibration = provider(self)
        except Exception:
            calibration = None

        buckets = (
            calibration.get("buckets")
            or calibration.get("calibration_buckets")
            or []
            if isinstance(calibration, dict)
            else []
        )
        bucket_rows = ""
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            predicted = _first_value(
                bucket,
                "predicted",
                "avg_predicted_confidence",
                default=0.0,
            )
            observed = _first_value(
                bucket,
                "observed",
                "empirical_accuracy",
                default=0.0,
            )
            error = _first_value(
                bucket,
                "error",
                "calibration_error",
                default=abs(float(predicted or 0.0) - float(observed or 0.0)),
            )
            details = _first_value(
                bucket,
                "details",
                "description",
                default="",
            )
            bucket_rows += dedent(
                f"""
                <tr>
                    <td>{escape(str(bucket.get("range") or "Unlabelled"))}</td>
                    <td>{escape(str(bucket.get("count") or 0))}</td>
                    <td>{float(predicted or 0.0):.1%}</td>
                    <td>{float(observed or 0.0):.1%}</td>
                    <td>{float(error or 0.0):.3f}</td>
                    <td>{escape(str(details))}</td>
                </tr>
                """
            )

        if bucket_rows:
            overall_error = float(
                _first_value(
                    calibration,
                    "calibration_error",
                    "overall_calibration_error",
                    default=0.0,
                )
                or 0.0
            )
            content = dedent(
                f"""
                <h1>Confidence calibration drill-down</h1>
                <p class="summary">
                    Overall calibration error: <strong>{overall_error:.3f}</strong>.
                    Each bucket compares SIP's predicted confidence with what
                    happened later.
                </p>
                <div class="table-shell">
                    <table>
                        <thead><tr><th>Confidence range</th><th>Count</th><th>Predicted</th><th>Observed</th><th>Error</th><th>Details</th></tr></thead>
                        <tbody>{bucket_rows}</tbody>
                    </table>
                </div>
                """
            )
        else:
            content = dedent(
                """
                <h1>Confidence calibration drill-down</h1>
                <div class="empty">
                    <h2>No calibration buckets yet</h2>
                    <p>SIP needs evaluated alerts before it can compare predicted confidence with real outcomes.</p>
                </div>
                """
            )

        return dedent(
            f"""
            <!doctype html><html lang="en"><head>
            <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
            <title>SIP Calibration Details</title>
            <style>
            body{{font-family:Arial,sans-serif;margin:auto;max-width:1100px;padding:24px;background:#f7fbff;color:#1f2937}}
            table{{width:100%;border-collapse:collapse;background:white}}td,th{{padding:12px;border-bottom:1px solid #ddd;text-align:left}}
            .table-shell{{overflow-x:auto;border-radius:14px;border:1px solid #d7e3ef}}.summary,.empty{{padding:16px;background:#fff9e8;border-radius:14px}}
            </style></head><body><a href="/">← Back to players</a>{content}</body></html>
            """
        )

    def render_player_detail(self, player_id: str) -> str:
        provider = self.player_detail_provider
        try:
            detail = provider(player_id)
        except TypeError:
            detail = provider(self, player_id)
        if not detail:
            content = (
                "<h1>Player details are not available yet</h1>"
                "<p>Load real game data, then try this player again.</p>"
            )
        else:
            hypothesis = self._get_top_hypothesis(player_id, detail)
            intelligence = detail.get("intelligence")
            profiles = getattr(intelligence, "profiles", {})
            comparisons = "".join(
                f"<li>{escape(name)}: {profile.baseline.points:.1f} PPG, "
                f"{profile.baseline.minutes:.1f} minutes, consistency "
                f"{profile.volatility.consistency_score:.0f}/100</li>"
                for name, profile in profiles.items()
            )
            history = "".join(
                f"<tr><td>{escape(str(game.get('game_date') or ''))}</td>"
                f"<td>{escape(str(game.get('team_abbreviation') or ''))}</td>"
                f"<td>{escape(str(game.get('pts') or 0))}</td>"
                f"<td>{escape(str(game.get('minutes') or 0))}</td></tr>"
                for game in detail.get("games") or []
            )
            hypothesis_html = self._render_top_hypothesis(hypothesis)
            content = dedent(
                f"""
                <h1>{escape(str(detail.get("player_name") or "Player"))}</h1>
                <p class="why"><strong>SIP explanation:</strong> {escape(str(detail.get("explanation") or ""))}</p>
                {hypothesis_html}
                <h2>Current, previous, and three-season comparisons</h2>
                <ul>{comparisons}</ul>
                <h2>Game-by-game history and trend evidence</h2>
                <table><tr><th>Date</th><th>Team</th><th>Points</th><th>Minutes</th></tr>{history}</table>
                """
            )
        return dedent(
            f"""
            <!doctype html><html lang="en"><head>
            <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
            <title>SIP Player Detail</title>
            <style>body{{font-family:Arial,sans-serif;margin:auto;max-width:900px;padding:24px;background:#f7fbff}}table{{width:100%;border-collapse:collapse}}td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}.why{{padding:14px;background:#fff9e8;border-radius:14px}}</style>
            </head><body><a href="/">← Back to players</a>{content}</body></html>
            """
        )

    def _get_top_hypothesis(
        self,
        player_id: str,
        detail: dict[str, Any],
    ) -> dict[str, Any] | None:
        embedded = (
            detail.get("top_hypothesis_update")
            or detail.get("hypothesis_update")
            or detail.get("top_hypothesis")
        )
        if isinstance(embedded, dict):
            return embedded

        provider = getattr(
            self,
            "top_hypothesis_provider",
            default_top_hypothesis_provider,
        )
        try:
            hypothesis = provider(player_id)
        except TypeError:
            hypothesis = provider(self, player_id)
        except Exception:
            return None
        return hypothesis if isinstance(hypothesis, dict) else None

    @staticmethod
    def _render_top_hypothesis(hypothesis: dict[str, Any] | None) -> str:
        if not hypothesis:
            return ""

        title = _first_value(
            hypothesis,
            "hypothesis",
            "title",
            default="Current player hypothesis",
        )
        status = _first_value(hypothesis, "status", default="pending")
        support = _first_value(
            hypothesis,
            "support_rate",
            "support",
            default=None,
        )
        recommendation = _first_value(
            hypothesis,
            "recommendation",
            default="Keep collecting evidence.",
        )
        if isinstance(support, (int, float)):
            support_text = f"{float(support):.0%}"
        else:
            support_text = str(support) if support is not None else "Not measured yet"

        return dedent(
            f"""
            <section class="why" aria-label="Top hypothesis update">
                <h2 style="margin-top:0">Top Hypothesis update</h2>
                <p><strong>{escape(str(title))}</strong></p>
                <p>Status: {escape(str(status))} · Evidence support: {escape(support_text)}</p>
                <p>Recommendation: {escape(str(recommendation))}</p>
            </section>
            """
        )

    def _format_refresh_time(self, value: Any) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc)
            return value.strftime("%Y-%m-%d %H:%M UTC")

        return str(value)

    def render_result(self, player: Player, view: object) -> str:
        trend = getattr(view, "trend", None)
        trend_data = ""
        if trend is not None:
            trend_data = (
                "<ul>"
                + "".join(
                    f"<li>{escape(str(key))}: {escape(str(value))}</li>"
                    for key, value in getattr(trend, "__dict__", {}).items()
                )
                + "</ul>"
            )

        return f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Feature Result</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 2rem; background: #f4f7fb; color: #222; }}
                .card {{ max-width: 700px; margin: auto; background: white; padding: 2rem; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.08); }}
                a {{ color: #2563eb; text-decoration: none; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Feature Output</h1>
                <p><strong>Player:</strong> {escape(player.name)} ({escape(player.team)})</p>
                <p><strong>Trend Feature:</strong></p>
                {trend_data}
                <p><a href="/">← Back</a></p>
            </div>
        </body>
        </html>
        """


def main() -> None:
    runtime = BasketballDemoRuntime()

    class ConfiguredFeatureUIHandler(FeatureUIHandler):
        pass

    ConfiguredFeatureUIHandler.trending_player_provider = staticmethod(
        runtime.get_trending_players
    )
    ConfiguredFeatureUIHandler.historical_load_callback = staticmethod(
        runtime.load_history
    )
    ConfiguredFeatureUIHandler.load_history_callback = staticmethod(
        runtime.load_history
    )
    ConfiguredFeatureUIHandler.historical_load_status_provider = staticmethod(
        runtime.get_load_status_message
    )
    ConfiguredFeatureUIHandler.historical_load_status_details_provider = staticmethod(
        runtime.get_load_status_details
    )
    ConfiguredFeatureUIHandler.player_detail_provider = staticmethod(
        getattr(
            runtime,
            "get_player_detail",
            default_player_detail_provider,
        )
    )
    ConfiguredFeatureUIHandler.data_health_provider = staticmethod(
        getattr(
            runtime,
            "get_data_health",
            default_data_health_provider,
        )
    )
    ConfiguredFeatureUIHandler.learning_summary_provider = staticmethod(
        getattr(
            runtime,
            "get_learning_summary",
            default_learning_summary_provider,
        )
    )
    ConfiguredFeatureUIHandler.calibration_detail_provider = staticmethod(
        getattr(
            runtime,
            "get_calibration_details",
            default_calibration_detail_provider,
        )
    )
    ConfiguredFeatureUIHandler.top_hypothesis_provider = staticmethod(
        getattr(
            runtime,
            "get_top_hypothesis_update",
            default_top_hypothesis_provider,
        )
    )
    ConfiguredFeatureUIHandler.refresh_callback = staticmethod(runtime.refresh)

    server = ThreadingHTTPServer(
        ("0.0.0.0", 8000),
        ConfiguredFeatureUIHandler,
    )
    print("Serving at http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__":
    main()
