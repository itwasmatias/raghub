from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from textwrap import dedent
from urllib.parse import parse_qs
from typing import Any

from models.sports.player import Player
from sports.features.builders.trend_builder import TrendFeatureBuilder
from sports.features.facade import FeatureFacade
from sports.features.registry import FeatureRegistry


DEMO_PLAYERS = [
    {
        "name": "Nova Carter",
        "team": "Sky Rockets",
        "badge": "rising",
        "recent_five_average": 18.4,
        "current_season_average": 16.9,
        "previous_season_average": 11.2,
        "weighted_two_season_average": 14.5,
        "why": "She has been stacking stronger finishes and keeps lifting the team when the game gets close.",
    },
    {
        "name": "Milo Grant",
        "team": "River Owls",
        "badge": "stable",
        "recent_five_average": 12.1,
        "current_season_average": 12.0,
        "previous_season_average": 11.8,
        "weighted_two_season_average": 11.9,
        "why": "He has been steady like a metronome, which makes him easy for SIP to trust.",
    },
    {
        "name": "Zuri Lane",
        "team": "Comet Cubs",
        "badge": "declining",
        "recent_five_average": 8.6,
        "current_season_average": 10.3,
        "previous_season_average": 13.4,
        "weighted_two_season_average": 11.7,
        "why": "Her early spark has cooled a bit, so SIP notices the dip and flags it for a comeback watch.",
    },
]


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
        "name": _first_value(
            player_details,
            "player_name",
            "name",
            "full_name",
            default="Unknown Player",
        ),
        "team": _first_value(
            team_details,
            "team_name",
            "name",
            "abbreviation",
            default="Unknown Team",
        ),
        "badge": str(badge),
        "recent_five_average": _first_value(
            player,
            "recent_five_average",
            "recent_5_game_average",
            "recent_average",
            default=0.0,
        ),
        "current_season_average": _first_value(
            player,
            "current_season_average",
            "season_average",
            default=0.0,
        ),
        "previous_season_average": _first_value(
            player,
            "previous_season_average",
            default=0.0,
        ),
        "weighted_two_season_average": _first_value(
            player,
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


class FeatureUIHandler(BaseHTTPRequestHandler):
    trending_player_provider = staticmethod(default_trending_player_provider)
    refresh_callback = staticmethod(default_refresh_callback)

    def do_GET(self) -> None:
        if self.path == "/":
            self.send_html(self.render_form())
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def _handle_refresh(self) -> None:
        self.refresh_callback()
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

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def render_form(self) -> str:
        trending_players = list(self._get_trending_players())
        if trending_players:
            cards = [
                self.render_trending_card(player, "Live Cached Data")
                for player in trending_players
            ]
            cards_html = "".join(cards)
            source_label = "Live Cached Data"
        else:
            cards_html = "".join(
                self.render_trending_card(player, "Demo Data")
                for player in DEMO_PLAYERS
            )
            source_label = "Demo Data"

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
                        <div class="cards">
                            {cards_html}
                        </div>
                        <div class="refresh">Last refreshed: {escape(refresh_time)}</div>
                        <form action="/refresh" method="post" style="margin-top: 16px;">
                            <button type="submit">Refresh NBA Data</button>
                        </form>
                    </section>

                    <section class="form-shell">
                        <h2>Manual Player Feature Form</h2>
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
            trending_players = provider()
        except TypeError:
            trending_players = provider(self)

        if not trending_players:
            return []

        return list(trending_players)

    def render_trending_card(self, player: Any, source_label: str) -> str:
        details = _normalize_trending_player(player)
        badge = str(details["badge"])
        return dedent(
            f"""
            <article class="player-card">
                <div>
                    <h2>{escape(str(details["name"]))}</h2>
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
                <p class="why"><strong>Why SIP noticed him:</strong> {escape(str(details["why"]))}</p>
                <div class="refresh">Last refreshed: {escape(self._format_refresh_time(details["last_refreshed_at"]))}</div>
            </article>
            """
        )

    def _format_refresh_time(self, value: Any) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc)
            return value.strftime("%Y-%m-%d %H:%M UTC")

        return str(value)

    def render_demo_card(self, player: dict[str, object]) -> str:
        return self.render_trending_card(player, "Demo Data")

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
    server = ThreadingHTTPServer(("0.0.0.0", 8000), FeatureUIHandler)
    print("Serving at http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__":
    main()
