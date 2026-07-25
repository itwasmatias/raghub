from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import re
from textwrap import dedent
from urllib.parse import parse_qs, urlparse
from typing import Any

from models.sports.player import Player
from services.retrieve import RetrievalService
from services.search import SearchService
from sports.application.production_runtime import (
    ApplicationRuntime,
    ProductionBasketballRuntime,
    build_production_runtime,
)

# Compatibility seam for existing launch wrappers and tests. Production paths
# construct the runtime through build_production_runtime().
BasketballDemoRuntime = ProductionBasketballRuntime
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


def default_dashboard_snapshot_provider() -> dict[str, Any] | None:
    return None


def default_betting_assessment_provider() -> list[Any]:
    return []


def default_betting_board_provider() -> dict[str, Any] | None:
    return None


def default_betting_replay_provider() -> dict[str, Any] | None:
    return None


def build_configured_feature_ui_handler(
    runtime: ApplicationRuntime,
) -> type[FeatureUIHandler]:
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
        getattr(runtime, "get_player_detail", default_player_detail_provider)
    )
    ConfiguredFeatureUIHandler.data_health_provider = staticmethod(
        getattr(runtime, "get_data_health", default_data_health_provider)
    )
    ConfiguredFeatureUIHandler.learning_summary_provider = staticmethod(
        getattr(runtime, "get_learning_summary", default_learning_summary_provider)
    )
    ConfiguredFeatureUIHandler.calibration_detail_provider = staticmethod(
        getattr(runtime, "get_calibration_details", default_calibration_detail_provider)
    )
    ConfiguredFeatureUIHandler.top_hypothesis_provider = staticmethod(
        getattr(runtime, "get_top_hypothesis_update", default_top_hypothesis_provider)
    )
    ConfiguredFeatureUIHandler.dashboard_snapshot_provider = staticmethod(
        getattr(runtime, "get_dashboard_snapshot", default_dashboard_snapshot_provider)
    )
    ConfiguredFeatureUIHandler.betting_assessment_provider = staticmethod(
        getattr(
            runtime,
            "get_betting_assessments",
            default_betting_assessment_provider,
        )
    )
    ConfiguredFeatureUIHandler.betting_board_provider = staticmethod(
        getattr(
            runtime,
            "get_betting_board",
            default_betting_board_provider,
        )
    )
    ConfiguredFeatureUIHandler.betting_replay_provider = staticmethod(
        getattr(
            runtime,
            "get_betting_replay_board",
            default_betting_replay_provider,
        )
    )
    ConfiguredFeatureUIHandler.refresh_callback = staticmethod(runtime.refresh)
    return ConfiguredFeatureUIHandler


