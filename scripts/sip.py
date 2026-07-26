from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from sports.personal.config import PersonalEditionSettings
from sports.personal.model import (
    BaselineMoneylineModel,
    InsufficientCalibrationData,
)
from sports.personal.repository import PersonalEditionRepository
from sports.personal.service import PersonalEditionService
from sports.personal.team_history import (
    PregameTeamFeatureBuilder,
    PublicTeamHistorySource,
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        values = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in (
            "correlation_id",
            "canonical_event_id",
            "sport",
            "events_received",
            "quotes_accepted",
            "attempt",
            "error_type",
            "model_version",
        ):
            if hasattr(record, name):
                values[name] = getattr(record, name)
        return json.dumps(values, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def service() -> PersonalEditionService:
    load_dotenv(override=True)
    return PersonalEditionService.from_environment()


def migrate() -> int:
    database_path = os.getenv("SIP_PERSONAL_DATABASE", "data/sip_personal.db")
    applied = PersonalEditionRepository(database_path).migrate()
    print(json.dumps({"status": "ok", "applied_migrations": applied}))
    return 0


def refresh() -> int:
    result = service().scheduler.run_pending(force=True)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] == "succeeded" else 1


def prepare_history(leagues: list[str]) -> int:
    source = PublicTeamHistorySource()
    current_year = datetime.now(timezone.utc).year
    summary = {}
    for league in leagues:
        games = source.fetch(
            league,
            tuple(range(current_year - 3, current_year + 1)),
        )
        path = Path("data/training") / f"{league.lower()}_team_games.json"
        source.save(games, path)
        summary[league] = {
            "games": len(games),
            "first": games[0].start_time if games else None,
            "last": games[-1].start_time if games else None,
            "path": str(path),
        }
    print(json.dumps(summary, indent=2))
    return 0 if all(item["games"] >= 50 for item in summary.values()) else 2


def train_model(dataset: str | None, leagues: list[str]) -> int:
    application = service()
    if dataset:
        targets = [(None, Path(dataset))]
    else:
        targets = [
            (
                league,
                application.history_path_for_league(league),
            )
            for league in leagues
        ]
    results = {}
    try:
        for league, path in targets:
            if league is None:
                model = BaselineMoneylineModel.train_csv(path)
                output = Path(application.settings.model_path)
                label = "legacy"
            else:
                games = PublicTeamHistorySource.load(path)
                rows = PregameTeamFeatureBuilder.training_rows(games)
                model = BaselineMoneylineModel.train(rows)
                output = application.model_path_for_league(league)
                label = league
            model.save(output)
            results[label] = {**asdict(model.metadata), "path": str(output)}
    except FileNotFoundError as error:
        print(
            f"MODEL_NOT_TRAINED: training dataset was not found: {error.filename}",
            file=sys.stderr,
        )
        return 2
    except InsufficientCalibrationData as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(results, indent=2))
    return 0


def evaluate() -> int:
    snapshot = service().snapshot()
    print(
        json.dumps(
            {
                "mode": snapshot["mode"],
                "feed": snapshot["feed"],
                "model": snapshot["model"],
                "events": len(snapshot["events"]),
                "qualified_choices": len(snapshot["qualified_choices"]),
                "no_bet_results": len(snapshot["no_bet_results"]),
                "thresholds": snapshot["thresholds"],
            },
            indent=2,
        )
    )
    return 0


def run_server() -> int:
    from app import create_app
    from wsgiref.simple_server import WSGIRequestHandler, make_server

    application = service()
    settings = application.settings
    flask_app = create_app(personal_service=application)

    class StructuredRequestHandler(WSGIRequestHandler):
        def log_message(self, format, *args):
            logging.getLogger("sip.http").info(format, *args)

    server = make_server(
        settings.host,
        settings.port,
        flask_app,
        handler_class=StructuredRequestHandler,
    )
    logging.getLogger("sip.http").info(
        "server_started",
        extra={"host": settings.host, "port": settings.port},
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.getLogger("sip.http").info("server_stopping")
    finally:
        server.server_close()
    return 0


def run_scheduler() -> int:
    application = service()
    stop = False

    def handle_stop(_signal, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    while not stop:
        result = application.scheduler.run_pending()
        if result["status"] != "not_due":
            print(json.dumps(result, default=str))
        time.sleep(min(30, application.settings.refresh_interval_seconds))
    return 0


def main() -> int:
    load_dotenv(override=True)
    configure_logging()
    parser = argparse.ArgumentParser(description="SIP Personal Edition operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("refresh")
    history = commands.add_parser("prepare-history")
    history.add_argument(
        "--league",
        choices=("all", "wnba", "mlb"),
        default="all",
    )
    training = commands.add_parser("train-model")
    training.add_argument(
        "--dataset",
        default=None,
    )
    training.add_argument(
        "--league",
        choices=("all", "wnba", "mlb"),
        default="all",
    )
    commands.add_parser("evaluate")
    commands.add_parser("run")
    commands.add_parser("scheduler")
    arguments = parser.parse_args()
    actions = {
        "migrate": migrate,
        "refresh": refresh,
        "evaluate": evaluate,
        "run": run_server,
        "scheduler": run_scheduler,
    }
    if arguments.command == "train-model":
        leagues = (
            ["WNBA", "MLB"] if arguments.league == "all" else [arguments.league.upper()]
        )
        return train_model(arguments.dataset, leagues)
    if arguments.command == "prepare-history":
        leagues = (
            ["WNBA", "MLB"] if arguments.league == "all" else [arguments.league.upper()]
        )
        return prepare_history(leagues)
    return actions[arguments.command]()


if __name__ == "__main__":
    raise SystemExit(main())
