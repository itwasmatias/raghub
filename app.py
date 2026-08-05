from dataclasses import asdict
import os
from pathlib import Path
import sqlite3
from decimal import Decimal
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
from services.wager_calculator import PracticeWagerCalculator
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
from sports.personal.execution import (
    FailureCode,
    ExecutionOrder,
    ExecutionMode,
    ExecutionOrchestrator,
    OrderState,
    StateMachineError,
)
from sports.personal.wagering import (
    Bet,
    BetStatus,
    DataQualityStatus,
    Event,
    ExposureSnapshot,
    LedgerTransaction,
    LedgerTransactionType,
    Market,
    MarketStatus,
    MarketTargetType,
    ModelForecast,
    OrderIntent,
    OrderIntentValidationEngine,
    OrderValidationResult,
    Outcome,
    OutcomeResult,
    OutcomeStatus,
    Position,
    PositionMode,
    PositionStatus,
    ProbabilitySnapshot,
    Quote,
    SyntheticPositionValuation,
    ValidationIssue,
    ValidationCode,
    american_to_decimal,
    compute_synthetic_shares,
    estimate_synthetic_value,
    expected_value_per_dollar,
    implied_probability_from_american,
    money,
    no_vig_probabilities_from_raw,
    now_iso,
    potential_profit_for_american_odds,
)
from sports.compute.repository import ComputeJobRepository
from sports.features.builders.trend_builder import TrendFeatureBuilder
from sports.features.facade import FeatureFacade
from sports.features.registry import FeatureRegistry
from web_ui import create_feature_ui_instance
from tools.ai_controller.operations_api import (
    ControllerOperations,
    create_operations_blueprint,
    validate_bind_address,
)


load_dotenv(Path(__file__).with_name(".env"), override=True)


