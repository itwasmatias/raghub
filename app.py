from dataclasses import asdict
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import perf_counter
from collections.abc import Callable
from collections import defaultdict, deque
from time import time
from uuid import uuid4
import hmac

from dotenv import load_dotenv
from flask import Flask, Response, g, make_response, redirect, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from intelligence.service import SituationRoomService
from intelligence.ui import render_situation_room
from models.sports.player import Player
from services.answer import AnswerService
from services.overview import OverviewService
from services.retrieve import RetrievalService
from sports.application.production_runtime import (
    ApplicationRuntime,
    build_production_runtime,
)
from sports.data.repositories.sqlite_lifecycle_repository import (
    SQLiteLifecycleRepository,
)
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService
from sports.release.demo import NBAReplayDemo
from sports.release.health import SystemHealthService
from sports.release.settings import ReleaseSettings
from sports.personal.service import PersonalEditionService
from sports.compute.repository import ComputeJobRepository
from sports.features.builders.trend_builder import TrendFeatureBuilder
from sports.features.facade import FeatureFacade
from sports.features.registry import FeatureRegistry
from web_ui import create_feature_ui_instance


load_dotenv(Path(__file__).with_name(".env"), override=True)


def create_app(
    *,
    answer_service: AnswerService | None = None,
    runtime: ApplicationRuntime | None = None,
    situation_room_service: SituationRoomService | None = None,
    lifecycle_service: IntelligenceLifecycleService | None = None,
    health_service: SystemHealthService | None = None,
    replay_demo_factory: Callable[[], NBAReplayDemo] | None = None,
    release_settings: ReleaseSettings | None = None,
    personal_service: PersonalEditionService | None = None,
    compute_repository: ComputeJobRepository | None = None,
    worker_token: str | None = None,
    overview_service: OverviewService | None = None,
):
    app = Flask(__name__)
    if os.getenv("SIP_TRUST_PROXY", "false").strip().lower() in {"1", "true", "yes"}:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    version = Path("VERSION").read_text(encoding="utf-8").strip()
    frontend_files = (
        Path("templates/sip_personal.html"),
        Path("templates/betting_intelligence.html"),
        Path("templates/situation_room_app.html"),
        Path("static/situation_room_app.css"),
        Path("static/situation_room_app.js"),
    )
    frontend_revision = max(
        (int(path.stat().st_mtime) for path in frontend_files if path.exists()),
        default=0,
    )
    frontend_version = f"{version}-{frontend_revision}"
    app.jinja_env.globals["frontend_version"] = frontend_version
    use_personal_betting_board = personal_service is not None or runtime is None
    release_settings = release_settings or ReleaseSettings.from_environment()
    app.config["MAX_CONTENT_LENGTH"] = release_settings.maximum_request_bytes
    app.config["SECRET_KEY"] = release_settings.secret_key or "development-only"
    answer_service = answer_service or AnswerService()
    runtime = runtime or build_production_runtime()
    situation_room_service = situation_room_service or SituationRoomService()
    compute_repository = compute_repository or ComputeJobRepository(
        os.getenv("RAGHUB_COMPUTE_DATABASE", "data/compute_jobs.db")
    )
    worker_token = (
        worker_token
        if worker_token is not None
        else os.getenv("RAGHUB_WORKER_TOKEN", "")
    )

    def _lifecycle_service() -> IntelligenceLifecycleService:
        nonlocal lifecycle_service
        if lifecycle_service is None:
            database_path = Path(
                os.getenv(
                    "RAGHUB_LIFECYCLE_DB",
                    str(Path("data") / "intelligence_lifecycle.db"),
                )
            )
            database_path.parent.mkdir(parents=True, exist_ok=True)
            lifecycle_service = IntelligenceLifecycleService(
                SQLiteLifecycleRepository(database_path)
            )
        return lifecycle_service

    def _default_health_service() -> SystemHealthService:
        def database_probe() -> dict[str, object]:
            start = perf_counter()
            database_path = Path(release_settings.lifecycle_database)
            database_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(database_path, timeout=2.0) as connection:
                connection.execute("SELECT 1").fetchone()
            return {
                "status": "healthy",
                "latency_ms": round((perf_counter() - start) * 1000, 2),
            }

        return SystemHealthService(
            probes={
                "api": lambda: {"status": "healthy", "latency_ms": 0},
                "database": database_probe,
                "graph": lambda: {
                    "status": "unknown",
                    "cause": "Graph has not been refreshed during this process.",
                    "cached_available": False,
                },
                "odds_feed": lambda: {
                    "status": "unconfigured",
                    "cause": (
                        "No production sportsbook adapter is configured; "
                        "rankings remain evidence-gated."
                    ),
                    "cached_available": False,
                },
            }
        )

    health_service = health_service or _default_health_service()
    request_windows: dict[str, deque[float]] = defaultdict(deque)

    def _personal_service() -> PersonalEditionService:
        nonlocal personal_service
        if personal_service is None:
            personal_service = PersonalEditionService.from_environment()
        return personal_service

    def _overview_service() -> OverviewService:
        nonlocal overview_service
        if overview_service is None:
            overview_service = OverviewService(
                personal_service=_personal_service(),
                situation_room_service=situation_room_service,
                compute_repository=compute_repository,
            )
        return overview_service

    @app.before_request
    def enforce_release_guardrails():
        supplied = request.headers.get("X-Request-ID", "").strip()
        g.request_id = supplied[:128] or f"req-{uuid4().hex}"
        now = time()
        address = request.remote_addr or "unknown"
        window = request_windows[address]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= release_settings.rate_limit_per_minute:
            return {
                "error": "Rate limit exceeded.",
                "retry_after_seconds": max(1, int(60 - (now - window[0]))),
                "request_id": g.request_id,
            }, 429
        window.append(now)
        if release_settings.demo_read_only and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            return {
                "error": "This deployment is in read-only demo mode.",
                "request_id": g.request_id,
            }, 403
        return None

    @app.after_request
    def add_release_headers(response):
        response.headers["X-Request-ID"] = g.get("request_id", "unavailable")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["X-SIP-Frontend-Version"] = frontend_version
        if response.mimetype in {
            "text/html",
            "text/css",
            "application/javascript",
            "text/javascript",
        }:
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    @app.errorhandler(413)
    def request_too_large(_error):
        return {
            "error": "Request body exceeds the configured size limit.",
            "request_id": g.get("request_id", "unavailable"),
        }, 413

    @app.errorhandler(500)
    def internal_error(_error):
        app.logger.exception(
            "request failed request_id=%s path=%s",
            g.get("request_id", "unavailable"),
            request.path,
        )
        return {
            "error": "The request could not be completed.",
            "request_id": g.get("request_id", "unavailable"),
        }, 500

    def _ui_handler():
        return create_feature_ui_instance(runtime, query_params=request.args.to_dict())

    def _build_manual_player() -> Player:
        name = (request.form.get("name") or "").strip()
        team = (request.form.get("team") or "").strip()
        history_text = (request.form.get("performance_history") or "").strip()
        try:
            history = [
                int(item.strip()) for item in history_text.split(",") if item.strip()
            ]
        except ValueError:
            history = []
        return Player(
            name=name or "Demo Player",
            team=team or "Demo Team",
            performance_history=history,
        )

    @app.route("/")
    def home():
        snapshot = _personal_service().snapshot()
        return render_template("sip_personal.html", snapshot=snapshot)

    @app.get("/overview")
    def overview():
        return render_template("overview.html")

    @app.get("/api/overview")
    def overview_snapshot():
        return _overview_service().snapshot()

    @app.route("/wnba")
    def wnba_home():
        snapshot = _personal_service().wnba_snapshot()
        return render_template("sip_wnba.html", snapshot=snapshot)

    @app.route("/legacy")
    def legacy_home():
        return _ui_handler().render_form()

    @app.route("/api")
    def api_home():
        return {
            "application": "RAGHub",
            "status": "running",
            "version": "1.0.0",
        }

    @app.route("/health")
    def health():
        return {"status": "healthy", "project": "raghub"}

    @app.get("/api/system/health")
    def system_health():
        sip = _personal_service().snapshot()
        release = health_service.snapshot()
        return {
            **release,
            "status": (
                "healthy"
                if sip["feed"]["status"] == "healthy"
                and sip["model"]["calibration_status"] == "calibrated"
                else "degraded"
            ),
            "release": release,
            "sip": {
                "mode": sip["mode"],
                "feed": sip["feed"],
                "model": sip["model"],
                "events": len(sip["events"]),
                "forecasts": sip.get("forecast_count", 0),
                "evaluations": len(sip["evaluations"]),
                "qualified_choices": len(sip["qualified_choices"]),
                "no_bet_results": len(sip["no_bet_results"]),
            },
            "situation_room": {
                "forecasts": situation_room_service.repository.count_forecasts(),
                "evidence": situation_room_service.repository.count_evidence(),
                "latest_autonomy_cycle": (
                    situation_room_service.get_latest_autonomy_cycle()
                ),
            },
            "compute_infrastructure": compute_repository.health(),
            "frontend_version": frontend_version,
        }

    @app.get("/api/system/compute-health")
    def compute_health():
        return compute_repository.health()

    def _authorized_worker():
        authorization = request.headers.get("Authorization", "")
        supplied = (
            authorization.removeprefix("Bearer ").strip()
            if authorization.startswith("Bearer ")
            else request.headers.get("X-RAGHub-Worker-Token", "").strip()
        )
        if not worker_token or not hmac.compare_digest(supplied, worker_token):
            return {
                "error": "Worker authentication required.",
                "request_id": g.get("request_id", "unavailable"),
            }, 401
        return None

    @app.post("/api/internal/workers/register")
    def register_compute_worker():
        rejected = _authorized_worker()
        if rejected:
            return rejected
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()[:100]
        version_text = str(payload.get("version", "")).strip()[:50]
        capabilities = tuple(
            str(value).strip()[:50]
            for value in payload.get("capabilities", [])
            if str(value).strip()
        )
        if not name or not version_text:
            return {"error": "Worker name and version are required."}, 400
        compute_repository.register_worker(name, version_text, capabilities)
        return {"status": "registered", "worker": name}

    @app.post("/api/internal/workers/heartbeat")
    def heartbeat_compute_worker():
        rejected = _authorized_worker()
        if rejected:
            return rejected
        payload = request.get_json(silent=True) or {}
        try:
            compute_repository.heartbeat_worker(
                str(payload.get("worker_name", "")),
                payload.get("active_job_id"),
            )
        except KeyError:
            return {"error": "Unknown worker."}, 404
        return {"status": "accepted"}

    @app.get("/api/internal/compute-jobs/next")
    def next_compute_job():
        rejected = _authorized_worker()
        if rejected:
            return rejected
        worker_name = request.args.get("worker_name", "").strip()
        capabilities = compute_repository.worker_capabilities(worker_name)
        if capabilities is None:
            return {"error": "Unknown worker."}, 404
        job = compute_repository.claim_next(
            worker_name=worker_name,
            capabilities=capabilities,
            lease_seconds=120,
        )
        if job is None:
            return {"job": None}
        compute_repository.heartbeat_worker(worker_name, job.id)
        return {"job": asdict(job)}

    @app.post("/api/internal/compute-jobs/<job_id>/heartbeat")
    def heartbeat_compute_job(job_id: str):
        rejected = _authorized_worker()
        if rejected:
            return rejected
        payload = request.get_json(silent=True) or {}
        worker_name = str(payload.get("worker_name", ""))
        job = compute_repository.get_job(job_id)
        if job is None or job.assigned_worker != worker_name:
            return {"error": "Job is not assigned to this worker."}, 409
        compute_repository.heartbeat_worker(worker_name, job_id)
        return {"status": "accepted"}

    @app.post("/api/internal/compute-jobs/<job_id>/complete")
    def complete_compute_job(job_id: str):
        rejected = _authorized_worker()
        if rejected:
            return rejected
        payload = request.get_json(silent=True) or {}
        try:
            job = compute_repository.complete_job(
                job_id,
                str(payload.get("worker_name", "")),
                payload=dict(payload.get("result") or {}),
                checksum=str(payload.get("checksum", "")),
            )
        except (KeyError, ValueError) as error:
            return {"error": str(error)}, 409
        return {"job": asdict(job)}

    @app.post("/api/internal/compute-jobs/<job_id>/fail")
    def fail_compute_job(job_id: str):
        rejected = _authorized_worker()
        if rejected:
            return rejected
        payload = request.get_json(silent=True) or {}
        try:
            job = compute_repository.fail_job(
                job_id,
                str(payload.get("worker_name", "")),
                str(payload.get("error", "Worker computation failed.")),
            )
        except KeyError:
            return {"error": "Unknown job."}, 404
        return {"job": asdict(job)}

    @app.get("/api/sip/dashboard")
    def sip_dashboard():
        return _personal_service().snapshot()

    @app.get("/api/sip/wnba")
    def sip_wnba_dashboard():
        return _personal_service().wnba_snapshot()

    @app.get("/api/sip/games")
    def sip_games():
        return {"events": _personal_service().snapshot()["events"]}

    @app.get("/api/sip/games/<path:canonical_event_id>")
    def sip_game_detail(canonical_event_id: str):
        detail = _personal_service().event_detail(canonical_event_id)
        if detail is None:
            return {"error": "Canonical event was not found."}, 404
        return detail

    @app.get("/api/sip/choices")
    def sip_choices():
        snapshot = _personal_service().snapshot()
        return {
            "qualified_choices": snapshot["qualified_choices"],
            "no_bet_results": snapshot["no_bet_results"],
        }

    @app.get("/api/sip/status")
    def sip_status():
        snapshot = _personal_service().snapshot()
        return {
            "version": snapshot["version"],
            "edition": snapshot["edition"],
            "mode": snapshot["mode"],
            "feed": snapshot["feed"],
            "model": snapshot["model"],
            "scheduler": snapshot["scheduler"],
            "thresholds": snapshot["thresholds"],
            "generated_at": snapshot["generated_at"],
        }

    @app.post("/api/sip/refresh")
    def sip_refresh():
        return _personal_service().scheduler.run_pending(force=True)

    def _run_replay_demo() -> dict[str, object]:
        if replay_demo_factory is not None:
            return replay_demo_factory().run()
        with TemporaryDirectory(prefix="raghub-demo-") as directory:
            return NBAReplayDemo(Path(directory) / "demo.db").run()

    @app.get("/api/demo/nba-opportunity")
    def nba_opportunity_demo_api():
        return _run_replay_demo()

    @app.get("/demo/nba")
    def nba_opportunity_demo():
        return render_template(
            "nba_opportunity_demo.html",
            demo=_run_replay_demo(),
        )

    @app.route("/player")
    def player_detail():
        player_id = request.args.get("player_id", "")
        return _ui_handler().render_player_detail(player_id)

    @app.route("/calibration")
    def calibration_detail():
        return _ui_handler().render_calibration_detail()

    @app.route("/betting")
    def betting_intelligence():
        replay_provider = getattr(runtime, "get_betting_replay_board", None)
        replay_board = replay_provider() if replay_provider is not None else {}
        handler = _ui_handler()
        if use_personal_betting_board:
            handler.betting_board_provider = _personal_service().betting_board
        handler.betting_replay_provider = lambda: replay_board
        response = make_response(
            render_template(
                "betting_intelligence.html",
                betting_html=handler.render_betting_intelligence(),
                replay=replay_board,
            )
        )
        response.headers["X-Betting-Replay-Status"] = str(
            replay_board.get("status") or "unavailable"
        )
        return response

    @app.route("/refresh", methods=["POST"])
    def refresh_dashboard():
        runtime.refresh()
        return redirect("/", code=303)

    @app.route("/load-history", methods=["POST"])
    def load_history():
        runtime.load_history()
        return redirect("/", code=303)

    @app.route("/build", methods=["POST"])
    def build_manual_features():
        player = _build_manual_player()
        registry = FeatureRegistry()
        registry.register_builder(TrendFeatureBuilder())
        facade = FeatureFacade(registry)
        view = facade.build(player, ["trend"])
        return _ui_handler().render_result(player, view)

    @app.route("/api/dashboard")
    def dashboard_snapshot():
        return runtime.get_dashboard_snapshot(
            league=request.args.get("league") or None,
            competition=request.args.get("competition") or None,
            research_query=request.args.get("research_q") or None,
        )

    @app.route("/situation-room")
    def situation_room():
        focus = request.args.get("focus") or "global"
        query = request.args.get("q") or request.args.get("query") or ""
        return render_template(
            "situation_room_app.html",
            focus=focus,
            query=query,
        )

    @app.route("/situation-room/evidence")
    def situation_room_evidence_explorer():
        return render_template(
            "evidence_explorer.html",
            focus=request.args.get("focus") or "global",
            query=request.args.get("q") or request.args.get("query") or "",
        )

    @app.route("/situation-room/legacy")
    def situation_room_legacy():
        snapshot = situation_room_service.get_snapshot(
            focus=request.args.get("focus") or "global",
            query=request.args.get("q") or request.args.get("query") or None,
        )
        return render_situation_room(snapshot)

    @app.route("/api/situation-room")
    def api_situation_room():
        return situation_room_service.get_snapshot(
            focus=request.args.get("focus") or "global",
            query=request.args.get("q") or request.args.get("query") or None,
        )

    @app.get("/api/situation-room/brief")
    @app.get("/api/situation-room/developments")
    def api_situation_room_brief():
        return situation_room_service.get_brief(
            focus=request.args.get("focus") or "global",
            query=request.args.get("q") or request.args.get("query") or None,
        )

    @app.get("/api/forecasts")
    def api_forecasts():
        return situation_room_service.list_forecasts()

    @app.get("/api/forecasts/<forecast_id>")
    def api_forecast(forecast_id: str):
        forecast = situation_room_service.get_forecast(forecast_id)
        if forecast is None:
            return {
                "status": "not_found",
                "forecast_id": forecast_id,
                "message": "Forecast record is not available.",
            }, 404
        return {"forecast": forecast}

    @app.get("/api/forecasts/<forecast_id>/history")
    def api_forecast_history(forecast_id: str):
        forecast = situation_room_service.get_forecast(forecast_id)
        if forecast is None:
            return {
                "status": "not_found",
                "forecast_id": forecast_id,
                "history": [],
            }, 404
        history = situation_room_service.get_forecast_history(forecast_id)
        return {
            "forecast_id": forecast_id,
            "count": len(history),
            "history": history,
        }

    @app.get("/api/evidence/<evidence_id>")
    def api_situation_room_evidence(evidence_id: str):
        evidence = situation_room_service.get_evidence(evidence_id)
        if evidence is None:
            return {
                "status": "evidence_unavailable",
                "evidence_id": evidence_id,
                "message": "Evidence unavailable. This item cannot be treated as verified.",
            }, 404
        return {"evidence": evidence}

    @app.get("/api/investigations")
    def api_situation_room_investigations():
        return situation_room_service.list_investigations(
            focus=request.args.get("focus") or "global",
            query=request.args.get("q") or request.args.get("query") or None,
        )

    @app.get("/api/investigations/<investigation_id>")
    def api_situation_room_investigation(investigation_id: str):
        investigation = situation_room_service.get_investigation(
            investigation_id,
            focus=request.args.get("focus") or "global",
        )
        if investigation is None:
            return {
                "status": "not_found",
                "investigation_id": investigation_id,
                "message": "Investigation record is not available.",
            }, 404
        return {"investigation": investigation}

    @app.get("/api/watchlists/changes")
    def api_situation_room_watchlist_changes():
        return situation_room_service.get_watchlist_changes(
            focus=request.args.get("focus") or "global",
            query=request.args.get("q") or request.args.get("query") or None,
        )

    @app.get("/api/autonomy/cycles/latest")
    def api_situation_room_latest_cycle():
        return situation_room_service.get_latest_autonomy_cycle()

    @app.get("/api/situation-room/health")
    def api_situation_room_system_health():
        return situation_room_service.get_system_health(
            focus=request.args.get("focus") or "global",
            query=request.args.get("q") or request.args.get("query") or None,
        )

    @app.route("/api/situation-room/graph")
    def api_situation_room_graph():
        try:
            return situation_room_service.get_graph(
                focus=request.args.get("focus") or "global",
                query=request.args.get("q") or request.args.get("query") or None,
            )
        except Exception as error:
            return {
                "status": "degraded",
                "generated_at": None,
                "summary": {
                    "node_count": 0,
                    "edge_count": 0,
                    "entity_types": [],
                },
                "nodes": [],
                "edges": [],
                "error": str(error),
            }

    @app.route("/api/situation-room/predict", methods=["GET", "POST"])
    def api_situation_room_predict():
        payload = request.get_json(silent=True) if request.method == "POST" else {}
        payload = payload or {}

        focus = str(payload.get("focus") or request.args.get("focus") or "global")
        query = str(
            payload.get("query")
            or payload.get("q")
            or request.args.get("q")
            or request.args.get("query")
            or ""
        )
        scenario = str(
            payload.get("scenario") or request.args.get("scenario") or query or focus
        )
        horizon_value = (
            payload.get("horizon_days")
            if "horizon_days" in payload
            else request.args.get("horizon_days")
        )
        mode = str(
            payload.get("confidence_mode")
            or request.args.get("confidence_mode")
            or "balanced"
        )

        try:
            horizon_days = int(horizon_value) if horizon_value is not None else 30
        except (TypeError, ValueError):
            return {"error": "horizon_days must be an integer."}, 400

        return situation_room_service.get_prediction(
            focus=focus,
            query=query or None,
            scenario=scenario,
            horizon_days=horizon_days,
            confidence_mode=mode,
        )

    @app.route("/api/situation-room/situations/<situation_id>")
    def api_situation_room_situation(situation_id: str):
        situation = situation_room_service.get_situation(
            situation_id,
            focus=str(request.args.get("focus") or "global"),
            query=request.args.get("q") or request.args.get("query"),
        )
        if situation is None:
            return {"error": "Situation not found.", "situation_id": situation_id}, 404
        return situation

    @app.route("/api/situation-room/autonomy", methods=["GET", "POST"])
    def api_situation_room_autonomy():
        if request.method == "GET":
            return situation_room_service.get_autonomy_status()

        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip().lower()
        focus = str(payload.get("focus") or request.args.get("focus") or "global")
        query = str(
            payload.get("query")
            or payload.get("q")
            or request.args.get("q")
            or request.args.get("query")
            or ""
        )

        if action in {"start", "enable"}:
            interval_value = payload.get("interval_seconds")
            interval_seconds = (
                int(interval_value) if interval_value is not None else None
            )
            return situation_room_service.set_autonomy(
                enabled=True,
                interval_seconds=interval_seconds,
                focus=focus,
                query=query,
                objective=str(payload.get("objective") or ""),
            )

        if action in {"stop", "disable"}:
            return situation_room_service.set_autonomy(
                enabled=False,
                focus=focus,
                query=query,
            )

        if action in {"run", "run-once", "cycle"}:
            return situation_room_service.run_autonomy_cycle(
                focus=focus,
                query=query or None,
                force=True,
            )

        return {
            "error": "Unknown action. Use start, stop, or run.",
            "received": action,
        }, 400

    @app.post("/api/intelligence/situations")
    def create_intelligence_situation():
        service = _lifecycle_service()
        payload = request.get_json(silent=True) or {}
        try:
            situation = service.create_situation(
                situation_id=str(payload["id"]),
                title=str(payload["title"]),
                objective=str(payload["objective"]),
                observation=str(payload["observation"]),
                observed_at=payload.get("observed_at"),
            )
        except (KeyError, TypeError, ValueError) as error:
            return {"error": str(error)}, 400
        return asdict(situation), 201

    @app.get("/api/intelligence/situations/<path:situation_id>")
    def get_intelligence_situation(situation_id: str):
        service = _lifecycle_service()
        situation = service.get_situation(situation_id)
        if situation is None:
            return {"error": "Situation not found."}, 404
        return asdict(situation)

    @app.post("/api/intelligence/situations/<path:situation_id>/events")
    def append_intelligence_event(situation_id: str):
        service = _lifecycle_service()
        payload = request.get_json(silent=True) or {}
        event_type = str(payload.get("event_type") or "")
        occurred_at = payload.get("occurred_at")
        handlers = {
            "observation_recorded": lambda: service.record_observation(
                situation_id, str(payload["observation"]), occurred_at=occurred_at
            ),
            "evidence_collected": lambda: service.record_evidence(
                situation_id, dict(payload["evidence"]), occurred_at=occurred_at
            ),
            "evidence_gap_recorded": lambda: service.record_evidence_gap(
                situation_id, str(payload["gap"]), occurred_at=occurred_at
            ),
            "hypothesis_added": lambda: service.add_hypothesis(
                situation_id, dict(payload["hypothesis"]), occurred_at=occurred_at
            ),
            "hypothesis_evaluated": lambda: service.evaluate_hypothesis(
                situation_id,
                str(payload["hypothesis_id"]),
                confidence=float(payload["confidence"]),
                status=str(payload["status"]),
                indicator_results=dict(payload["indicator_results"]),
                supporting_evidence=list(payload["supporting_evidence"]),
                contradicting_evidence=list(payload["contradicting_evidence"]),
                reason=str(payload["reason"]),
                occurred_at=occurred_at,
            ),
            "forecast_added": lambda: service.add_forecast(
                situation_id, dict(payload["forecast"]), occurred_at=occurred_at
            ),
            "action_recommended": lambda: service.add_recommended_action(
                situation_id, str(payload["action"]), occurred_at=occurred_at
            ),
            "monitoring_rule_added": lambda: service.add_monitoring_rule(
                situation_id, dict(payload["rule"]), occurred_at=occurred_at
            ),
            "outcome_recorded": lambda: service.record_outcome(
                situation_id, dict(payload["outcome"]), occurred_at=occurred_at
            ),
            "lesson_recorded": lambda: service.record_lesson(
                situation_id, str(payload["lesson"]), occurred_at=occurred_at
            ),
        }
        handler = handlers.get(event_type)
        if handler is None:
            return {"error": f"Unsupported lifecycle event: {event_type}"}, 400
        try:
            event = handler()
        except (KeyError, TypeError, ValueError) as error:
            return {"error": str(error)}, 400
        return asdict(event), 201

    @app.get("/api/intelligence/situations/<path:situation_id>/history")
    def get_intelligence_history(situation_id: str):
        service = _lifecycle_service()
        return {
            "situation_id": situation_id,
            "events": service.timeline(situation_id),
        }

    @app.get("/api/intelligence/situations/<path:situation_id>/graph")
    def get_intelligence_claim_graph(situation_id: str):
        service = _lifecycle_service()
        try:
            return service.claim_graph(situation_id)
        except KeyError:
            return {"error": "Situation not found."}, 404

    @app.get("/api/intelligence/forecast-quality")
    def get_intelligence_forecast_quality():
        return _lifecycle_service().evaluate_forecasts()

    @app.get("/api/intelligence/research-memory")
    def get_intelligence_research_memory():
        return _lifecycle_service().build_research_memory()

    @app.route("/api/answer/stream")
    def stream_answer():
        query = request.args.get("q") or request.args.get("query") or ""
        if not query:
            return {"error": "q is required"}, 400

        source = request.args.get("source", "wikipedia")
        limit = request.args.get("limit", default=10, type=int)
        session_id = request.args.get("session_id")

        def generate():
            try:
                yield from answer_service.stream_answer(
                    query=query,
                    source=source,
                    limit=limit,
                    session_id=session_id,
                )
            except TypeError:
                yield from answer_service.stream_answer(
                    query=query,
                    source=source,
                    limit=limit,
                )

        return Response(generate(), mimetype="text/plain")

    @app.route("/api/answer")
    def answer():
        query = request.args.get("q") or request.args.get("query") or ""
        if not query:
            return {"error": "q is required"}, 400

        source = request.args.get("source", "wikipedia")
        limit = request.args.get("limit", default=10, type=int)
        session_id = request.args.get("session_id")
        providers = request.args.get("providers")
        if providers:
            answer_service.retrieval_service = RetrievalService(
                provider_names=[
                    name.strip() for name in providers.split(",") if name.strip()
                ]
            )
        try:
            return answer_service.answer(
                query=query, source=source, limit=limit, session_id=session_id
            )
        except TypeError:
            return answer_service.answer(query=query, source=source, limit=limit)

    return app


app = create_app()


if __name__ == "__main__":
    load_dotenv(Path(__file__).with_name(".env"))
    personal = PersonalEditionService.from_environment()
    app.run(
        host=personal.settings.host,
        port=personal.settings.port,
        debug=False,
        use_reloader=False,
    )