def create_feature_ui_instance(
    runtime: ApplicationRuntime,
    *,
    query_params: dict[str, Any] | None = None,
) -> FeatureUIHandler:
    handler_class = build_configured_feature_ui_handler(runtime)
    handler = object.__new__(handler_class)
    query_params = query_params or {}
    handler.selected_league = str(query_params.get("league") or "")
    handler.selected_competition = str(query_params.get("competition") or "")
    handler.explanation_mode = str(query_params.get("mode") or "adult")
    handler.research_query = str(query_params.get("research_q") or "").strip()
    providers_value = query_params.get("providers") or ""
    providers_text = (
        ",".join(str(item) for item in providers_value)
        if isinstance(providers_value, (list, tuple))
        else str(providers_value)
    )
    handler.selected_research_providers = [
        value.strip().lower() for value in providers_text.split(",") if value.strip()
    ]
    return handler


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
    dashboard_snapshot_provider = staticmethod(default_dashboard_snapshot_provider)
    betting_assessment_provider = staticmethod(default_betting_assessment_provider)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self.selected_league = query.get("league", [""])[0]
        self.selected_competition = query.get("competition", [""])[0]
        self.explanation_mode = query.get("mode", ["adult"])[0]
        self.research_query = query.get("research_q", [""])[0].strip()
        self.selected_research_providers = [
            value.strip().lower()
            for value in query.get("providers", [""])[0].split(",")
            if value.strip()
        ]
        if parsed.path == "/":
            self.send_html(self.render_form())
            return
        if parsed.path == "/player":
            self.send_html(self.render_player_detail(query.get("player_id", [""])[0]))
            return
        if parsed.path == "/calibration":
            self.send_html(self.render_calibration_detail())
            return
        if parsed.path == "/betting":
            self.send_html(self.render_betting_intelligence())
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
        dashboard_snapshot = self._build_dashboard_snapshot(
            trending_players,
            load_status_message,
            load_status_details,
        )
        research_html = self._render_research_console(trending_players)
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
                <title>SIP Sports Intelligence Platform</title>
                <style>
                    :root {{
                        color-scheme: dark;
                        --bg: #070b14;
                        --bg-alt: #0d1321;
                        --panel: rgba(10, 17, 30, 0.92);
                        --panel-strong: #0f1729;
                        --panel-soft: #111c31;
                        --border: rgba(124, 144, 179, 0.18);
                        --text: #eef4ff;
                        --muted: #96a8c7;
                        --green: #27d17f;
                        --amber: #f4bf41;
                        --red: #ff6f7a;
                        --blue: #64b5ff;
                        --purple: #8a7cff;
                        --shadow: 0 22px 50px rgba(0, 0, 0, 0.32);
                        --radius-lg: 26px;
                        --radius-md: 18px;
                        --radius-sm: 14px;
                    }}
                    * {{ box-sizing: border-box; }}
                    body {{
                        margin: 0;
                        min-height: 100vh;
                        font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
                        color: var(--text);
                        background:
                            radial-gradient(circle at top left, rgba(100, 181, 255, 0.12), transparent 24%),
                            radial-gradient(circle at top right, rgba(138, 124, 255, 0.16), transparent 22%),
                            linear-gradient(180deg, #050913 0%, #0a1020 45%, #09111f 100%);
                    }}
                    a {{ color: inherit; text-decoration: none; }}
                    .app-shell {{ display: grid; grid-template-columns: 260px minmax(0, 1fr); min-height: 100vh; }}
                    .sidebar {{
                        padding: 24px 18px;
                        border-right: 1px solid var(--border);
                        background: linear-gradient(180deg, rgba(9, 14, 26, 0.98), rgba(8, 12, 23, 0.92));
                        position: sticky;
                        top: 0;
                        height: 100vh;
                        overflow-y: auto;
                    }}
                    .brand {{ display: flex; align-items: center; gap: 12px; margin-bottom: 26px; }}
                    .brand-mark {{
                        width: 42px;
                        height: 42px;
                        border-radius: 12px;
                        background: linear-gradient(135deg, #8a7cff, #3f7cff 65%, #3de1a2);
                        box-shadow: 0 12px 24px rgba(63, 124, 255, 0.28);
                    }}
                    .brand-name {{ font-size: 1.7rem; font-weight: 800; letter-spacing: 0.02em; }}
                    .brand-subtitle {{ color: var(--muted); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.18em; }}
                    .sidebar-group {{ margin-top: 18px; }}
                    .sidebar-group h2 {{ margin: 0 0 10px; color: var(--muted); font-size: 0.76rem; letter-spacing: 0.16em; text-transform: uppercase; }}
                    .nav-item {{
                        display: flex;
                        align-items: center;
                        justify-content: space-between;
                        gap: 12px;
                        padding: 10px 12px;
                        border-radius: 12px;
                        color: #dce6fb;
                        margin-bottom: 6px;
                        background: transparent;
                    }}
                    .nav-item.active {{ background: rgba(100, 181, 255, 0.18); border: 1px solid rgba(100, 181, 255, 0.24); }}
                    .nav-item span:last-child {{ color: var(--muted); font-size: 0.85rem; }}
                    .sidebar-footer {{
                        margin-top: 28px;
                        padding: 14px;
                        border-radius: var(--radius-md);
                        border: 1px solid var(--border);
                        background: rgba(18, 28, 48, 0.85);
                    }}
                    .sidebar-footer strong {{ display: block; margin-bottom: 4px; }}
                    .main {{ padding: 20px 22px 32px; }}
                    .topbar {{
                        display: grid;
                        grid-template-columns: 1fr minmax(260px, 560px) auto;
                        gap: 16px;
                        align-items: center;
                        margin-bottom: 16px;
                    }}
                    .season-chip, .status-pill, .subtle-chip, .demo-label {{
                        display: inline-flex;
                        align-items: center;
                        gap: 8px;
                        padding: 8px 12px;
                        border-radius: 999px;
                        border: 1px solid var(--border);
                        background: rgba(16, 25, 43, 0.86);
                        color: #dfe8fb;
                        font-weight: 700;
                    }}
                    .search-bar {{
                        display: flex;
                        align-items: center;
                        gap: 10px;
                        padding: 12px 16px;
                        border-radius: 16px;
                        background: rgba(10, 17, 30, 0.92);
                        border: 1px solid var(--border);
                    }}
                    .search-bar input {{
                        width: 100%;
                        border: none;
                        background: transparent;
                        color: var(--text);
                        outline: none;
                        padding: 0;
                    }}
                    .status-row {{ display: flex; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }}
                    .status-pill.good {{ color: #97ffc4; }}
                    .status-pill.info {{ color: #c8d8ff; }}
                    .page {{ display: grid; gap: 16px; }}
                    .welcome-card {{
                        background: linear-gradient(180deg, rgba(11, 18, 32, 0.94), rgba(9, 15, 28, 0.98));
                        border: 1px solid var(--border);
                        border-radius: var(--radius-lg);
                        box-shadow: var(--shadow);
                        padding: 24px;
                    }}
                    .welcome-grid {{ display: grid; grid-template-columns: 1.4fr 1fr; gap: 18px; align-items: start; }}
                    .title {{ margin: 10px 0 8px; font-size: clamp(2rem, 3vw, 3.1rem); line-height: 1.02; }}
                    .subtitle {{ margin: 0; max-width: 760px; color: var(--muted); font-size: 1rem; line-height: 1.6; }}
                    .metric-strip {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
                    .metric-card, .panel, .player-card, .form-shell {{
                        background: linear-gradient(180deg, rgba(14, 22, 38, 0.98), rgba(10, 17, 30, 0.98));
                        border: 1px solid var(--border);
                        border-radius: var(--radius-md);
                        box-shadow: var(--shadow);
                    }}
                    .metric-card {{ padding: 16px; min-height: 112px; }}
                    .metric-card .label {{ color: var(--muted); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.08em; }}
                    .metric-card .value {{ font-size: 2rem; font-weight: 800; margin: 10px 0 2px; }}
                    .metric-card .delta.good {{ color: var(--green); }}
                    .metric-card .delta.warn {{ color: var(--amber); }}
                    .metric-card .delta.bad {{ color: var(--red); }}
                    .cards {{
                        display: grid;
                        grid-template-columns: repeat(3, minmax(0, 1fr));
                        gap: 16px;
                    }}
                    .player-card {{
                        padding: 18px;
                        display: flex;
                        flex-direction: column;
                        gap: 12px;
                    }}
                    .player-card h2 {{ margin: 0; font-size: 1.35rem; }}
                    .team {{ color: var(--muted); font-weight: 700; margin-top: -4px; }}
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
                        border: 1px solid transparent;
                    }}
                    .badge::before {{
                        content: "";
                        width: 10px;
                        height: 10px;
                        border-radius: 50%;
                        background: currentColor;
                    }}
                    .rising {{ background: rgba(39, 209, 127, 0.16); color: #8dffc0; border-color: rgba(39, 209, 127, 0.28); }}
                    .stable {{ background: rgba(100, 181, 255, 0.14); color: #b4d5ff; border-color: rgba(100, 181, 255, 0.25); }}
                    .declining {{ background: rgba(255, 111, 122, 0.14); color: #ffb7bd; border-color: rgba(255, 111, 122, 0.22); }}
                    .stat-grid {{ display: grid; gap: 8px; }}
                    .stat {{
                        display: flex;
                        justify-content: space-between;
                        gap: 12px;
                        padding: 10px 12px;
                        border-radius: 14px;
                        background: rgba(16, 25, 43, 0.92);
                        border: 1px solid var(--border);
                    }}
                    .stat span:first-child {{ color: var(--muted); }}
                    .stat span:last-child {{ font-weight: 700; }}
                    .why {{
                        margin: 0;
                        padding: 12px;
                        border-radius: 16px;
                        background: rgba(244, 191, 65, 0.10);
                        border: 1px solid rgba(244, 191, 65, 0.20);
                        line-height: 1.45;
                    }}
                    .refresh {{ font-size: 0.88rem; color: var(--muted); }}
                    .form-shell {{
                        padding: 24px;
                    }}
                    .form-shell h2 {{ margin-top: 0; }}
                    form {{ display: grid; gap: 14px; max-width: 760px; }}
                    label {{ font-weight: 700; }}
                    input, button {{
                        width: 100%;
                        padding: 0.9rem 1rem;
                        border-radius: 14px;
                        border: 1px solid var(--border);
                        font: inherit;
                    }}
                    input {{ background: rgba(8, 14, 27, 0.9); color: var(--text); }}
                    select {{
                        width: 100%;
                        padding: 0.75rem;
                        border-radius: 12px;
                        border: 1px solid var(--border);
                        background: rgba(8, 14, 27, 0.9);
                        color: var(--text);
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
                        background: linear-gradient(135deg, #3c76ff, #2ccaa0);
                        color: white;
                        border: none;
                        font-weight: 700;
                        cursor: pointer;
                    }}
                    button:hover {{ filter: brightness(1.04); }}
                    .hint {{ color: var(--muted); font-size: 0.92rem; margin-top: -6px; }}
                    .two-col {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
                    .section-note {{ margin-top: 10px; color: var(--muted); }}
                    .dashboard-grid {{ display: grid; grid-template-columns: 1.3fr 1fr 0.95fr; gap: 16px; }}
                    .lower-grid {{ display: grid; grid-template-columns: 1.1fr 1fr 0.95fr 0.8fr; gap: 16px; }}
                    .panel {{ padding: 18px; overflow: hidden; }}
                    .panel h2 {{ margin: 0 0 14px; font-size: 1rem; letter-spacing: 0.04em; text-transform: uppercase; color: #dfe8fb; }}
                    .panel-header {{ display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 12px; }}
                    .panel-header a, .panel-header span {{ color: var(--blue); font-size: 0.86rem; }}
                    .featured-matchup {{ min-height: 270px; }}
                    .matchup-teams {{ display: grid; grid-template-columns: 1fr auto 1fr; gap: 14px; align-items: center; margin: 20px 0 14px; }}
                    .team-block {{ padding: 14px; border-radius: 18px; background: rgba(16, 25, 43, 0.85); border: 1px solid var(--border); }}
                    .team-code {{ font-size: 2rem; font-weight: 800; }}
                    .team-record {{ color: var(--muted); margin-top: 6px; }}
                    .matchup-center {{ color: var(--muted); font-weight: 700; text-transform: uppercase; letter-spacing: 0.12em; }}
                    .probability-track {{ height: 18px; border-radius: 999px; overflow: hidden; background: rgba(14, 21, 38, 0.95); border: 1px solid var(--border); display: grid; grid-template-columns: var(--left-prob, 50%) 1fr; }}
                    .probability-left {{ background: linear-gradient(90deg, #ffcf57, #f4bf41); }}
                    .probability-right {{ background: linear-gradient(90deg, #2bc19a, #27d17f); }}
                    .probability-labels {{ display: flex; justify-content: space-between; margin-top: 8px; color: var(--muted); font-size: 0.88rem; }}
                    .button-row {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }}
                    .button-row .subtle-button {{ flex: 1 1 150px; text-align: center; padding: 11px 14px; border-radius: 14px; border: 1px solid var(--border); background: rgba(16, 25, 43, 0.92); font-weight: 700; }}
                    .spotlight-player {{ display: grid; grid-template-columns: 130px 1fr; gap: 16px; align-items: center; }}
                    .player-avatar {{
                        width: 130px;
                        height: 160px;
                        border-radius: 24px;
                        background: linear-gradient(180deg, rgba(100, 181, 255, 0.24), rgba(138, 124, 255, 0.16));
                        border: 1px solid var(--border);
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        font-size: 3rem;
                        font-weight: 800;
                    }}
                    .spotlight-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }}
                    .ring-wrap {{ display: flex; align-items: center; justify-content: center; margin-top: 8px; }}
                    .ring {{
                        width: 96px;
                        height: 96px;
                        border-radius: 50%;
                        display: grid;
                        place-items: center;
                        background: conic-gradient(var(--green) calc(var(--pct, 0.5) * 1turn), rgba(19, 30, 50, 0.95) 0turn);
                    }}
                    .ring::before {{ content: ""; width: 70px; height: 70px; border-radius: 50%; background: var(--panel-strong); border: 1px solid var(--border); }}
                    .ring-value {{ position: absolute; font-size: 1.4rem; font-weight: 800; }}
                    .news-list, .watch-list, .hypothesis-list, .upcoming-list, .research-list {{ display: grid; gap: 12px; }}
                    .news-item, .watch-item, .hypothesis-item, .upcoming-item, .research-item {{ padding: 12px; border-radius: 14px; background: rgba(15, 24, 41, 0.88); border: 1px solid var(--border); }}
                    .news-item strong, .watch-item strong, .hypothesis-item strong, .upcoming-item strong {{ display: block; margin-bottom: 4px; }}
                    .meta-row {{ display: flex; gap: 10px; flex-wrap: wrap; color: var(--muted); font-size: 0.86rem; }}
                    .pill {{ display: inline-flex; padding: 4px 8px; border-radius: 999px; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.03em; }}
                    .pill.good {{ background: rgba(39, 209, 127, 0.14); color: #8dffc0; }}
                    .pill.warn {{ background: rgba(244, 191, 65, 0.14); color: #ffd98b; }}
                    .pill.bad {{ background: rgba(255, 111, 122, 0.14); color: #ffb7bd; }}
                    .pill.info {{ background: rgba(100, 181, 255, 0.14); color: #b6d6ff; }}
                    .trend-lines {{ display: grid; gap: 10px; }}
                    .trend-row {{ display: grid; grid-template-columns: 60px 56px 1fr; gap: 10px; align-items: center; }}
                    .sparkline {{
                        height: 8px;
                        border-radius: 999px;
                        background: linear-gradient(90deg, rgba(100, 181, 255, 0.2), rgba(39, 209, 127, 0.45));
                        position: relative;
                        overflow: hidden;
                    }}
                    .sparkline::after {{
                        content: "";
                        position: absolute;
                        inset: 0;
                        background: radial-gradient(circle at var(--marker, 70%) 50%, #27d17f 0 2px, transparent 3px);
                    }}
                    .mini-table {{ display: grid; gap: 10px; }}
                    .table-row {{ display: grid; grid-template-columns: 1.15fr 0.7fr 0.8fr; gap: 10px; align-items: center; padding: 10px 0; border-bottom: 1px solid rgba(124, 144, 179, 0.12); }}
                    .table-row:last-child {{ border-bottom: none; }}
                    .confidence-grid {{ display: grid; gap: 10px; margin-top: 12px; }}
                    .confidence-row {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; }}
                    .bar {{ height: 8px; border-radius: 999px; overflow: hidden; background: rgba(15, 24, 41, 0.95); border: 1px solid var(--border); }}
                    .bar > span {{ display: block; height: 100%; background: linear-gradient(90deg, #f4bf41, #27d17f); }}
                    .research-form {{ max-width: none; grid-template-columns: 1.35fr 1fr auto; align-items: end; }}
                    .research-empty {{ color: var(--muted); }}
                    .research-providers {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }}
                    .research-providers .subtle-chip {{ font-size: 0.82rem; }}
                    progress {{ accent-color: #27d17f; }}
                    @media (max-width: 980px) {{
                        .app-shell {{ grid-template-columns: 1fr; }}
                        .sidebar {{ position: static; height: auto; border-right: none; border-bottom: 1px solid var(--border); }}
                        .topbar, .welcome-grid, .dashboard-grid, .lower-grid, .metric-strip {{ grid-template-columns: 1fr; }}
                        .cards {{ grid-template-columns: 1fr; }}
                        .two-col {{ grid-template-columns: 1fr; }}
                        .filter-bar, .health-grid {{ grid-template-columns: 1fr; }}
                        .spotlight-player {{ grid-template-columns: 1fr; }}
                        .matchup-teams, .research-form {{ grid-template-columns: 1fr; }}
                    }}
                </style>
            </head>
            <body>
                <div class="app-shell">
                    <aside class="sidebar">
                        <div class="brand">
                            <div class="brand-mark"></div>
                            <div>
                                <div class="brand-name">SIP</div>
                                <div class="brand-subtitle">Sports Intelligence Platform</div>
                            </div>
                        </div>
                        {self._render_sidebar(dashboard_snapshot)}
                        <div class="sidebar-footer">
                            <strong>Matias R.</strong>
                            <div class="refresh">Premium Plan · Live runtime enabled</div>
                        </div>
                    </aside>
                    <main class="main">
                        <div class="topbar">
                            <div class="season-chip">NBA 2024-25 · SIP Ops</div>
                            <form action="/" method="get" class="search-bar" style="max-width: none;">
                                <input type="hidden" name="league" value="{escape(getattr(self, "selected_league", "") or "")}">
                                <input type="hidden" name="competition" value="{escape(getattr(self, "selected_competition", "") or "")}">
                                <input type="hidden" name="mode" value="{escape(getattr(self, "explanation_mode", "adult") or "adult")}">
                                <span>Search players, teams, matchups, news...</span>
                                <input name="research_q" value="{escape(getattr(self, "research_query", "") or "")}" placeholder="Try Nikola Jokic trends, Celtics matchup outlook, injury rotation impact...">
                            </form>
                            <div class="status-row">
                                <div class="status-pill good">System Status · {escape(self._system_status_label(dashboard_snapshot))}</div>
                                <div class="status-pill info">SIP Agent · Active</div>
                            </div>
                        </div>

                        <div class="page">
                            <section class="welcome-card">
                                <div class="welcome-grid">
                                    <div>
                                        <div class="demo-label">{escape(source_label)}</div>
                                        <h1 class="title">Welcome back, Matias.</h1>
                                        <p class="subtitle">
                                            Your intelligence edge for smarter sports decisions. This dashboard now blends live basketball runtime data,
                                            SIP learning signals, and the repo's current retrieval APIs into one control surface.
                                        </p>
                                        {status_html}
                                        {metadata_html}
                                    </div>
                                    <div class="metric-strip">{self._render_metric_cards(dashboard_snapshot)}</div>
                                </div>
                                {health_html}
                                {learning_html}
                                <form action="/" method="get" class="filter-bar" style="margin-top: 16px; max-width: none;">
                                    <label>League<select name="league"><option value="">All</option><option value="NBA"{" selected" if getattr(self, "selected_league", "") == "NBA" else ""}>NBA</option><option value="WNBA"{" selected" if getattr(self, "selected_league", "") == "WNBA" else ""}>WNBA</option></select></label>
                                    <label>Competition<select name="competition"><option value="">All</option><option value="regular"{" selected" if getattr(self, "selected_competition", "") == "regular" else ""}>Regular season</option><option value="playoffs"{" selected" if getattr(self, "selected_competition", "") == "playoffs" else ""}>Playoffs</option><option value="summer_league"{" selected" if getattr(self, "selected_competition", "") == "summer_league" else ""}>Summer League</option></select></label>
                                    <label>Explanation<select name="mode"><option value="adult"{" selected" if getattr(self, "explanation_mode", "adult") == "adult" else ""}>Standard</option><option value="kids"{" selected" if getattr(self, "explanation_mode", "adult") == "kids" else ""}>Explain for kids</option></select></label>
                                    <button type="submit">Apply filters</button>
                                </form>
                            </section>

                            <section class="dashboard-grid">
                                {self._render_featured_matchup_panel(dashboard_snapshot)}
                                {self._render_spotlight_panel(dashboard_snapshot)}
                                {self._render_news_panel(dashboard_snapshot)}
                            </section>

                            <section class="lower-grid">
                                {self._render_watchlist_panel(dashboard_snapshot)}
                                {self._render_team_form_panel(dashboard_snapshot)}
                                {self._render_market_panel(dashboard_snapshot)}
                                {self._render_upcoming_panel(dashboard_snapshot)}
                            </section>

                            <section class="dashboard-grid" style="grid-template-columns: 1fr 1fr 1fr 1fr;">
                                {self._render_hypotheses_panel(dashboard_snapshot)}
                                {self._render_insights_panel(dashboard_snapshot)}
                                {self._render_public_pulse_panel(dashboard_snapshot)}
                                {self._render_confidence_panel(dashboard_snapshot)}
                            </section>

                            <section class="panel">
                                <div class="panel-header"><h2>Trending Players</h2><span>Feature cards</span></div>
                                <div class="cards">{cards_html}</div>
                                <div class="refresh">Last refreshed: {escape(refresh_time)}</div>
                                {action_form_html}
                            </section>

                            {research_html}

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
                        </div>
                    </main>
                </div>
            </body>
            </html>
            """
        )

    def _build_dashboard_snapshot(
        self,
        trending_players: list[Any],
        load_status_message: str | None,
        load_status_details: dict[str, Any] | None,
    ) -> dict[str, Any]:
        provider = getattr(
            self,
            "dashboard_snapshot_provider",
            default_dashboard_snapshot_provider,
        )
        try:
            provided = provider(
                league=getattr(self, "selected_league", "") or None,
                competition=getattr(self, "selected_competition", "") or None,
                research_query=getattr(self, "research_query", "") or None,
            )
        except TypeError:
            try:
                provided = provider()
            except TypeError:
                provided = provider(self)
        except Exception:
            provided = None

        health = self._get_data_health_payload()
        learning = self._get_learning_summary_payload()
        source_label = self._resolve_source_label(
            load_status_details, load_status_message
        )
        normalized = [_normalize_trending_player(player) for player in trending_players]

        if isinstance(provided, dict):
            snapshot = dict(provided)
        else:
            snapshot = {}

        snapshot.setdefault("source_label", source_label)
        snapshot.setdefault("trending_players", normalized)
        snapshot.setdefault("health", health)
        snapshot.setdefault("learning", learning)

        team_form = snapshot.get("team_form")
        if not isinstance(team_form, list) or not team_form:
            team_form = self._derive_team_form(normalized)
            snapshot["team_form"] = team_form

        featured = snapshot.get("featured_matchup")
        if not isinstance(featured, dict):
            featured = self._derive_featured_matchup(team_form, normalized)
            snapshot["featured_matchup"] = featured

        spotlight = snapshot.get("spotlight")
        if not isinstance(spotlight, dict):
            spotlight = self._derive_spotlight(normalized)
            snapshot["spotlight"] = spotlight

        alerts = snapshot.get("alerts")
        if not isinstance(alerts, list) or not alerts:
            alerts = self._derive_alerts(
                normalized, health, learning, load_status_message
            )
            snapshot["alerts"] = alerts

        watchlist = snapshot.get("watchlist")
        if not isinstance(watchlist, list) or not watchlist:
            watchlist = self._derive_watchlist(normalized)
            snapshot["watchlist"] = watchlist

        upcoming_games = snapshot.get("upcoming_games")
        if not isinstance(upcoming_games, list) or not upcoming_games:
            upcoming_games = self._derive_upcoming_games(team_form, featured)
            snapshot["upcoming_games"] = upcoming_games

        market_board = snapshot.get("market_board")
        if not isinstance(market_board, list):
            market_board = []
            snapshot["market_board"] = market_board

        hypotheses = snapshot.get("hypotheses")
        if not isinstance(hypotheses, list) or not hypotheses:
            hypotheses = self._derive_hypotheses(learning)
            snapshot["hypotheses"] = hypotheses

        insights = snapshot.get("insights")
        if not isinstance(insights, list) or not insights:
            insights = self._derive_insights(normalized, health, learning, team_form)
            snapshot["insights"] = insights

        snapshot.setdefault(
            "confidence",
            self._derive_confidence(health, learning, normalized),
        )
        snapshot.setdefault(
            "metrics",
            self._derive_metrics(normalized, learning, snapshot),
        )
        return snapshot

    def _get_data_health_payload(self) -> dict[str, Any]:
        provider = getattr(self, "data_health_provider", default_data_health_provider)
        try:
            payload = provider()
        except TypeError:
            payload = provider(self)
        except Exception:
            payload = None
        return payload if isinstance(payload, dict) else {}

    def _get_learning_summary_payload(self) -> dict[str, Any]:
        provider = getattr(
            self,
            "learning_summary_provider",
            default_learning_summary_provider,
        )
        try:
            payload = provider()
        except TypeError:
            payload = provider(self)
        except Exception:
            payload = None
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _derive_team_form(
        self, normalized: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for player in normalized:
            team = str(player.get("team") or "Unknown Team")
            grouped.setdefault(team, []).append(player)

        rows = []
        for team, players in grouped.items():
            avg_recent = sum(
                self._safe_float(item.get("recent_five_average")) for item in players
            ) / max(1, len(players))
            avg_season = sum(
                self._safe_float(item.get("current_season_average")) for item in players
            ) / max(1, len(players))
            delta = avg_recent - avg_season
            rising_count = sum(
                1 for item in players if str(item.get("badge") or "") == "rising"
            )
            wins = min(10, max(0, int(round(5 + delta / 2 + rising_count))))
            losses = max(0, 10 - wins)
            rows.append(
                {
                    "team": team,
                    "code": self._team_code(team),
                    "record": f"{wins}-{losses}",
                    "trend_score": delta,
                    "rising_players": rising_count,
                    "avg_recent": avg_recent,
                    "avg_season": avg_season,
                    "confidence": max(
                        45, min(89, int(round(56 + delta * 4 + rising_count * 5)))
                    ),
                }
            )
        rows.sort(
            key=lambda item: (item["trend_score"], item["rising_players"]), reverse=True
        )
        return rows[:5]

    def _derive_featured_matchup(
        self,
        team_form: list[dict[str, Any]],
        normalized: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if len(team_form) >= 2:
            left = team_form[0]
            right = team_form[1]
        elif len(normalized) >= 2:
            left = {
                "team": normalized[0]["team"],
                "code": self._team_code(str(normalized[0]["team"])),
                "record": "Tracked",
                "confidence": 52,
            }
            right = {
                "team": normalized[1]["team"],
                "code": self._team_code(str(normalized[1]["team"])),
                "record": "Tracked",
                "confidence": 48,
            }
        else:
            left = {"team": "No Team", "code": "TBD", "record": "0-0", "confidence": 50}
            right = {
                "team": "Awaiting Data",
                "code": "TBD",
                "record": "0-0",
                "confidence": 50,
            }

        left_prob = max(5, min(95, int(left.get("confidence", 50))))
        right_prob = 100 - left_prob
        return {
            "away_code": left.get("code", "TBD"),
            "away_team": left.get("team", "Unknown"),
            "away_record": left.get("record", "0-0"),
            "home_code": right.get("code", "TBD"),
            "home_team": right.get("team", "Unknown"),
            "home_record": right.get("record", "0-0"),
            "tipoff": "Live monitored slate",
            "venue": "SIP derived matchup",
            "away_probability": left_prob,
            "home_probability": right_prob,
        }

    def _derive_spotlight(self, normalized: list[dict[str, Any]]) -> dict[str, Any]:
        if not normalized:
            return {
                "name": "No player loaded",
                "team": "Load historical basketball data",
                "badge": "stable",
                "trend_score": 50,
                "summary": "SIP will surface a live spotlight once current data is cached.",
                "stats": [],
            }

        player = normalized[0]
        recent = self._safe_float(player.get("recent_five_average"))
        season = self._safe_float(player.get("current_season_average"))
        previous = self._safe_float(player.get("previous_season_average"))
        trend_score = max(
            10,
            min(99, int(round(50 + (recent - season) * 6 + (season - previous) * 2))),
        )
        return {
            "name": player.get("name"),
            "team": player.get("team"),
            "badge": player.get("badge"),
            "trend_score": trend_score,
            "summary": player.get("why"),
            "stats": [
                {"label": "PPG", "value": f"{recent:.1f}"},
                {"label": "Season", "value": f"{season:.1f}"},
                {"label": "Prev", "value": f"{previous:.1f}"},
                {"label": "Delta", "value": f"{recent - season:+.1f}"},
            ],
        }

    def _derive_alerts(
        self,
        normalized: list[dict[str, Any]],
        health: dict[str, Any],
        learning: dict[str, Any],
        load_status_message: str | None,
    ) -> list[dict[str, Any]]:
        alerts = []
        for player in normalized[:3]:
            change = self._safe_float(
                player.get("recent_five_average")
            ) - self._safe_float(player.get("current_season_average"))
            severity = "good" if change >= 2 else "warn" if change >= 0 else "bad"
            alerts.append(
                {
                    "title": f"{player.get('name')} trending {player.get('badge')}",
                    "body": player.get("why")
                    or "SIP detected a meaningful recent shift.",
                    "meta": f"{player.get('team')} · {change:+.1f} vs season average",
                    "severity": severity,
                }
            )

        recommendation = ""
        updates = learning.get("hypothesis_updates") or []
        if updates and isinstance(updates[0], dict):
            recommendation = str(updates[0].get("recommendation") or "")
        if recommendation:
            alerts.append(
                {
                    "title": "Model learning update",
                    "body": recommendation,
                    "meta": f"Evaluated alerts: {int(learning.get('evaluated_alerts') or 0)}",
                    "severity": "info",
                }
            )

        if load_status_message:
            alerts.append(
                {
                    "title": "Runtime status",
                    "body": load_status_message,
                    "meta": f"Sources: {', '.join(health.get('sources') or []) or 'Unknown'}",
                    "severity": "warn" if health.get("missing_datasets") else "good",
                }
            )
        return alerts[:4]

    def _derive_watchlist(
        self, normalized: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        watchlist = []
        for player in normalized[:4]:
            delta = self._safe_float(
                player.get("recent_five_average")
            ) - self._safe_float(player.get("current_season_average"))
            watchlist.append(
                {
                    "name": player.get("name"),
                    "team": player.get("team"),
                    "tag": str(player.get("badge") or "stable").upper(),
                    "summary": player.get("why") or "Trend under review.",
                    "confidence": max(41, min(92, int(round(58 + delta * 8)))),
                }
            )
        return watchlist

    def _derive_upcoming_games(
        self,
        team_form: list[dict[str, Any]],
        featured: dict[str, Any],
    ) -> list[dict[str, Any]]:
        games = []
        if featured:
            games.append(
                {
                    "matchup": f"{featured.get('away_code', 'TBD')} @ {featured.get('home_code', 'TBD')}",
                    "time": featured.get("tipoff", "Monitoring"),
                    "edge": f"{featured.get('home_probability', 50)}% home edge",
                }
            )
        for row in team_form[2:5]:
            games.append(
                {
                    "matchup": f"{row.get('code', 'TBD')} follow-up",
                    "time": "Tracked window",
                    "edge": f"{int(row.get('confidence', 50))}% confidence",
                }
            )
        return games[:4]

    def _derive_market_board(
        self,
        upcoming_games: list[dict[str, Any]],
        team_form: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return []

    def _derive_hypotheses(self, learning: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for index, item in enumerate(learning.get("hypothesis_updates") or [], start=1):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "label": f"H{index}",
                    "title": item.get("hypothesis")
                    or item.get("recommendation")
                    or "Research hypothesis",
                    "status": item.get("status") or "pending",
                    "support": item.get("support_rate")
                    if item.get("support_rate") is not None
                    else 0.0,
                }
            )
        if rows:
            return rows[:3]
        return [
            {
                "label": "H1",
                "title": "SIP is collecting enough evaluated outcomes to open new hypothesis cycles.",
                "status": "monitoring",
                "support": 0.5,
            }
        ]

    def _derive_insights(
        self,
        normalized: list[dict[str, Any]],
        health: dict[str, Any],
        learning: dict[str, Any],
        team_form: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        insights = []
        if team_form:
            leader = team_form[0]
            insights.append(
                {
                    "title": f"{leader.get('team')} leads the monitored momentum board.",
                    "body": f"Internal form rating {leader.get('confidence')} with {leader.get('record')} tracked form.",
                }
            )
        if normalized:
            player = normalized[0]
            diff = self._safe_float(
                player.get("recent_five_average")
            ) - self._safe_float(player.get("weighted_two_season_average"))
            insights.append(
                {
                    "title": f"{player.get('name')} is {diff:+.1f} above the weighted two-season baseline.",
                    "body": str(
                        player.get("why") or "Recent form is worth monitoring."
                    ),
                }
            )
        evaluated = int(learning.get("evaluated_alerts") or 0)
        insights.append(
            {
                "title": f"Learning system has evaluated {evaluated} historical alerts.",
                "body": f"Confidence readiness is {'strong' if health.get('confidence_ready') else 'still provisional'} based on loaded datasets.",
            }
        )
        return insights[:3]

    def _derive_confidence(
        self,
        health: dict[str, Any],
        learning: dict[str, Any],
        normalized: list[dict[str, Any]],
    ) -> dict[str, Any]:
        data_quality = 85 if health.get("confidence_ready") else 56
        model_performance = (
            int(round(self._safe_float(learning.get("accuracy")) * 100))
            if learning
            else 62
        )
        sample_size = (
            min(100, 35 + int(health.get("games") or 0) // 4)
            if health
            else max(30, len(normalized) * 8)
        )
        market_efficiency = max(35, min(90, 55 + len(normalized) * 4))
        overall = int(
            round(
                (data_quality + model_performance + sample_size + market_efficiency) / 4
            )
        )
        return {
            "overall": overall,
            "label": "Good"
            if overall >= 65
            else "Fair"
            if overall >= 50
            else "Developing",
            "breakdown": [
                {
                    "label": "Data Quality",
                    "value": data_quality,
                    "status": "High" if data_quality >= 75 else "Fair",
                },
                {
                    "label": "Model Performance",
                    "value": model_performance,
                    "status": "Good" if model_performance >= 60 else "Fair",
                },
                {
                    "label": "Sample Size",
                    "value": sample_size,
                    "status": "Good" if sample_size >= 60 else "Low",
                },
                {
                    "label": "Market Efficiency",
                    "value": market_efficiency,
                    "status": "Fair" if market_efficiency < 70 else "Good",
                },
            ],
        }

    def _derive_metrics(
        self,
        normalized: list[dict[str, Any]],
        learning: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        confidence = snapshot.get("confidence") or {}
        return [
            {
                "label": "Watchlist Alerts",
                "value": len(snapshot.get("watchlist") or []),
                "delta": f"{sum(1 for item in normalized if str(item.get('badge')) == 'rising')} new",
                "tone": "good",
            },
            {
                "label": "Tracked Players",
                "value": len(normalized),
                "delta": f"{len(snapshot.get('team_form') or [])} teams",
                "tone": "info",
            },
            {
                "label": "Active Hypotheses",
                "value": len(snapshot.get("hypotheses") or []),
                "delta": f"{int(learning.get('evaluated_alerts') or 0)} evaluated",
                "tone": "warn",
            },
            {
                "label": "Model Accuracy",
                "value": f"{int(self._safe_float(learning.get('accuracy')) * 100) if learning else int(confidence.get('overall', 0))}%",
                "delta": f"Confidence {int(confidence.get('overall', 0))}%",
                "tone": "good"
                if self._safe_float(learning.get("accuracy")) >= 0.6
                else "warn",
            },
        ]

    @staticmethod
    def _team_code(team: str) -> str:
        parts = [
            part
            for part in str(team).replace("@", " ").replace("-", " ").split()
            if part
        ]
        if not parts:
            return "TBD"
        return "".join(part[0] for part in parts[:3]).upper()

    def _system_status_label(self, snapshot: dict[str, Any]) -> str:
        health = snapshot.get("health") or {}
        return (
            "All Systems Operational"
            if health.get("confidence_ready")
            else "Monitoring Sources"
        )

    def _render_sidebar(self, snapshot: dict[str, Any]) -> str:
        counts = len(snapshot.get("watchlist") or [])
        return dedent(
            f"""
            <div class="sidebar-group">
                <h2>Overview</h2>
                <a class="nav-item active" href="/"><span>Overview</span><span>{counts}</span></a>
                <a class="nav-item" href="/situation-room"><span>Situation Room</span><span>Global</span></a>
            </div>
            <div class="sidebar-group">
                <h2>Observe</h2>
                <div class="nav-item"><span>Watchlist</span><span>{counts}</span></div>
                <div class="nav-item"><span>News &amp; Rumors</span><span>{len(snapshot.get("alerts") or [])}</span></div>
                <a class="nav-item" href="/betting"><span>Betting Intelligence</span><span>{len(snapshot.get("market_board") or [])}</span></a>
                <div class="nav-item"><span>Social Sentiment</span><span>Live</span></div>
            </div>
            <div class="sidebar-group">
                <h2>Analyze</h2>
                <div class="nav-item"><span>Player Analytics</span><span>{len(snapshot.get("trending_players") or [])}</span></div>
                <div class="nav-item"><span>Team Analytics</span><span>{len(snapshot.get("team_form") or [])}</span></div>
                <div class="nav-item"><span>Matchup Analyzer</span><span>Live</span></div>
                <div class="nav-item"><span>Lineup Optimizer</span><span>Beta</span></div>
            </div>
            <div class="sidebar-group">
                <h2>Research</h2>
                <div class="nav-item"><span>Hypotheses</span><span>{len(snapshot.get("hypotheses") or [])}</span></div>
                <div class="nav-item"><span>Experiments</span><span>{int((snapshot.get("learning") or {}).get("evaluated_alerts") or 0)}</span></div>
                <div class="nav-item"><span>Evidence Graph</span><span>API</span></div>
            </div>
            <div class="sidebar-group">
                <h2>System</h2>
                <div class="nav-item"><span>Data Sources</span><span>{len((snapshot.get("health") or {}).get("sources") or [])}</span></div>
                <div class="nav-item"><span>Settings</span><span>Ops</span></div>
                <div class="nav-item"><span>Integrations</span><span>4</span></div>
            </div>
            """
        )

    def _render_metric_cards(self, snapshot: dict[str, Any]) -> str:
        cards = []
        for item in snapshot.get("metrics") or []:
            cards.append(
                f"""
                <div class="metric-card">
                    <div class="label">{escape(str(item.get("label") or "Metric"))}</div>
                    <div class="value">{escape(str(item.get("value") or 0))}</div>
                    <div class="delta {escape(str(item.get("tone") or "info"))}">{escape(str(item.get("delta") or ""))}</div>
                </div>
                """
            )
        return "".join(cards)

    def _render_featured_matchup_panel(self, snapshot: dict[str, Any]) -> str:
        matchup = snapshot.get("featured_matchup") or {}
        away_probability = int(matchup.get("away_probability") or 50)
        return dedent(
            f"""
            <section class="panel featured-matchup">
                <div class="panel-header"><h2>Featured Matchup</h2><span>{escape(str(matchup.get("venue") or "SIP model view"))}</span></div>
                <div class="matchup-teams">
                    <div class="team-block"><div class="team-code">{escape(str(matchup.get("away_code") or "TBD"))}</div><div>{escape(str(matchup.get("away_team") or "Away"))}</div><div class="team-record">{escape(str(matchup.get("away_record") or "0-0"))}</div></div>
                    <div class="matchup-center">@</div>
                    <div class="team-block"><div class="team-code">{escape(str(matchup.get("home_code") or "TBD"))}</div><div>{escape(str(matchup.get("home_team") or "Home"))}</div><div class="team-record">{escape(str(matchup.get("home_record") or "0-0"))}</div></div>
                </div>
                <div class="refresh">{escape(str(matchup.get("tipoff") or "Live monitored slate"))}</div>
                <div class="refresh" style="margin-top: 8px;">SIP Win Probability</div>
                <div class="probability-track" style="--left-prob:{away_probability}%">
                    <div class="probability-left"></div><div class="probability-right"></div>
                </div>
                <div class="probability-labels"><span>{away_probability}%</span><span>{100 - away_probability}%</span></div>
                <div class="button-row">
                    <div class="subtle-button">Matchup Analysis</div>
                    <div class="subtle-button">Lineup Optimizer</div>
                    <div class="subtle-button">Simulate Game</div>
                </div>
            </section>
            """
        )

    def _render_spotlight_panel(self, snapshot: dict[str, Any]) -> str:
        spotlight = snapshot.get("spotlight") or {}
        initials = (
            "".join(
                part[0] for part in str(spotlight.get("name") or "SIP").split()[:2]
            ).upper()
            or "S"
        )
        trend_score = int(spotlight.get("trend_score") or 0)
        stat_html = "".join(
            f'<div class="stat"><span>{escape(str(item.get("label") or "Stat"))}</span><span>{escape(str(item.get("value") or "0"))}</span></div>'
            for item in spotlight.get("stats") or []
        )
        return dedent(
            f"""
            <section class="panel">
                <div class="panel-header"><h2>Player Spotlight</h2><span>Overall trend</span></div>
                <div class="spotlight-player">
                    <div class="player-avatar">{escape(initials)}</div>
                    <div>
                        <strong style="font-size:1.6rem; display:block;">{escape(str(spotlight.get("name") or "No player loaded"))}</strong>
                        <div class="refresh">{escape(str(spotlight.get("team") or "Awaiting data"))}</div>
                        <div class="badge {escape(str(spotlight.get("badge") or "stable"))}" style="margin-top:12px;">{escape(str(spotlight.get("badge") or "stable"))}</div>
                        <p class="why" style="margin-top:12px;">{escape(str(spotlight.get("summary") or "SIP will surface a spotlight as soon as data is ready."))}</p>
                        <div class="spotlight-grid">{stat_html}</div>
                    </div>
                </div>
                <div class="ring-wrap" style="position:relative;">
                    <div class="ring" style="--pct:{max(0, min(100, trend_score)) / 100}"></div>
                    <div class="ring-value">{trend_score}</div>
                </div>
            </section>
            """
        )

    def _render_news_panel(self, snapshot: dict[str, Any]) -> str:
        items = []
        for alert in snapshot.get("alerts") or []:
            severity = str(alert.get("severity") or "info")
            url = str(alert.get("url") or "").strip()
            published = str(alert.get("published") or "").strip()
            footer_parts = [str(alert.get("meta") or "").strip()]
            if published:
                footer_parts.append(published)
            footer_text = " · ".join(part for part in footer_parts if part)
            title = escape(str(alert.get("title") or "Alert"))
            if url:
                title = (
                    f'<a href="{escape(url)}" target="_blank" rel="noreferrer">'
                    f"{title}</a>"
                )
            items.append(
                f"""
                <div class="news-item">
                    <div class="pill {escape(severity)}">{escape(severity.title())}</div>
                    <strong>{title}</strong>
                    <div>{escape(str(alert.get("body") or ""))}</div>
                    <div class="meta-row"><span>{escape(footer_text)}</span></div>
                </div>
                """
            )
        if not items:
            items.append('<div class="news-item">No alerts yet.</div>')
        return f'<section class="panel"><div class="panel-header"><h2>News, Rumors &amp; Injuries</h2><span>ESPN + live summaries</span></div><div class="news-list">{"".join(items)}</div></section>'

    def _render_watchlist_panel(self, snapshot: dict[str, Any]) -> str:
        items = []
        for item in snapshot.get("watchlist") or []:
            tone = "good" if item.get("confidence", 0) >= 65 else "warn"
            items.append(
                f"""
                <div class="watch-item">
                    <strong>{escape(str(item.get("name") or "Player"))}</strong>
                    <div class="meta-row"><span>{escape(str(item.get("team") or ""))}</span><span class="pill {tone}">{escape(str(item.get("tag") or "WATCH"))}</span><span>{escape(str(item.get("confidence") or 0))}% confidence</span></div>
                    <div>{escape(str(item.get("summary") or ""))}</div>
                </div>
                """
            )
        if not items:
            items.append('<div class="watch-item">No watchlist alerts yet.</div>')
        return f'<section class="panel"><div class="panel-header"><h2>Watchlist Alerts</h2><span>View all</span></div><div class="watch-list">{"".join(items)}</div></section>'

    def _render_team_form_panel(self, snapshot: dict[str, Any]) -> str:
        rows = []
        for index, item in enumerate(snapshot.get("team_form") or [], start=1):
            marker = max(
                10, min(90, int(50 + self._safe_float(item.get("trend_score")) * 10))
            )
            rows.append(
                f"""
                <div class="trend-row">
                    <strong>{escape(str(item.get("code") or "TBD"))}</strong>
                    <span>{escape(str(item.get("record") or "0-0"))}</span>
                    <div class="sparkline" style="--marker:{marker}%"></div>
                </div>
                """
            )
        if not rows:
            rows.append('<div class="refresh">No team form data yet.</div>')
        return f'<section class="panel"><div class="panel-header"><h2>Team Form (Last 10 Games)</h2><span>View all</span></div><div class="trend-lines">{"".join(rows)}</div></section>'

    def _render_market_panel(self, snapshot: dict[str, Any]) -> str:
        rows = []
        for item in snapshot.get("market_board") or []:
            rows.append(
                f"""
                <div class="table-row">
                    <div>{escape(str(item.get("matchup") or "TBD"))}</div>
                    <div>{escape(str(item.get("spread") or "0.0"))}</div>
                    <div>{escape(str(item.get("total") or "0.0"))} · {escape(str(item.get("signal") or "0%"))}</div>
                </div>
                """
            )
        if not rows:
            rows.append('<div class="refresh">No edge board entries yet.</div>')
        return f'<section class="panel"><div class="panel-header"><h2>Odds Monitor</h2><span>ESPN market context</span></div><div class="mini-table">{"".join(rows)}</div></section>'

    def _render_upcoming_panel(self, snapshot: dict[str, Any]) -> str:
        rows = []
        for item in snapshot.get("upcoming_games") or []:
            rows.append(
                f"""
                <div class="upcoming-item">
                    <strong>{escape(str(item.get("matchup") or "TBD"))}</strong>
                    <div class="meta-row"><span>{escape(str(item.get("time") or "Monitoring"))}</span><span>{escape(str(item.get("edge") or ""))}</span></div>
                </div>
                """
            )
        if not rows:
            rows.append('<div class="upcoming-item">No upcoming games available.</div>')
        return f'<section class="panel"><div class="panel-header"><h2>Upcoming Games</h2><span>Live slate</span></div><div class="upcoming-list">{"".join(rows)}</div></section>'

    def _render_hypotheses_panel(self, snapshot: dict[str, Any]) -> str:
        rows = []
        for item in snapshot.get("hypotheses") or []:
            status = str(item.get("status") or "monitoring")
            tone = (
                "good"
                if status == "strengthen"
                else "warn"
                if status in {"monitor", "monitoring"}
                else "info"
            )
            support = self._safe_float(item.get("support")) * 100
            rows.append(
                f"""
                <div class="hypothesis-item">
                    <div class="meta-row"><span class="pill {tone}">{escape(str(item.get("label") or "H"))}</span><span>{escape(status.title())}</span><span>{support:.0f}% support</span></div>
                    <strong>{escape(str(item.get("title") or "Hypothesis"))}</strong>
                </div>
                """
            )
        if not rows:
            rows.append('<div class="hypothesis-item">No active hypotheses.</div>')
        return f'<section class="panel"><div class="panel-header"><h2>Active Hypotheses</h2><span>View all</span></div><div class="hypothesis-list">{"".join(rows)}</div></section>'

    def _render_insights_panel(self, snapshot: dict[str, Any]) -> str:
        rows = []
        for item in snapshot.get("insights") or []:
            rows.append(
                f"""
                <div class="hypothesis-item">
                    <strong>{escape(str(item.get("title") or "Insight"))}</strong>
                    <div>{escape(str(item.get("body") or ""))}</div>
                </div>
                """
            )
        if not rows:
            rows.append('<div class="hypothesis-item">No system insights yet.</div>')
        return f'<section class="panel"><div class="panel-header"><h2>System Insights</h2><span>SIP generated</span></div><div class="hypothesis-list">{"".join(rows)}</div></section>'

    def _render_public_pulse_panel(self, snapshot: dict[str, Any]) -> str:
        pulse = snapshot.get("public_pulse") or {}
        items = []
        for item in pulse.get("items") or []:
            url = str(item.get("url") or "").strip()
            title = escape(str(item.get("title") or "Headline"))
            if url:
                title = (
                    f'<a href="{escape(url)}" target="_blank" rel="noreferrer">'
                    f"{title}</a>"
                )
            items.append(
                f"""
                <div class="hypothesis-item">
                    <div class="meta-row"><span class="pill info">{escape(str(item.get("label") or "Pulse"))}</span><span>{escape(str(item.get("source") or "Public headline"))}</span></div>
                    <strong>{title}</strong>
                    <div>{escape(str(item.get("summary") or ""))}</div>
                </div>
                """
            )
        if not items:
            items.append(
                '<div class="hypothesis-item">No public pulse items available.</div>'
            )
        summary_text = str(pulse.get("summary") or "Public headline sentiment")
        return f'<section class="panel"><div class="panel-header"><h2>Public Pulse</h2><span>{escape(summary_text)}</span></div><div class="hypothesis-list">{"".join(items)}</div></section>'

    def _render_confidence_panel(self, snapshot: dict[str, Any]) -> str:
        confidence = snapshot.get("confidence") or {}
        rows = []
        for item in confidence.get("breakdown") or []:
            rows.append(
                f"""
                <div class="confidence-row">
                    <div>
                        <div>{escape(str(item.get("label") or "Confidence"))}</div>
                        <div class="bar"><span style="width:{max(0, min(100, int(item.get("value") or 0)))}%"></span></div>
                    </div>
                    <div>{escape(str(item.get("status") or ""))}</div>
                </div>
                """
            )
        return dedent(
            f"""
            <section class="panel">
                <div class="panel-header"><h2>SIP Confidence Index</h2><span>Reliability</span></div>
                <div class="ring-wrap" style="position:relative; margin-bottom:12px;">
                    <div class="ring" style="--pct:{max(0, min(100, int(confidence.get("overall", 0)))) / 100}"></div>
                    <div class="ring-value">{escape(str(confidence.get("overall", 0)))}%</div>
                </div>
                <div class="refresh" style="text-align:center; margin-bottom:12px;">{escape(str(confidence.get("label") or "Developing"))}</div>
                <div class="confidence-grid">{"".join(rows)}</div>
            </section>
            """
        )

    def _render_research_console(self, trending_players: list[Any]) -> str:
        default_query = getattr(self, "research_query", "") or ""
        if not default_query and trending_players:
            first = _normalize_trending_player(trending_players[0])
            default_query = str(first.get("name") or "")

        selected = getattr(self, "selected_research_providers", []) or []
        selected_csv = ",".join(selected)
        result_html = self._render_research_results(default_query, selected)
        provider_chips = "".join(
            f'<span class="subtle-chip">{escape(name)}</span>'
            for name in ["pgvector", "wikipedia", "pubmed", "arxiv"]
        )
        return dedent(
            f"""
            <section class="panel">
                <div class="panel-header"><h2>Research Console</h2><span>Current data APIs</span></div>
                <form action="/" method="get" class="research-form">
                    <input type="hidden" name="league" value="{escape(getattr(self, "selected_league", "") or "")}">
                    <input type="hidden" name="competition" value="{escape(getattr(self, "selected_competition", "") or "")}">
                    <input type="hidden" name="mode" value="{escape(getattr(self, "explanation_mode", "adult") or "adult")}">
                    <label>Research Query<input name="research_q" value="{escape(default_query)}" placeholder="Query all available retrieval APIs"></label>
                    <label>Providers<input name="providers" value="{escape(selected_csv)}" placeholder="pgvector,wikipedia,pubmed,arxiv"></label>
                    <button type="submit">Run Research</button>
                </form>
                <div class="research-providers">{provider_chips}</div>
                {result_html}
            </section>
            """
        )

    def _render_research_results(
        self,
        default_query: str,
        selected: list[str],
    ) -> str:
        if not getattr(self, "research_query", ""):
            return '<p class="research-empty">Run a query to blend the current retrieval providers into the dashboard. Supported providers: pgvector, wikipedia, pubmed, arxiv.</p>'

        search_items: list[str] = []
        retrieval_items: list[str] = []
        safe_providers = selected or ["pgvector", "wikipedia", "pubmed", "arxiv"]

        try:
            search_results = SearchService().search(
                source="wikipedia",
                query=default_query,
                limit=3,
            )
        except Exception:
            search_results = []

        try:
            retrieval_results = RetrievalService(
                provider_names=safe_providers
            ).retrieve(
                default_query,
                limit=6,
            )
        except Exception:
            retrieval_results = []

        for result in search_results[:3]:
            search_items.append(
                f"""
                <div class="research-item">
                    <strong>{escape(str(result.title or "Wikipedia result"))}</strong>
                    <div class="meta-row"><span>{escape(str(result.source or "wikipedia"))}</span><span>{escape(str(result.url or ""))}</span></div>
                    <div>{escape(str(result.snippet or ""))}</div>
                </div>
                """
            )
        for result in retrieval_results[:4]:
            source = (
                result.metadata.get("source")
                if isinstance(result.metadata, dict)
                else None
            )
            retrieval_items.append(
                f"""
                <div class="research-item">
                    <strong>{escape(str(result.document_id or "Retrieved document"))}</strong>
                    <div class="meta-row"><span>{escape(str(source or getattr(result, "source", "") or "provider"))}</span><span>Score {self._safe_float(result.score):.2f}</span></div>
                    <div>{escape(str(result.content or "")[:260])}</div>
                </div>
                """
            )

        if not search_items and not retrieval_items:
            return '<p class="research-empty">No research results were returned for this query.</p>'

        return dedent(
            f"""
            <div class="two-col" style="margin-top:16px; align-items:start;">
                <div>
                    <h3 style="margin:0 0 10px;">Search Surface</h3>
                    <div class="research-list">{"".join(search_items) or '<div class="research-item">No search results.</div>'}</div>
                </div>
                <div>
                    <h3 style="margin:0 0 10px;">Retrieval Surface</h3>
                    <div class="research-list">{"".join(retrieval_items) or '<div class="research-item">No retrieval results.</div>'}</div>
                </div>
            </div>
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
            calibration.get("buckets") or calibration.get("calibration_buckets") or []
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

    def render_betting_intelligence(self) -> str:
        board_provider = getattr(self, "betting_board_provider", None)
        board: dict[str, Any] = {}
        if board_provider is not None:
            try:
                board = dict(board_provider() or {})
            except TypeError:
                board = dict(board_provider(self) or {})
            except Exception as error:
                board = {
                    "status": "error",
                    "assessments": [],
                    "message": f"Live betting board unavailable: {error}",
                    "exclusions": [],
                }
        if board:
            assessments = list(board.get("assessments") or [])
        else:
            provider = getattr(
                self,
                "betting_assessment_provider",
                default_betting_assessment_provider,
            )
            try:
                supplied = provider()
            except TypeError:
                supplied = provider(self)
            except Exception:
                supplied = []
            assessments = list(supplied or [])
            board = {
                "data_classification": "live",
                "status": "ready" if assessments else "unavailable",
                "assessments": assessments,
                "message": "",
                "exclusions": [],
                "source_health": {},
            }

        replay_board: dict[str, Any] = {}
        replay_provider = getattr(self, "betting_replay_provider", None)
        if replay_provider is not None:
            try:
                replay_board = dict(replay_provider() or {})
            except TypeError:
                replay_board = dict(replay_provider(self) or {})
            except Exception:
                replay_board = {}

        qualified = sum(
            1 for item in assessments if self._betting_value(item, "qualified", False)
        )
        no_bets = len(assessments) - qualified
        best_edges = [
            float(self._betting_value(item, "probability_edge", 0.0) or 0.0)
            for item in assessments
        ]
        stale_count = sum(
            1
            for item in assessments
            for price in (self._betting_value(item, "book_prices", ()) or ())
            if self._betting_value(price, "stale", False)
        )

        cards = "".join(
            self._render_betting_card(item, rank=index)
            for index, item in enumerate(assessments, start=1)
        )
        if not cards:
            cards = dedent(
                f"""
                <section class="empty-state">
                    <div class="empty-icon">No live quotes</div>
                    <h2>No market assessments yet</h2>
                    <p>{escape(str(board.get("message") or "Connect a normalized odds feed and calibrated SIP prediction provider to populate this board."))}</p>
                    <strong>No bet is the default until fresh, complete market evidence is available.</strong>
                </section>
                """
            )
        board_classification = str(
            board.get("data_classification") or "delayed"
        ).lower()
        live_ready = bool(assessments) and board_classification == "live"
        classification_labels = {
            "live": "LIVE MARKET DATA" if assessments else "LIVE DATA UNAVAILABLE",
            "delayed": "DELAYED MARKET DATA",
            "replay": "REPLAY DATA",
            "model-only": "MODEL-ONLY ANALYSIS",
        }
        live_label = classification_labels.get(
            board_classification,
            "DATA CLASSIFICATION UNKNOWN",
        )
        generated_at = str(board.get("generated_at") or "Not retrieved")
        source_health = dict(board.get("source_health") or {})
        source_status = str(source_health.get("status") or board.get("status") or "unknown")
        exclusions = list(board.get("exclusions") or [])
        exclusion_panel = (
            "<details class='diagnostics'><summary>Why markets were not ranked</summary>"
            + self._betting_list(exclusions, "No normalization exclusions.")
            + "</details>"
            if exclusions
            else ""
        )
        replay_assessments = list(replay_board.get("assessments") or [])
        replay_cards = "".join(
            self._render_betting_card(
                item,
                rank=index,
                replay=True,
                ranking=replay_board.get("ranking"),
            )
            for index, item in enumerate(replay_assessments, start=1)
        )
        replay_section = (
            f"""
            <section class="replay-zone">
                <div class="source-banner replay"><strong>HISTORICAL REPLAY</strong>
                <span>{escape(str(replay_board.get("message") or "Illustrative replay only; not current odds."))}</span></div>
                <h2>Illustrative replay best option</h2>
                <p class="subtitle">This section demonstrates how a qualified option looks. It is not a current recommendation and cannot be wagered through RAGHub.</p>
                <div class="board">{replay_cards}</div>
            </section>
            """
            if replay_cards
            else ""
        )

        return dedent(
            f"""
            <!doctype html>
            <html lang="en">
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <title>SIP Betting Intelligence</title>
                <style>
                    :root {{
                        color-scheme: dark; --bg:#070b14; --panel:#0e1626;
                        --soft:#121d32; --border:rgba(124,144,179,.2);
                        --text:#eef4ff; --muted:#96a8c7; --green:#27d17f;
                        --amber:#f4bf41; --red:#ff6f7a; --blue:#64b5ff;
                    }}
                    *{{box-sizing:border-box}} body{{margin:0;font-family:"Segoe UI",Arial,sans-serif;color:var(--text);
                    background:radial-gradient(circle at top right,rgba(100,181,255,.14),transparent 25%),var(--bg)}}
                    a{{color:inherit;text-decoration:none}} .shell{{max-width:1440px;margin:auto;padding:28px}}
                    .top{{display:flex;justify-content:space-between;gap:18px;align-items:center;margin-bottom:24px}}
                    .back,.chip{{display:inline-flex;padding:9px 13px;border:1px solid var(--border);border-radius:999px;background:var(--soft)}}
                    h1{{font-size:clamp(2rem,4vw,3.4rem);margin:12px 0 8px}} .subtitle{{color:var(--muted);max-width:820px;line-height:1.6}}
                    .metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}}
                    .metric,.card,.empty-state{{background:linear-gradient(180deg,#101a2d,#0b1322);border:1px solid var(--border);border-radius:20px}}
                    .metric{{padding:16px}} .metric span{{display:block;color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.08em}}
                    .metric strong{{font-size:1.8rem;display:block;margin-top:8px}} .board{{display:grid;gap:16px}}
                    .card{{padding:20px}} .card-head{{display:flex;justify-content:space-between;gap:16px;align-items:start}}
                    .market{{color:var(--muted);text-transform:uppercase;font-size:.78rem;letter-spacing:.1em}}
                    .decision{{font-weight:800;padding:8px 12px;border-radius:999px;white-space:nowrap}}
                    .decision.good{{color:#8dffc0;background:rgba(39,209,127,.14)}} .decision.warn{{color:#ffd98b;background:rgba(244,191,65,.14)}}
                    .edge-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:18px 0}}
                    .datum{{padding:12px;border-radius:14px;background:var(--soft);border:1px solid var(--border)}}
                    .datum span{{display:block;color:var(--muted);font-size:.76rem;margin-bottom:6px}} .datum strong{{font-size:1.2rem}}
                    .section-title{{margin:18px 0 8px;font-size:.78rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}}
                    .books{{display:grid;gap:7px}} .book{{display:grid;grid-template-columns:1fr repeat(4,110px);gap:10px;padding:10px 12px;border-radius:12px;background:rgba(18,29,50,.75)}}
                    .stale{{color:var(--amber)}} .context{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
                    .note{{padding:12px;border-radius:14px;background:rgba(100,181,255,.08);border:1px solid rgba(100,181,255,.16);line-height:1.5}}
                    .risk{{background:rgba(244,191,65,.08);border-color:rgba(244,191,65,.18)}} ul{{margin:8px 0;padding-left:20px}}
                    .empty-state{{text-align:center;padding:64px 20px}} .empty-state p{{color:var(--muted)}} .empty-icon{{font-size:3rem;color:var(--blue)}}
                    .source-banner{{display:flex;gap:12px;align-items:center;padding:12px 14px;border-radius:14px;background:rgba(255,111,122,.1);border:1px solid rgba(255,111,122,.25);margin:14px 0}}
                    .source-banner strong{{font-size:.76rem;letter-spacing:.08em}}.source-banner span{{color:var(--muted)}}
                    .source-banner.live{{background:rgba(39,209,127,.09);border-color:rgba(39,209,127,.22)}}
                    .source-banner.replay{{background:rgba(244,191,65,.1);border-color:rgba(244,191,65,.25)}}
                    .replay-zone{{margin-top:34px;padding-top:24px;border-top:1px solid var(--border)}}
                    .probability-visual{{display:grid;gap:8px;margin:16px 0}}.probability-row{{display:grid;grid-template-columns:95px 1fr 62px;gap:9px;align-items:center}}
                    .probability-track{{height:12px;border-radius:999px;background:#26334a;overflow:hidden}}.probability-bar{{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--blue),var(--green))}}
                    .probability-row.market-row .probability-bar{{background:linear-gradient(90deg,#8a6eff,var(--amber))}}
                    .diagnostics{{margin:14px 0;padding:12px 14px;border:1px solid var(--border);border-radius:14px;background:var(--soft)}}
                    @media(max-width:900px){{.metrics,.edge-grid{{grid-template-columns:repeat(2,1fr)}}.context{{grid-template-columns:1fr}}.book{{grid-template-columns:1fr 1fr}}}}
                    @media(max-width:560px){{.shell{{padding:18px}}.metrics,.edge-grid{{grid-template-columns:1fr}}.top,.card-head{{align-items:flex-start;flex-direction:column}}}}
                </style>
            </head>
            <body><main class="shell">
                <div class="top"><a class="back" href="/">← Dashboard</a><span class="chip">Decision layer · point-in-time safe</span></div>
                <header>
                    <div class="market">SIP Market Desk</div>
                    <h1>Betting Intelligence</h1>
                    <p class="subtitle">Where SIP's calibrated probability differs meaningfully from the no-vig market—after freshness, uncertainty, model agreement, calibration, and risk checks.</p>
                </header>
                <div class="source-banner {"live" if live_ready else ""}"><strong>{live_label}</strong>
                <span>{escape(str(board.get("message") or "Point-in-time normalized sportsbook assessments."))}</span></div>
                <p class="subtitle">Source: {escape(str(board.get("provider") or "configured sportsbook provider"))} · Provider status: {escape(source_status)} · Board generated: {escape(generated_at)}</p>
                <section class="metrics">
                    <div class="metric"><span>Markets assessed</span><strong>{len(assessments)}</strong></div>
                    <div class="metric"><span>Qualified edges</span><strong>{qualified}</strong></div>
                    <div class="metric"><span>No bet</span><strong>{no_bets}</strong></div>
                    <div class="metric"><span>Largest edge</span><strong>{max(best_edges, default=0.0):.1%}</strong></div>
                </section>
                {"<p class='subtitle'>Stale quotes excluded across the board: " + str(stale_count) + "</p>" if stale_count else ""}
                {exclusion_panel}
                <section class="board">{cards}</section>
                {replay_section}
            </main></body></html>
            """
        )

    def _render_betting_card(
        self,
        assessment: Any,
        *,
        rank: int | None = None,
        replay: bool = False,
        ranking: Any = None,
    ) -> str:
        prediction = self._betting_value(assessment, "prediction", {})
        quote = self._betting_value(assessment, "best_quote")
        qualified = bool(self._betting_value(assessment, "qualified", False))
        probability_value = self._betting_value(prediction, "probability")
        probability = (
            float(probability_value) if probability_value is not None else 0.0
        )
        probability_text = self._format_optional_percent(probability_value)
        consensus = self._betting_value(assessment, "consensus_probability")
        edge = self._betting_value(assessment, "probability_edge")
        expected_return = self._betting_value(assessment, "expected_return")
        adjusted_return = self._betting_value(
            assessment,
            "confidence_adjusted_return",
        )
        interval = self._betting_value(assessment, "confidence_interval", (0.0, 0.0))
        agreement_value = self._betting_value(assessment, "model_agreement")
        agreement = (
            float(agreement_value) if agreement_value is not None else 0.0
        )
        interval_low = interval[0] if interval and len(interval) > 0 else None
        interval_high = interval[1] if interval and len(interval) > 1 else None
        interval_text = (
            f"{float(interval_low):.1%}–{float(interval_high):.1%}"
            if interval_low is not None and interval_high is not None
            else "Unavailable"
        )
        agreement_text = self._format_optional_percent(agreement_value)
        market = (
            str(self._betting_value(prediction, "market", "Market"))
            .replace("_", " ")
            .title()
        )
        selection = str(
            self._betting_value(prediction, "selection", "Selection")
        ).title()
        line = self._betting_value(prediction, "line")
        event_id = self._betting_value(prediction, "event_id", "Unknown event")
        line_text = f" {float(line):g}" if line is not None else ""

        prices = []
        for price in self._betting_value(assessment, "book_prices", ()) or ():
            american = int(self._betting_value(price, "american_price", 0) or 0)
            age = float(self._betting_value(price, "age_seconds", 0.0) or 0.0)
            stale = bool(self._betting_value(price, "stale", False))
            prices.append(
                f"""<div class="book{" stale" if stale else ""}">
                <strong>{escape(str(self._betting_value(price, "sportsbook", "Book")))}</strong>
                <span>{american:+d}</span>
                <span>{float(self._betting_value(price, "decimal_price", 0.0) or 0.0):.2f} decimal</span>
                <span>{float(self._betting_value(price, "fair_probability", 0.0) or 0.0):.1%} fair</span>
                <span>{age:.0f}s{" · stale" if stale else ""}</span></div>"""
            )
        reasons = self._betting_value(prediction, "reasons", ()) or ()
        invalidators = self._betting_value(prediction, "invalidators", ()) or ()
        rejections = self._betting_value(assessment, "rejection_reasons", ()) or ()
        warnings = self._betting_value(assessment, "warnings", ()) or ()
        best_book = self._betting_value(quote, "sportsbook", "Unavailable")
        best_price = self._betting_value(quote, "american_price")
        best_price_text = (
            f"{int(best_price):+d}" if best_price is not None else "Unavailable"
        )
        market_probability = float(consensus or 0.0)
        rank_label = (
            f" · rank #{rank}" if rank is not None and qualified else ""
        )
        replay_score = (
            self._betting_value(ranking, "score")
            if ranking is not None
            else None
        )

        return dedent(
            f"""
            <article class="card">
                <div class="card-head">
                    <div><div class="market">{escape(str(event_id))} · {escape(market)}</div>
                    <h2>{escape(selection)}{escape(line_text)}</h2></div>
                    <span class="decision {"good" if qualified else "warn"}">{"Qualified edge" if qualified else "No bet"}{rank_label}</span>
                </div>
                <div class="probability-visual" aria-label="SIP versus market probability">
                    <div class="probability-row"><span>SIP {probability_text}</span><div class="probability-track"><span class="probability-bar" style="width:{probability * 100:.1f}%"></span></div><strong>{probability_text}</strong></div>
                    <div class="probability-row market-row"><span>Market {market_probability:.1%}</span><div class="probability-track"><span class="probability-bar" style="width:{market_probability * 100:.1f}%"></span></div><strong>{market_probability:.1%}</strong></div>
                </div>
                <div class="edge-grid">
                    <div class="datum"><span>SIP probability</span><strong>{probability_text}</strong></div>
                    <div class="datum"><span>No-vig consensus</span><strong>{self._format_optional_percent(consensus)}</strong></div>
                    <div class="datum"><span>Probability edge</span><strong>{self._format_optional_percent(edge, signed=True)}</strong></div>
                    <div class="datum"><span>Point-estimate EV</span><strong>{self._format_optional_percent(expected_return, signed=True)}</strong></div>
                    <div class="datum"><span>Best price</span><strong>{escape(str(best_book))} · {best_price_text}</strong></div>
                </div>
                <p class="subtitle">Confidence-adjusted EV: {self._format_optional_percent(adjusted_return, signed=True)}</p>
                {"<p class='subtitle'>Replay opportunity score: " + escape(str(replay_score)) + "/100</p>" if replay and replay_score is not None else ""}
                <div class="context">
                    <div class="note"><strong>Confidence and agreement</strong>
                    <p>{interval_text} probability range · {agreement_text} model agreement</p>
                    <strong>Why the edge may exist</strong>{self._betting_list(reasons, "No model explanation supplied.")}</div>
                    <div class="note risk"><strong>Risk and abstention checks</strong>
                    {self._betting_list((*rejections, *warnings), "All configured qualification gates passed.")}
                    <strong>What could invalidate it</strong>{self._betting_list(invalidators, "No explicit invalidators supplied.")}</div>
                </div>
                <div class="section-title">Book-by-book prices · freshness · no-vig fair probability</div>
                <div class="books">{"".join(prices) or '<div class="note">No usable book prices.</div>'}</div>
            </article>
            """
        )

    @staticmethod
    def _betting_value(candidate: Any, name: str, default: Any = None) -> Any:
        if candidate is None:
            return default
        if isinstance(candidate, dict):
            return candidate.get(name, default)
        return getattr(candidate, name, default)

    @staticmethod
    def _format_optional_percent(value: Any, *, signed: bool = False) -> str:
        if value is None:
            return "Unavailable"
        return f"{float(value):+.1%}" if signed else f"{float(value):.1%}"

    @staticmethod
    def _betting_list(values: Any, empty_text: str) -> str:
        items = [str(value) for value in (values or ()) if str(value).strip()]
        if not items:
            return f"<p>{escape(empty_text)}</p>"
        return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"

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
    ConfiguredFeatureUIHandler = build_configured_feature_ui_handler(runtime)

    server = ThreadingHTTPServer(
        ("0.0.0.0", 8000),
        ConfiguredFeatureUIHandler,
    )
    print("Serving at http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__":
    main()