def _controller_routes_enabled() -> bool:
    return os.getenv("RAGHUB_CONTROLLER_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


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
    controller_operations: ControllerOperations | None = None,
):
    app = Flask(__name__)
    if controller_operations is not None or _controller_routes_enabled():
        if controller_operations is None:
            controller_root = Path(
                os.getenv("RAGHUB_CONTROLLER_ROOT", "data/controller")
            )
            controller_operations = ControllerOperations(
                missions_root=controller_root / "missions",
                queue_root=controller_root / "queue",
                reports_root=controller_root / "reports",
                approvals_root=controller_root / "approvals",
                proposal_ttl_seconds=int(
                    os.getenv("RAGHUB_CONTROLLER_PROPOSAL_TTL_SECONDS", "900")
                ),
                allowed_origins=tuple(
                    origin.strip()
                    for origin in os.getenv(
                        "RAGHUB_CONTROLLER_ALLOWED_ORIGINS", ""
                    ).split(",")
                    if origin.strip()
                ),
            )
        app.register_blueprint(create_operations_blueprint(controller_operations))
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

    _execution_orchestrator: ExecutionOrchestrator | None = None

    def _execution_service() -> ExecutionOrchestrator:
        nonlocal _execution_orchestrator
        if _execution_orchestrator is None:
            direct_enabled = os.getenv(
                "SIP_TRANSACTIONAL_SPORTSBOOK_API_AUTHORIZED", "false"
            ).strip().lower() in {"1", "true", "yes"}
            _execution_orchestrator = ExecutionOrchestrator(
                direct_authorized=direct_enabled
            )
        return _execution_orchestrator

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

    @app.post("/api/sip/practice-wager")
    def sip_practice_wager():
        payload = request.get_json(silent=True) or {}
        try:
            result = PracticeWagerCalculator.calculate(
                american_odds=payload.get("american_odds"),
                wager_amount_usd=payload.get("wager_amount_usd"),
                model_probability=payload.get("model_probability"),
                market_implied_probability=payload.get("market_implied_probability"),
            )
        except ValueError as error:
            return {"error": str(error)}, 400

        return {
            "potential_profit_usd": f"{result['potential_profit_usd']:.2f}",
            "total_return_usd": f"{result['total_return_usd']:.2f}",
            "break_even_probability": f"{result['break_even_probability']:.4f}",
            "model_probability": (
                f"{result['model_probability']:.4f}"
                if result["model_probability"] is not None
                else None
            ),
            "market_implied_probability": (
                f"{result['market_implied_probability']:.4f}"
                if result["market_implied_probability"] is not None
                else None
            ),
            "expected_profit_usd": (
                f"{result['expected_profit_usd']:.2f}"
                if result["expected_profit_usd"] is not None
                else None
            ),
            "expected_return_percentage": (
                f"{result['expected_return_percentage']:.2f}"
                if result["expected_return_percentage"] is not None
                else None
            ),
            "win_outcome_usd": f"{result['win_outcome_usd']:.2f}",
            "loss_outcome_usd": f"{result['loss_outcome_usd']:.2f}",
            "plain_language": result["plain_language"],
            "warning": (
                "Practice Bet Calculator — estimates hypothetical returns only. "
                "It does not place a wager."
            ),
        }

    def _default_account_id() -> str:
        return "sip-default"

    def _ensure_default_account(account_id: str):
        repo = _personal_service().repository
        account = repo.get_bankroll_account(account_id)
        if account is not None:
            return account
        now = now_iso()
        from sports.personal.wagering import BankrollAccount

        seeded = BankrollAccount(
            account_id=account_id,
            currency="USD",
            balance_usd=money("10000"),
            available_usd=money("10000"),
            held_usd=money("0"),
            status="active",
            created_at=now,
            updated_at=now,
            metadata={"seeded_for": "market_wireframe"},
        )
        repo.save_bankroll_account(seeded)
        return seeded

    def _seed_markets_from_persisted_events() -> None:
        repo = _personal_service().repository
        if repo.list_markets(limit=1):
            return
        events = repo.list_events()
        quotes = repo.list_quotes()
        if not events:
            return

        now = now_iso()
        markets = []
        outcomes = []
        market_quotes = []
        snapshots = []
        forecasts = []
        event_catalog = []

        for event in events:
            event_catalog.append(
                Event(
                    id=f"event:{event.canonical_id}",
                    sport=(
                        "baseball"
                        if str(event.league).strip().upper() == "MLB"
                        else "basketball"
                    ),
                    league=event.league,
                    starts_at=event.start_time,
                    home_team=event.home_team_name,
                    away_team=event.away_team_name,
                    status=MarketStatus.UPCOMING,
                    provider="sip-normalized",
                    created_at=now,
                    updated_at=now,
                    metadata={"canonical_event_id": event.canonical_id},
                )
            )

            market_id = f"market:{event.canonical_id}:moneyline"
            markets.append(
                Market(
                    id=market_id,
                    canonical_event_id=event.canonical_id,
                    league=event.league,
                    sport=(
                        "baseball"
                        if str(event.league).strip().upper() == "MLB"
                        else "basketball"
                    ),
                    market_type="moneyline",
                    period="full_game",
                    title=f"{event.away_team_name} at {event.home_team_name}",
                    description="Who wins this game?",
                    status=MarketStatus.UPCOMING,
                    opens_at=event.start_time,
                    closes_at=event.start_time,
                    settlement_rule="Official final score determines winner.",
                    resolution_source="League official box score",
                    created_at=now,
                    updated_at=now,
                    target_type=MarketTargetType.EVENT,
                    provider="sip-normalized",
                    home_team=event.home_team_name,
                    away_team=event.away_team_name,
                    metadata={"popularity_score": 50, "open_exposure": 0},
                )
            )
            home_id = f"outcome:{market_id}:home"
            away_id = f"outcome:{market_id}:away"
            outcomes.append(
                Outcome(
                    id=home_id,
                    market_id=market_id,
                    name=event.home_team_name,
                    selection_key="home",
                    team_id=event.home_team_name.lower().replace(" ", "-"),
                    status=OutcomeStatus.OPEN,
                    result=OutcomeResult.PENDING,
                )
            )
            outcomes.append(
                Outcome(
                    id=away_id,
                    market_id=market_id,
                    name=event.away_team_name,
                    selection_key="away",
                    team_id=event.away_team_name.lower().replace(" ", "-"),
                    status=OutcomeStatus.OPEN,
                    result=OutcomeResult.PENDING,
                )
            )

            market_specific_quotes = [
                item for item in quotes if item.canonical_event_id == event.canonical_id
            ]
            for index, quote in enumerate(market_specific_quotes, start=1):
                outcome_id = home_id if quote.selection == "home" else away_id
                raw_prob = implied_probability_from_american(quote.american_price)
                market_quotes.append(
                    Quote(
                        id=f"quote:{market_id}:{index}",
                        market_id=market_id,
                        outcome_id=outcome_id,
                        provider="sip-normalized",
                        sportsbook=quote.sportsbook,
                        american_odds=quote.american_price,
                        decimal_odds=american_to_decimal(quote.american_price),
                        implied_probability=raw_prob,
                        observed_at=quote.observed_at,
                        is_best_price=False,
                        is_stale=False,
                        quote_status=DataQualityStatus.VERIFIED,
                    )
                )

            # Build additional event markets and prop wireframes.
            spread_market_id = f"market:{event.canonical_id}:spread"
            total_market_id = f"market:{event.canonical_id}:total"
            team_prop_market_id = f"market:{event.canonical_id}:team-prop"
            player_prop_market_id = f"market:{event.canonical_id}:player-prop"

            markets.extend(
                [
                    Market(
                        id=spread_market_id,
                        canonical_event_id=event.canonical_id,
                        league=event.league,
                        sport=(
                            "baseball"
                            if str(event.league).strip().upper() == "MLB"
                            else "basketball"
                        ),
                        market_type="spread",
                        period="full_game",
                        title=f"{event.away_team_name} at {event.home_team_name} Spread",
                        description="Which side covers the spread?",
                        status=MarketStatus.UPCOMING,
                        opens_at=event.start_time,
                        closes_at=event.start_time,
                        settlement_rule="Official score with line adjustment.",
                        resolution_source="League official box score",
                        created_at=now,
                        updated_at=now,
                        target_type=MarketTargetType.EVENT,
                        provider="sip-normalized",
                        home_team=event.home_team_name,
                        away_team=event.away_team_name,
                    ),
                    Market(
                        id=total_market_id,
                        canonical_event_id=event.canonical_id,
                        league=event.league,
                        sport=(
                            "baseball"
                            if str(event.league).strip().upper() == "MLB"
                            else "basketball"
                        ),
                        market_type="total",
                        period="full_game",
                        title=f"{event.away_team_name} at {event.home_team_name} Total",
                        description="Will total points/runs finish over or under?",
                        status=MarketStatus.UPCOMING,
                        opens_at=event.start_time,
                        closes_at=event.start_time,
                        settlement_rule="Official game total versus listed threshold.",
                        resolution_source="League official box score",
                        created_at=now,
                        updated_at=now,
                        target_type=MarketTargetType.EVENT,
                        provider="sip-normalized",
                        home_team=event.home_team_name,
                        away_team=event.away_team_name,
                    ),
                    Market(
                        id=team_prop_market_id,
                        canonical_event_id=event.canonical_id,
                        league=event.league,
                        sport=(
                            "baseball"
                            if str(event.league).strip().upper() == "MLB"
                            else "basketball"
                        ),
                        market_type="team_prop",
                        period="full_game",
                        title=f"{event.home_team_name} Team Total Over/Under",
                        description="Team scoring prop market.",
                        status=MarketStatus.UPCOMING,
                        opens_at=event.start_time,
                        closes_at=event.start_time,
                        settlement_rule="Official team scoring against threshold.",
                        resolution_source="League official box score",
                        created_at=now,
                        updated_at=now,
                        target_type=MarketTargetType.TEAM_PROP,
                        provider="sip-normalized",
                        home_team=event.home_team_name,
                        away_team=event.away_team_name,
                        prop_statistic="team_points",
                        prop_threshold=Decimal("84.5"),
                    ),
                    Market(
                        id=player_prop_market_id,
                        canonical_event_id=event.canonical_id,
                        league=event.league,
                        sport=(
                            "baseball"
                            if str(event.league).strip().upper() == "MLB"
                            else "basketball"
                        ),
                        market_type="player_prop",
                        period="full_game",
                        title=f"{event.home_team_name} Star Player Points O/U",
                        description="Player prop market.",
                        status=MarketStatus.UPCOMING,
                        opens_at=event.start_time,
                        closes_at=event.start_time,
                        settlement_rule="Official player statbook against threshold.",
                        resolution_source="League official box score",
                        created_at=now,
                        updated_at=now,
                        target_type=MarketTargetType.PLAYER_PROP,
                        provider="sip-normalized",
                        home_team=event.home_team_name,
                        away_team=event.away_team_name,
                        player_id="player-star",
                        player_name="Star Player",
                        player_team_id=event.home_team_name.lower().replace(" ", "-"),
                        prop_statistic="points",
                        prop_threshold=Decimal("24.5"),
                    ),
                ]
            )

            generated_prop_outcomes = [
                Outcome(
                    id=f"outcome:{spread_market_id}:home-minus",
                    market_id=spread_market_id,
                    name=f"{event.home_team_name} -3.5",
                    selection_key="home_minus",
                    team_id=event.home_team_name.lower().replace(" ", "-"),
                    status=OutcomeStatus.UPCOMING,
                    result=OutcomeResult.PENDING,
                ),
                Outcome(
                    id=f"outcome:{spread_market_id}:away-plus",
                    market_id=spread_market_id,
                    name=f"{event.away_team_name} +3.5",
                    selection_key="away_plus",
                    team_id=event.away_team_name.lower().replace(" ", "-"),
                    status=OutcomeStatus.UPCOMING,
                    result=OutcomeResult.PENDING,
                ),
                Outcome(
                    id=f"outcome:{total_market_id}:over",
                    market_id=total_market_id,
                    name="Over 164.5",
                    selection_key="over",
                    team_id=None,
                    status=OutcomeStatus.UPCOMING,
                    result=OutcomeResult.PENDING,
                ),
                Outcome(
                    id=f"outcome:{total_market_id}:under",
                    market_id=total_market_id,
                    name="Under 164.5",
                    selection_key="under",
                    team_id=None,
                    status=OutcomeStatus.UPCOMING,
                    result=OutcomeResult.PENDING,
                ),
                Outcome(
                    id=f"outcome:{team_prop_market_id}:over",
                    market_id=team_prop_market_id,
                    name=f"{event.home_team_name} Over 84.5",
                    selection_key="over",
                    team_id=event.home_team_name.lower().replace(" ", "-"),
                    status=OutcomeStatus.UPCOMING,
                    result=OutcomeResult.PENDING,
                    statistic="team_points",
                    threshold=Decimal("84.5"),
                    direction="over",
                ),
                Outcome(
                    id=f"outcome:{team_prop_market_id}:under",
                    market_id=team_prop_market_id,
                    name=f"{event.home_team_name} Under 84.5",
                    selection_key="under",
                    team_id=event.home_team_name.lower().replace(" ", "-"),
                    status=OutcomeStatus.UPCOMING,
                    result=OutcomeResult.PENDING,
                    statistic="team_points",
                    threshold=Decimal("84.5"),
                    direction="under",
                ),
                Outcome(
                    id=f"outcome:{player_prop_market_id}:over",
                    market_id=player_prop_market_id,
                    name="Star Player Over 24.5",
                    selection_key="over",
                    team_id=event.home_team_name.lower().replace(" ", "-"),
                    status=OutcomeStatus.UPCOMING,
                    result=OutcomeResult.PENDING,
                    player_id="player-star",
                    player_name="Star Player",
                    statistic="points",
                    threshold=Decimal("24.5"),
                    direction="over",
                ),
                Outcome(
                    id=f"outcome:{player_prop_market_id}:under",
                    market_id=player_prop_market_id,
                    name="Star Player Under 24.5",
                    selection_key="under",
                    team_id=event.home_team_name.lower().replace(" ", "-"),
                    status=OutcomeStatus.UPCOMING,
                    result=OutcomeResult.PENDING,
                    player_id="player-star",
                    player_name="Star Player",
                    statistic="points",
                    threshold=Decimal("24.5"),
                    direction="under",
                ),
            ]
            outcomes.extend(generated_prop_outcomes)

            for extra_index, generated in enumerate(generated_prop_outcomes, start=1):
                base_odds = -110 if generated.direction != "under" else 100
                dec = american_to_decimal(base_odds)
                raw = implied_probability_from_american(base_odds)
                market_quotes.append(
                    Quote(
                        id=f"quote:{generated.market_id}:{extra_index}",
                        market_id=generated.market_id,
                        outcome_id=generated.id,
                        provider="sip-normalized",
                        sportsbook="consensus",
                        american_odds=base_odds,
                        decimal_odds=dec,
                        implied_probability=raw,
                        observed_at=now,
                        is_best_price=True,
                        is_stale=False,
                        quote_status=DataQualityStatus.DEGRADED,
                    )
                )

        best_by_outcome: dict[str, tuple[int, int]] = {}
        for idx, quote in enumerate(market_quotes):
            score = quote.american_odds
            current = best_by_outcome.get(quote.outcome_id)
            if current is None or score > current[1]:
                best_by_outcome[quote.outcome_id] = (idx, score)
        for idx, _score in best_by_outcome.values():
            current = market_quotes[idx]
            market_quotes[idx] = Quote(
                id=current.id,
                market_id=current.market_id,
                outcome_id=current.outcome_id,
                provider=current.provider,
                sportsbook=current.sportsbook,
                american_odds=current.american_odds,
                decimal_odds=current.decimal_odds,
                implied_probability=current.implied_probability,
                observed_at=current.observed_at,
                is_best_price=True,
                is_stale=current.is_stale,
                quote_status=current.quote_status,
            )

        repo.upsert_market_bundle(
            markets=markets, outcomes=outcomes, quotes=market_quotes
        )
        repo.upsert_event_catalog(event_catalog)

        latest_by_outcome: dict[str, list[Quote]] = {}
        for quote in market_quotes:
            latest_by_outcome.setdefault(quote.outcome_id, []).append(quote)
        for bucket in latest_by_outcome.values():
            raw_values = [item.implied_probability for item in bucket]
            normalized = no_vig_probabilities_from_raw(raw_values)
            for idx, quote in enumerate(bucket):
                no_vig = (
                    normalized[idx]
                    if idx < len(normalized)
                    else quote.implied_probability
                )
                sip_probability = (
                    no_vig + Decimal("0.0300")
                    if no_vig < Decimal("0.9700")
                    else Decimal("0.9700")
                )
                edge = sip_probability - no_vig
                expected = expected_value_per_dollar(
                    sip_probability, quote.decimal_odds
                )
                snapshots.append(
                    ProbabilitySnapshot(
                        id=f"ps:{quote.id}",
                        market_id=quote.market_id,
                        outcome_id=quote.outcome_id,
                        provider=quote.provider,
                        sportsbook=quote.sportsbook,
                        american_odds=quote.american_odds,
                        decimal_odds=quote.decimal_odds,
                        raw_implied_probability=quote.implied_probability,
                        no_vig_probability=no_vig,
                        sportsbook_consensus_probability=no_vig,
                        sip_adjusted_probability=sip_probability,
                        edge=edge,
                        expected_value=expected,
                        quote_timestamp=quote.observed_at,
                        forecast_timestamp=now,
                        model_version="sip-market-probability-v1",
                        data_quality_status=quote.quote_status,
                    )
                )
                forecasts.append(
                    ModelForecast(
                        id=f"forecast:{quote.id}",
                        market_id=quote.market_id,
                        outcome_id=quote.outcome_id,
                        probability=sip_probability,
                        generated_at=now,
                        model_version="sip-market-probability-v1",
                        feature_version="market-feed-v1",
                        data_quality_status=quote.quote_status,
                        notes=("seeded from persisted quotes",),
                    )
                )

        repo.save_probability_snapshots(snapshots)
        repo.save_model_forecasts_v2(forecasts)

    def _build_market_feed(
        *,
        sport: str | None,
        league: str | None,
        date: str | None,
        team: str | None,
        player: str | None,
        market_type: str | None,
        provider: str | None,
        search: str | None,
        status: str | None,
        sort_by: str | None,
        account_id: str,
    ):
        repo = _personal_service().repository
        _seed_markets_from_persisted_events()
        status_filter = None
        if status:
            try:
                status_filter = MarketStatus(status)
            except ValueError:
                status_filter = None
        markets = repo.list_markets(
            sport=sport,
            league=league,
            date=date,
            team=team,
            player=player,
            market_type=market_type,
            provider=provider,
            search=search,
            status=status_filter,
            sort_by=sort_by,
            limit=200,
        )
        open_positions = repo.list_positions(
            account_id=account_id,
            status=PositionStatus.OPEN,
            limit=1000,
        )
        open_market_ids = {str(item["market_id"]) for item in open_positions}

        cards = []
        for market in markets:
            detail = repo.get_market_detail(str(market["id"])) or {
                "outcomes": [],
                "quotes": [],
                "positions": [],
            }
            quotes = detail["quotes"]
            outcomes = detail["outcomes"]
            probability_history = detail.get("probability_snapshots") or []
            history_by_outcome: dict[str, list[dict[str, object]]] = {}
            for item in probability_history:
                history_by_outcome.setdefault(str(item["outcome_id"]), []).append(item)
            latest_by_outcome: dict[str, dict[str, object]] = {}
            movement: dict[str, list[float]] = {}
            for quote in quotes:
                key = str(quote["outcome_id"])
                movement.setdefault(key, []).append(float(quote["implied_probability"]))
                if key not in latest_by_outcome:
                    latest_by_outcome[key] = quote

            outcome_cards = []
            for outcome in outcomes:
                quote = latest_by_outcome.get(str(outcome["id"]))
                latest_snapshot = None
                if history_by_outcome.get(str(outcome["id"])):
                    latest_snapshot = history_by_outcome[str(outcome["id"])][0]

                market_prob = (
                    float(latest_snapshot["sportsbook_consensus_probability"])
                    if latest_snapshot is not None
                    else (
                        float(quote["implied_probability"])
                        if quote is not None
                        else None
                    )
                )
                sip_prob = (
                    float(latest_snapshot["sip_adjusted_probability"])
                    if latest_snapshot is not None
                    else None
                )
                edge = (
                    float(latest_snapshot["edge"])
                    if latest_snapshot is not None
                    else None
                )
                expected_value = (
                    float(latest_snapshot["expected_value"])
                    if latest_snapshot is not None
                    else None
                )
                outcome_cards.append(
                    {
                        "id": outcome["id"],
                        "name": outcome["name"],
                        "selection_key": outcome["selection_key"],
                        "player_name": outcome.get("player_name"),
                        "statistic": outcome.get("statistic"),
                        "threshold": outcome.get("threshold"),
                        "direction": outcome.get("direction"),
                        "american_odds": quote["american_odds"] if quote else None,
                        "decimal_odds": quote["decimal_odds"] if quote else None,
                        "raw_implied_probability": (
                            float(latest_snapshot["raw_implied_probability"])
                            if latest_snapshot is not None
                            else market_prob
                        ),
                        "no_vig_probability": (
                            float(latest_snapshot["no_vig_probability"])
                            if latest_snapshot is not None
                            else market_prob
                        ),
                        "sportsbook_consensus_probability": market_prob,
                        "market_probability": market_prob,
                        "sip_probability": sip_prob,
                        "edge": edge,
                        "expected_value": expected_value,
                        "quote_timestamp": (
                            latest_snapshot["quote_timestamp"]
                            if latest_snapshot is not None
                            else (quote["observed_at"] if quote else None)
                        ),
                        "forecast_timestamp": (
                            latest_snapshot["forecast_timestamp"]
                            if latest_snapshot is not None
                            else None
                        ),
                        "model_version": (
                            latest_snapshot["model_version"]
                            if latest_snapshot is not None
                            else None
                        ),
                        "data_quality_status": (
                            latest_snapshot["data_quality_status"]
                            if latest_snapshot is not None
                            else (quote.get("quote_status") if quote else "unavailable")
                        ),
                        "best_odds": quote["american_odds"] if quote else None,
                        "movement": movement.get(str(outcome["id"]), [])[:8],
                    }
                )

            cards.append(
                {
                    **market,
                    "outcomes": outcome_cards,
                    "event_id": f"event:{market['canonical_event_id']}",
                    "top_edge": max(
                        [item.get("edge") or 0 for item in outcome_cards],
                        default=0,
                    ),
                    "top_expected_value": max(
                        [item.get("expected_value") or 0 for item in outcome_cards],
                        default=0,
                    ),
                    "qualified_label": (
                        "Qualified"
                        if any(
                            item.get("edge") is not None and item["edge"] > 0
                            for item in outcome_cards
                        )
                        else "No Bet"
                    ),
                    "has_open_position": str(market["id"]) in open_market_ids,
                }
            )

        return {
            "markets": cards,
            "has_persisted_data": bool(cards),
            "unavailable_state": None
            if cards
            else "No persisted market records are available yet.",
        }

    @app.get("/sip/markets")
    def sip_markets_page():
        return render_template("sip_markets.html")

    @app.get("/sip/events/<event_id>")
    def sip_event_detail_page(event_id: str):
        return render_template("sip_event_detail.html", event_id=event_id)

    @app.get("/sip/props")
    def sip_prop_browser_page():
        return render_template("sip_prop_browser.html")

    @app.get("/sip/parlay-builder")
    def sip_parlay_builder_page():
        return render_template("sip_parlay_builder.html")

    @app.get("/sip/markets/<market_id>")
    def sip_market_detail_page(market_id: str):
        return render_template("sip_market_detail.html", market_id=market_id)

    @app.get("/sip/portfolio")
    def sip_portfolio_page():
        return render_template("sip_portfolio.html")

    @app.get("/sip/activity")
    def sip_activity_page():
        return render_template("sip_activity.html")

    @app.get("/sip/open-positions")
    def sip_open_positions_page():
        return render_template("sip_open_positions.html")

    @app.get("/sip/settled-positions")
    def sip_settled_positions_page():
        return render_template("sip_settled_positions.html")

    @app.get("/sip/exposure")
    def sip_exposure_dashboard_page():
        return render_template("sip_exposure_dashboard.html")

    @app.get("/sip/model-history")
    def sip_model_history_page():
        return render_template("sip_model_history.html")

    @app.get("/api/sip/markets")
    def sip_markets_feed():
        account_id = request.args.get("account_id", _default_account_id())
        sport = request.args.get("sport") or None
        league = request.args.get("league") or None
        date = request.args.get("date") or None
        team = request.args.get("team") or None
        player = request.args.get("player") or None
        market_type = request.args.get("market_type") or None
        provider = request.args.get("provider") or None
        search = request.args.get("search") or None
        status = request.args.get("status") or None
        sort_by = request.args.get("sort_by") or "start_time"
        _ensure_default_account(account_id)
        payload = _build_market_feed(
            sport=sport,
            league=league,
            date=date,
            team=team,
            player=player,
            market_type=market_type,
            provider=provider,
            search=search,
            status=status,
            sort_by=sort_by,
            account_id=account_id,
        )
        payload["filters"] = {
            "sport": sport,
            "league": league,
            "date": date,
            "team": team,
            "player": player,
            "market_type": market_type,
            "provider": provider,
            "search": search,
            "status": status,
            "sort_by": sort_by,
            "account_id": account_id,
        }
        return payload

    @app.get("/api/sip/events")
    def sip_events_feed():
        repo = _personal_service().repository
        _seed_markets_from_persisted_events()
        return {
            "events": repo.list_event_catalog(
                sport=request.args.get("sport") or None,
                league=request.args.get("league") or None,
                date=request.args.get("date") or None,
                team=request.args.get("team") or None,
                status=request.args.get("status") or None,
                search=request.args.get("search") or None,
                limit=int(request.args.get("limit") or "300"),
            )
        }

    @app.get("/api/sip/events/<event_id>")
    def sip_event_detail(event_id: str):
        repo = _personal_service().repository
        _seed_markets_from_persisted_events()
        event_rows = repo.list_event_catalog(limit=1000)
        event = next((item for item in event_rows if item["id"] == event_id), None)
        if event is None:
            return {"error": "Event not found.", "event_id": event_id}, 404
        markets = repo.list_markets(
            league=event.get("league"),
            date=str(event.get("starts_at", ""))[:10],
            team=event.get("home_team"),
            limit=200,
        )
        return {
            "event": event,
            "markets": [
                item
                for item in markets
                if item.get("canonical_event_id")
                == event.get("id", "").replace("event:", "")
                or item.get("title", "").find(event.get("home_team", "")) >= 0
            ],
        }

    @app.get("/api/sip/props")
    def sip_props_feed():
        repo = _personal_service().repository
        _seed_markets_from_persisted_events()
        markets = repo.list_markets(
            league=request.args.get("league") or None,
            date=request.args.get("date") or None,
            team=request.args.get("team") or None,
            player=request.args.get("player") or None,
            provider=request.args.get("provider") or None,
            market_type=request.args.get("market_type") or None,
            status=(
                MarketStatus(request.args.get("status"))
                if request.args.get("status") in {item.value for item in MarketStatus}
                else None
            ),
            search=request.args.get("search") or None,
            sort_by=request.args.get("sort_by") or "expected_value",
            limit=300,
        )
        prop_markets = [
            item
            for item in markets
            if item.get("market_type") in {"player_prop", "team_prop"}
        ]
        return {
            "markets": prop_markets,
            "count": len(prop_markets),
        }

    @app.get("/api/sip/markets/<market_id>")
    def sip_market_detail(market_id: str):
        repo = _personal_service().repository
        _seed_markets_from_persisted_events()
        detail = repo.get_market_detail(market_id)
        if detail is None:
            return {
                "error": "Market not found.",
                "market_id": market_id,
            }, 404
        activity = [
            item
            for item in repo.list_activity_feed(
                account_id=request.args.get("account_id", _default_account_id()),
                limit=300,
            )
            if item.get("payload", {}).get("market_id") == market_id
        ]
        related = [
            item
            for item in repo.list_markets(limit=25)
            if item["canonical_event_id"] == detail["market"]["canonical_event_id"]
            and item["id"] != market_id
        ]
        return {
            **detail,
            "related_markets": related,
            "recent_activity": activity[:25],
            "supporting_factors": ["Model confidence", "Recent form", "Injury impact"],
            "contradicting_factors": [
                "Steam move",
                "Back-to-back fatigue",
                "Lineup uncertainty",
            ],
            "context": {
                "injuries": "Unavailable",
                "rest": "Unavailable",
                "form": "Unavailable",
                "probable_starters": "Unavailable",
            },
        }

    @app.get("/api/sip/markets/<market_id>/probability-history")
    def sip_market_probability_history(market_id: str):
        repo = _personal_service().repository
        _seed_markets_from_persisted_events()
        return {
            "market_id": market_id,
            "history": repo.list_probability_history(
                market_id=market_id,
                outcome_id=request.args.get("outcome_id") or None,
                limit=int(request.args.get("limit") or "300"),
            ),
        }

    @app.post("/api/sip/order-intents")
    def sip_record_order_intent():
        payload = request.get_json(silent=True) or {}
        repo = _personal_service().repository
        _seed_markets_from_persisted_events()

        account_id = str(payload.get("account_id") or _default_account_id())
        market_id = str(payload.get("market_id") or "").strip()
        outcome_id = str(payload.get("outcome_id") or "").strip()
        mode_value = str(payload.get("mode") or "practice").strip()
        requested_odds = int(payload.get("requested_odds") or 0)
        accepted_odds = int(payload.get("accepted_odds") or requested_odds or 0)
        stake = money(payload.get("stake") or "0")
        account = _ensure_default_account(account_id)

        detail = repo.get_market_detail(market_id)
        market_status = detail["market"]["status"] if detail is not None else "closed"
        validation_engine = OrderIntentValidationEngine()
        validation = validation_engine.validate(
            outcome_id=outcome_id,
            stake=stake,
            requested_odds=requested_odds,
            accepted_odds=accepted_odds,
            mode=mode_value,
            market_status=str(market_status),
            available_bankroll=account.available_usd,
        )

        try:
            mode = PositionMode(mode_value)
        except ValueError:
            mode = PositionMode.PRACTICE

        intent_id = f"intent-{uuid4().hex[:12]}"
        estimated_profit = potential_profit_for_american_odds(stake, accepted_odds)
        intent = OrderIntent(
            id=intent_id,
            account_id=account_id,
            market_id=market_id,
            outcome_id=outcome_id,
            stake=stake,
            requested_odds=requested_odds,
            accepted_odds=accepted_odds,
            mode=mode,
            estimated_payout=(stake + estimated_profit).quantize(Decimal("0.01")),
            exposure_impact={
                "market": stake,
                "event": stake,
                "league": stake,
            },
            validation_result=validation,
            created_at=now_iso(),
        )
        repo.save_order_intent(intent)

        if not validation.is_valid:
            return {
                "status": "rejected",
                "order_intent_id": intent_id,
                "validation": {
                    "is_valid": False,
                    "issues": [
                        {"code": issue.code.value, "detail": issue.detail}
                        for issue in validation.issues
                    ],
                },
                "execution": "disabled",
                "message": "Order intent recorded but not committed to ledger.",
            }, 400

        bet_id = f"bet-{uuid4().hex[:12]}"
        position_id = f"pos-{uuid4().hex[:12]}"
        bet = Bet(
            id=bet_id,
            account_id=account_id,
            market_id=market_id,
            outcome_id=outcome_id,
            mode=mode,
            stake=stake,
            accepted_odds=accepted_odds,
            status=BetStatus.RECORDED,
            created_at=now_iso(),
            idempotency_key=f"bet:{account_id}:{intent_id}",
        )
        repo.save_bet(bet)

        position = Position(
            id=position_id,
            account_id=account_id,
            bet_id=bet_id,
            market_id=market_id,
            outcome_id=outcome_id,
            mode=mode,
            stake=stake,
            average_accepted_odds=accepted_odds,
            entry_model_probability=None,
            current_model_probability=None,
            entry_market_probability=implied_probability_from_american(accepted_odds),
            current_market_probability=implied_probability_from_american(accepted_odds),
            potential_profit=estimated_profit,
            potential_return=(stake + estimated_profit).quantize(Decimal("0.01")),
            estimated_current_value=None,
            realized_profit_loss=money("0"),
            status=PositionStatus.OPEN,
            opened_at=now_iso(),
            settled_at=None,
        )
        repo.save_position(position)

        entry_probability = implied_probability_from_american(accepted_odds)
        synthetic_shares = compute_synthetic_shares(stake, entry_probability)
        market_value = estimate_synthetic_value(synthetic_shares, entry_probability)
        sip_probability = (
            entry_probability + Decimal("0.02")
            if entry_probability < Decimal("0.98")
            else Decimal("0.98")
        )
        sip_value = estimate_synthetic_value(synthetic_shares, sip_probability)
        repo.save_synthetic_position_valuation(
            SyntheticPositionValuation(
                id=f"valuation-{uuid4().hex[:12]}",
                position_id=position_id,
                entry_probability=entry_probability,
                synthetic_shares=synthetic_shares,
                current_market_probability=entry_probability,
                current_sip_probability=sip_probability,
                estimated_market_value=market_value,
                estimated_sip_value=sip_value,
                estimated_change_since_entry_market=(market_value - stake).quantize(
                    Decimal("0.01")
                ),
                estimated_change_since_entry_sip=(sip_value - stake).quantize(
                    Decimal("0.01")
                ),
                valuation_timestamp=now_iso(),
            )
        )

        repo.save_ledger_transaction(
            LedgerTransaction(
                id=f"tx-{uuid4().hex[:12]}",
                account_id=account_id,
                position_id=position_id,
                transaction_type=LedgerTransactionType.STAKE_RESERVATION,
                amount=(stake * Decimal("-1")).quantize(Decimal("0.01")),
                balance_after=account.balance_usd,
                reserved_after=(account.held_usd + stake).quantize(Decimal("0.01")),
                available_after=(account.available_usd - stake).quantize(
                    Decimal("0.01")
                ),
                note=(
                    "Record Practice Position"
                    if mode is PositionMode.PRACTICE
                    else "Record Manual Position"
                ),
                created_at=now_iso(),
                reference_id=intent_id,
            )
        )

        return {
            "status": "recorded",
            "order_intent_id": intent_id,
            "bet_id": bet_id,
            "position_id": position_id,
            "execution": "disabled",
            "actions": {
                "primary": (
                    "Record Practice Position"
                    if mode is PositionMode.PRACTICE
                    else "Record Manual Position"
                )
            },
        }

    def _execution_order_from_payload(payload: dict[str, object]) -> ExecutionOrder:
        failure_raw = payload.get("failure_code")
        return ExecutionOrder(
            id=str(payload["id"]),
            order_intent_id=str(payload["order_intent_id"]),
            account_id=str(payload["account_id"]),
            market_id=str(payload["market_id"]),
            outcome_id=str(payload["outcome_id"]),
            stake=Decimal(str(payload["stake"])),
            requested_odds=int(payload["requested_odds"]),
            accepted_odds=int(payload["accepted_odds"]),
            sportsbook=str(payload["sportsbook"]),
            mode=ExecutionMode(str(payload["mode"])),
            state=OrderState(str(payload["state"])),
            requires_confirmation=bool(payload["requires_confirmation"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            provider_reference=(
                str(payload["provider_reference"])
                if payload.get("provider_reference")
                else None
            ),
            prefilled_url=(
                str(payload["prefilled_url"]) if payload.get("prefilled_url") else None
            ),
            failure_code=(FailureCode(str(failure_raw)) if failure_raw else None),
            failure_message=(
                str(payload["failure_message"])
                if payload.get("failure_message")
                else None
            ),
            metadata=dict(payload.get("metadata") or {}),
        )

    @app.post("/api/sip/execution/orders")
    def sip_create_execution_order():
        payload = request.get_json(silent=True) or {}
        repo = _personal_service().repository
        orchestrator = _execution_service()

        order_intent_id = str(
            payload.get("order_intent_id") or f"intent-{uuid4().hex[:12]}"
        )
        account_id = str(payload.get("account_id") or _default_account_id())
        market_id = str(payload.get("market_id") or "").strip()
        outcome_id = str(payload.get("outcome_id") or "").strip()
        requested_odds = int(payload.get("requested_odds") or 0)
        accepted_odds = int(payload.get("accepted_odds") or requested_odds or 0)
        stake = money(payload.get("stake") or "0")
        sportsbook = (
            str(payload.get("sportsbook") or "draftkings").strip() or "draftkings"
        )
        mode_text = str(payload.get("execution_mode") or "practice").strip()
        auto_submit = bool(payload.get("auto_submit", True))

        try:
            execution_mode = ExecutionMode(mode_text)
        except ValueError:
            return {
                "error": "Unsupported execution_mode.",
                "supported": [item.value for item in ExecutionMode],
            }, 400

        _ensure_default_account(account_id)

        if not market_id or not outcome_id:
            return {
                "error": "market_id and outcome_id are required for execution order creation.",
            }, 400

        existing_intent = repo.get_order_intent(order_intent_id)
        if existing_intent is None:
            intent_mode = (
                PositionMode.PRACTICE
                if execution_mode == ExecutionMode.PRACTICE
                else PositionMode.RECORDED_REAL
            )
            bootstrap_intent = OrderIntent(
                id=order_intent_id,
                account_id=account_id,
                market_id=market_id,
                outcome_id=outcome_id,
                stake=stake,
                requested_odds=requested_odds,
                accepted_odds=accepted_odds,
                mode=intent_mode,
                estimated_payout=(
                    stake + potential_profit_for_american_odds(stake, accepted_odds)
                ).quantize(Decimal("0.01")),
                exposure_impact={"market": stake, "event": stake, "league": stake},
                validation_result=OrderValidationResult(is_valid=True, issues=()),
                created_at=now_iso(),
            )
            repo.save_order_intent_if_absent(bootstrap_intent)

        order, transitions, receipts = orchestrator.new_order(
            order_intent_id=order_intent_id,
            account_id=account_id,
            market_id=market_id,
            outcome_id=outcome_id,
            stake=stake,
            requested_odds=requested_odds,
            accepted_odds=accepted_odds,
            sportsbook=sportsbook,
            mode=execution_mode,
            metadata={"source": "api"},
        )

        repo.save_execution_order(order)
        for transition in transitions:
            repo.append_execution_transition(transition)
        for receipt in receipts:
            repo.save_execution_receipt(receipt)

        if auto_submit and order.state == OrderState.READY_TO_SUBMIT:
            order, submit_transitions, submit_receipts = orchestrator.submit_order(
                order
            )
            repo.save_execution_order(order)
            for transition in submit_transitions:
                repo.append_execution_transition(transition)
            for receipt in submit_receipts:
                repo.save_execution_receipt(receipt)

        return {
            "order": repo.get_execution_order(order.id),
            "transitions": repo.list_execution_transitions(order.id),
            "receipts": repo.list_execution_receipts(order.id),
            "automatic_execution_enabled": (
                os.getenv("SIP_TRANSACTIONAL_SPORTSBOOK_API_AUTHORIZED", "false")
                .strip()
                .lower()
                in {"1", "true", "yes"}
            ),
        }

    @app.post("/api/sip/execution/orders/<order_id>/confirm")
    def sip_confirm_execution_order(order_id: str):
        payload = request.get_json(silent=True) or {}
        repo = _personal_service().repository
        orchestrator = _execution_service()
        saved = repo.get_execution_order(order_id)
        if saved is None:
            return {"error": "Execution order not found."}, 404

        try:
            order = _execution_order_from_payload(saved)
            order, transition, receipt = orchestrator.confirm_order(
                order,
                actor_id=str(payload.get("actor_id") or "user"),
                note=str(payload.get("note") or ""),
            )
        except StateMachineError as error:
            return {"error": str(error)}, 409

        repo.save_execution_order(order)
        repo.append_execution_transition(transition)
        repo.save_execution_receipt(receipt)

        auto_submit = bool(payload.get("auto_submit", True))
        if auto_submit and order.state == OrderState.READY_TO_SUBMIT:
            order, submit_transitions, submit_receipts = orchestrator.submit_order(
                order
            )
            repo.save_execution_order(order)
            for item in submit_transitions:
                repo.append_execution_transition(item)
            for item in submit_receipts:
                repo.save_execution_receipt(item)

        return {
            "order": repo.get_execution_order(order.id),
            "transitions": repo.list_execution_transitions(order.id),
            "receipts": repo.list_execution_receipts(order.id),
        }

    @app.post("/api/sip/execution/orders/<order_id>/external-confirmation")
    def sip_external_execution_confirmation(order_id: str):
        payload = request.get_json(silent=True) or {}
        repo = _personal_service().repository
        orchestrator = _execution_service()
        saved = repo.get_execution_order(order_id)
        if saved is None:
            return {"error": "Execution order not found."}, 404

        try:
            order = _execution_order_from_payload(saved)
            order, transition, receipt = orchestrator.finalize_external_confirmation(
                order,
                accepted=bool(payload.get("accepted", False)),
                external_reference=(
                    str(payload.get("external_reference"))
                    if payload.get("external_reference")
                    else None
                ),
                note=str(payload.get("note") or ""),
            )
        except StateMachineError as error:
            return {"error": str(error)}, 409

        repo.save_execution_order(order)
        repo.append_execution_transition(transition)
        repo.save_execution_receipt(receipt)
        return {
            "order": repo.get_execution_order(order.id),
            "transitions": repo.list_execution_transitions(order.id),
            "receipts": repo.list_execution_receipts(order.id),
        }

    @app.get("/api/sip/execution/orders")
    def sip_list_execution_orders():
        repo = _personal_service().repository
        account_id = request.args.get("account_id", _default_account_id())
        _ensure_default_account(account_id)
        state = request.args.get("state")
        state_filter = None
        if state in {item.value for item in OrderState}:
            state_filter = OrderState(state)
        return {
            "orders": repo.list_execution_orders(
                account_id=account_id,
                state=state_filter,
                limit=int(request.args.get("limit") or "250"),
            )
        }

    @app.get("/api/sip/execution/orders/<order_id>")
    def sip_get_execution_order(order_id: str):
        repo = _personal_service().repository
        order = repo.get_execution_order(order_id)
        if order is None:
            return {"error": "Execution order not found."}, 404
        return {
            "order": order,
            "transitions": repo.list_execution_transitions(order_id),
            "receipts": repo.list_execution_receipts(order_id),
        }

    @app.get("/api/sip/portfolio")
    def sip_portfolio():
        repo = _personal_service().repository
        account_id = request.args.get("account_id", _default_account_id())
        _ensure_default_account(account_id)
        summary = repo.portfolio_summary(account_id)
        exposure_snapshot = ExposureSnapshot(
            id=f"exposure-{uuid4().hex[:12]}",
            account_id=account_id,
            as_of=now_iso(),
            exposure_by_league={
                key: Decimal(str(value))
                for key, value in summary["exposure"]["league"].items()
            },
            exposure_by_team={
                key: Decimal(str(value))
                for key, value in summary["exposure"]["team"].items()
            },
            exposure_by_player={
                key: Decimal(str(value))
                for key, value in summary["exposure"].get("player", {}).items()
            },
            exposure_by_event={
                key: Decimal(str(value))
                for key, value in summary["exposure"]["event"].items()
            },
            exposure_by_market={
                key: Decimal(str(value))
                for key, value in summary["exposure"]["market"].items()
            },
            exposure_by_market_type={
                key: Decimal(str(value))
                for key, value in summary["exposure"].get("market_type", {}).items()
            },
            exposure_by_sportsbook={
                key: Decimal(str(value))
                for key, value in summary["exposure"].get("sportsbook", {}).items()
            },
            exposure_by_outcome={
                key: Decimal(str(value))
                for key, value in summary["exposure"].get("outcome", {}).items()
            },
            exposure_by_date={
                key: Decimal(str(value))
                for key, value in summary["exposure"].get("date", {}).items()
            },
            exposure_by_settlement_horizon={
                key: Decimal(str(value))
                for key, value in summary["exposure"]
                .get("settlement_horizon", {})
                .items()
            },
        )
        repo.save_exposure_snapshot(exposure_snapshot)
        return {
            **summary,
            "open_positions_list": repo.list_positions(
                account_id=account_id,
                status=PositionStatus.OPEN,
                limit=200,
            ),
            "settled_positions_list": repo.list_positions(
                account_id=account_id,
                status=PositionStatus.SETTLED,
                limit=200,
            ),
            "synthetic_valuations": repo.latest_synthetic_valuations(
                account_id=account_id,
                limit=200,
            ),
        }

    @app.get("/api/sip/open-positions")
    def sip_open_positions():
        repo = _personal_service().repository
        account_id = request.args.get("account_id", _default_account_id())
        _ensure_default_account(account_id)
        return {
            "positions": repo.list_positions(
                account_id=account_id,
                status=PositionStatus.OPEN,
                limit=500,
            )
        }

    @app.get("/api/sip/settled-positions")
    def sip_settled_positions():
        repo = _personal_service().repository
        account_id = request.args.get("account_id", _default_account_id())
        _ensure_default_account(account_id)
        return {
            "positions": repo.list_positions(
                account_id=account_id,
                status=PositionStatus.SETTLED,
                limit=500,
            )
        }

    @app.get("/api/sip/exposure")
    def sip_exposure_dashboard():
        repo = _personal_service().repository
        account_id = request.args.get("account_id", _default_account_id())
        _ensure_default_account(account_id)
        summary = repo.portfolio_summary(account_id)
        return {
            "account_id": account_id,
            "exposure": summary["exposure"],
            "open_positions": summary["open_positions"],
            "open_stake": summary["open_stake"],
        }

    @app.get("/api/sip/activity")
    def sip_activity():
        repo = _personal_service().repository
        account_id = request.args.get("account_id", _default_account_id())
        _ensure_default_account(account_id)
        return {
            "account_id": account_id,
            "events": repo.list_activity_feed(account_id=account_id, limit=500),
            "domain_events": repo.list_domain_events(limit=500),
        }

    @app.get("/api/sip/domain-events")
    def sip_domain_events():
        repo = _personal_service().repository
        return {
            "events": repo.list_domain_events(
                aggregate_type=request.args.get("aggregate_type") or None,
                aggregate_id=request.args.get("aggregate_id") or None,
                event_type=request.args.get("event_type") or None,
                correlation_id=request.args.get("correlation_id") or None,
                limit=int(request.args.get("limit") or "500"),
            )
        }

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


def _run_main_application() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    personal = PersonalEditionService.from_environment()
    if _controller_routes_enabled():
        validate_bind_address(
            personal.settings.host,
            tailscale_address=os.getenv(
                "RAGHUB_CONTROLLER_TAILSCALE_ADDRESS"
            ),
        )
    app.run(
        host=personal.settings.host,
        port=personal.settings.port,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    _run_main_application()
