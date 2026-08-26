"""Isolated durable HTTP test service for Integrated Demonstrator v0.1.

This service is deliberately tiny:

GET  /state
POST /deploy-v2

The deployment endpoint records every attempt and durably transitions the
service from version 1 to version 2 at most once.

For deterministic failure injection, callers may send:

    X-MissionaryX-Drop-Response: 1

The service commits the deployment and then closes the transport before
returning an HTTP response. This simulates loss of completion confirmation
after the external effect has already landed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class TestServiceState:
    active_version: int
    deployment_attempt_count: int
    successful_transition_count: int


class DemoServiceStore:
    """Durable singleton state for the isolated external test service."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS service_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    active_version INTEGER NOT NULL
                        CHECK (active_version IN (1, 2)),
                    deployment_attempt_count INTEGER NOT NULL
                        CHECK (deployment_attempt_count >= 0),
                    successful_transition_count INTEGER NOT NULL
                        CHECK (successful_transition_count >= 0)
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO service_state (
                    singleton,
                    active_version,
                    deployment_attempt_count,
                    successful_transition_count
                ) VALUES (1, 1, 0, 0)
                """
            )

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> TestServiceState:
        return TestServiceState(
            active_version=int(row["active_version"]),
            deployment_attempt_count=int(row["deployment_attempt_count"]),
            successful_transition_count=int(row["successful_transition_count"]),
        )

    def read_state(self) -> TestServiceState:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    active_version,
                    deployment_attempt_count,
                    successful_transition_count
                FROM service_state
                WHERE singleton = 1
                """
            ).fetchone()

        if row is None:
            raise RuntimeError("test-service state row is missing")

        return self._row_to_state(row)

    def deploy_v2(self) -> tuple[TestServiceState, bool]:
        """Attempt the only permitted external mutation.

        Returns:
            (state, transitioned)

        Every call increments deployment_attempt_count.

        successful_transition_count increments only when active_version
        actually changes from 1 to 2.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")

            row = connection.execute(
                """
                SELECT
                    active_version,
                    deployment_attempt_count,
                    successful_transition_count
                FROM service_state
                WHERE singleton = 1
                """
            ).fetchone()

            if row is None:
                raise RuntimeError("test-service state row is missing")

            active_version = int(row["active_version"])
            attempts = int(row["deployment_attempt_count"]) + 1
            transitions = int(row["successful_transition_count"])

            if active_version == 1:
                active_version = 2
                transitions += 1
                transitioned = True
            elif active_version == 2:
                transitioned = False
            else:
                raise RuntimeError(
                    f"unsupported active_version {active_version!r}"
                )

            connection.execute(
                """
                UPDATE service_state
                SET
                    active_version = ?,
                    deployment_attempt_count = ?,
                    successful_transition_count = ?
                WHERE singleton = 1
                """,
                (active_version, attempts, transitions),
            )
            connection.commit()

            state = TestServiceState(
                active_version=active_version,
                deployment_attempt_count=attempts,
                successful_transition_count=transitions,
            )
            return state, transitioned
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class TestServiceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: DemoServiceStore,
    ) -> None:
        self.store = store
        super().__init__(server_address, TestServiceHandler)


class TestServiceHandler(BaseHTTPRequestHandler):
    """Bounded HTTP interface over DemoServiceStore."""

    server: TestServiceHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        # Keep deterministic tests and demo output quiet.
        return

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/state":
            self._write_json(404, {"error": "not_found"})
            return

        state = self.server.store.read_state()
        self._write_json(200, asdict(state))

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/deploy-v2":
            self._write_json(404, {"error": "not_found"})
            return

        content_length = self.headers.get("Content-Length")
        if content_length not in (None, "0"):
            self._write_json(400, {"error": "request_body_not_allowed"})
            return

        state, transitioned = self.server.store.deploy_v2()

        if self.headers.get("X-MissionaryX-Drop-Response") == "1":
            # The external mutation is already committed. Intentionally destroy
            # the completion channel before MissionaryX can receive a receipt.
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return

        self._write_json(
            200 if transitioned else 409,
            {
                **asdict(state),
                "transitioned": transitioned,
            },
        )


def create_server(
    database_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> TestServiceHTTPServer:
    """Create but do not start the isolated test HTTP server."""
    if host != "127.0.0.1":
        raise ValueError("Integrated Demonstrator test service must bind to 127.0.0.1")
    if not isinstance(port, int) or isinstance(port, bool):
        raise TypeError("port must be int")
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")

    return TestServiceHTTPServer(
        (host, port),
        DemoServiceStore(database_path),
    )
