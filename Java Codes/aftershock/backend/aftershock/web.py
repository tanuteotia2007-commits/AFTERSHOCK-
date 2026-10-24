from __future__ import annotations

import json
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator
from wsgiref.simple_server import make_server
from urllib.parse import urlsplit

from dotenv import load_dotenv

from aftershock.agents.agentverse import (
    AgentverseAdapter,
    AgentverseConfigurationError,
    AgentverseRequestError,
)
from aftershock.agents.mock_agents import MockNaiveOrderAgent, MockResilientOrderAgent
from aftershock.engine.faults import Scenario, ValidationError
from aftershock.engine.fixes import suggest_fixes
from aftershock.engine.runner import RunResult, analyze, replay
from aftershock.engine.verify import verify_fix

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

AGENT_TYPES = {
    "naive": MockNaiveOrderAgent,
    "resilient": MockResilientOrderAgent,
}
AGENT_LABELS = {
    "naive": f"SIMULATED · {MockNaiveOrderAgent.label}",
    "resilient": f"SIMULATED · {MockResilientOrderAgent.label}",
    "agentverse": "REAL Agentverse agent",
}
MAX_REQUEST_BYTES = 1_000_000
DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "aftershock.sqlite3"
INDEX_FILE = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_auth_tables()
        self._migrate_agent_kinds()
        self.initialize()

    def _initialize_auth_tables(self) -> None:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()

    def _migrate_agent_kinds(self) -> None:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agents'"
            ).fetchone()
            if row is not None:
                agent_columns = {
                    column[1]
                    for column in connection.execute("PRAGMA table_info(agents)")
                }
                if "'agentverse'" not in row[0].lower():
                    owner_expression = "owner_id" if "owner_id" in agent_columns else "NULL"
                    connection.execute("BEGIN")
                    connection.execute(
                        """
                        CREATE TABLE agents_new (
                            id INTEGER PRIMARY KEY,
                            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
                            kind TEXT NOT NULL CHECK(kind IN ('naive', 'resilient', 'agentverse')),
                            label TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE
                        )
                        """
                    )
                    connection.execute(
                        "INSERT INTO agents_new (id, name, kind, label, created_at, owner_id) "
                        f"SELECT id, name, kind, label, created_at, {owner_expression} FROM agents"
                    )
                    connection.execute("DROP TABLE agents")
                    connection.execute("ALTER TABLE agents_new RENAME TO agents")
                    connection.commit()
                elif "owner_id" not in agent_columns:
                    connection.execute(
                        "ALTER TABLE agents ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE"
                    )

            run_row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
            ).fetchone()
            if run_row is not None:
                run_columns = {
                    column[1] for column in connection.execute("PRAGMA table_info(runs)")
                }
                if "owner_id" not in run_columns:
                    connection.execute(
                        "ALTER TABLE runs ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
                    kind TEXT NOT NULL CHECK(kind IN ('naive', 'resilient', 'agentverse')),
                    label TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY,
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    scenario_name TEXT NOT NULL,
                    passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
                    violations_count INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    findings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_created_at_idx
                    ON runs(created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS agents_owner_idx ON agents(owner_id, id DESC);
                CREATE INDEX IF NOT EXISTS runs_owner_idx ON runs(owner_id, created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS scenarios (
                    id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    scenario_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replays (
                    id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fix_suggestions (
                    id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    suggestions_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS verifications (
                    id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_events_owner_idx
                    ON audit_events(owner_id, created_at DESC, id DESC);
                """
            )

    def audit(
        self, owner_id: int, action: str, resource_type: str, resource_id: int
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_events "
                "(owner_id, action, resource_type, resource_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    owner_id,
                    action,
                    resource_type,
                    resource_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def create_user(self, username: str, password_hash: str) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, created_at),
            )
            user_id = cursor.lastrowid
            connection.execute(
                "UPDATE agents SET owner_id = ? WHERE owner_id IS NULL", (user_id,)
            )
            connection.execute(
                "UPDATE runs SET owner_id = ? WHERE owner_id IS NULL", (user_id,)
            )
            row = connection.execute(
                "SELECT id, username, created_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return dict(row)

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return None if row is None else dict(row)

    def create_session(self, token_hash: str, user_id: int, expires_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, expires_at, datetime.now(timezone.utc).isoformat()),
            )

    def session_user(self, token_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT u.id, u.username, s.expires_at
                FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is not None and row["expires_at"] <= datetime.now(timezone.utc).isoformat():
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
        return None if row is None else dict(row)

    def delete_session(self, token_hash: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def create_scenario(
        self, owner_id: int, name: str, scenario: dict[str, Any]
    ) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO scenarios (owner_id, name, scenario_json, created_at) VALUES (?, ?, ?, ?)",
                (owner_id, name, json.dumps(scenario, sort_keys=True), created_at),
            )
            row = connection.execute(
                "SELECT id, name, scenario_json, created_at FROM scenarios WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return {
            "id": row["id"],
            "name": row["name"],
            "scenario": json.loads(row["scenario_json"]),
            "created_at": row["created_at"],
        }

    def list_scenarios(self, owner_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, scenario_json, created_at FROM scenarios "
                "WHERE owner_id = ? ORDER BY id DESC",
                (owner_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "scenario": json.loads(row["scenario_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def save_replay(self, owner_id: int, run_id: int, replay: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO replays (owner_id, run_id, result_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    owner_id,
                    run_id,
                    json.dumps(replay, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def list_replays(self, owner_id: int, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, result_json, created_at FROM replays "
                "WHERE owner_id = ? AND run_id = ? ORDER BY id DESC",
                (owner_id, run_id),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "result": json.loads(row["result_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_suggestions(self, owner_id: int, run_id: int) -> list[dict[str, Any]] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT suggestions_json FROM fix_suggestions WHERE owner_id = ? AND run_id = ?",
                (owner_id, run_id),
            ).fetchone()
        return None if row is None else json.loads(row["suggestions_json"])

    def list_verifications(self, owner_id: int, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, result_json, created_at FROM verifications "
                "WHERE owner_id = ? AND run_id = ? ORDER BY id DESC",
                (owner_id, run_id),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "result": json.loads(row["result_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def save_suggestions(
        self, owner_id: int, run_id: int, suggestions: list[dict[str, Any]]
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO fix_suggestions (owner_id, run_id, suggestions_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_id, run_id) DO UPDATE SET
                    suggestions_json = excluded.suggestions_json,
                    created_at = excluded.created_at
                """,
                (
                    owner_id,
                    run_id,
                    json.dumps(suggestions, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def save_verification(
        self, owner_id: int, run_id: int, result: dict[str, Any]
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO verifications (owner_id, run_id, result_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    owner_id,
                    run_id,
                    json.dumps(result, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def register_agent(self, name: str, kind: str, owner_id: int) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO agents (name, kind, label, created_at, owner_id) VALUES (?, ?, ?, ?, ?)",
                (name, kind, AGENT_LABELS[kind], created_at, owner_id),
            )
            row = connection.execute(
                "SELECT id, name, kind, label, created_at FROM agents WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return dict(row)

    def list_agents(self, owner_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, kind, label, created_at FROM agents WHERE owner_id = ? ORDER BY id DESC",
                (owner_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_run(
        self,
        agent_id: int,
        owner_id: int,
        result: dict[str, Any],
        findings: list[dict[str, Any]],
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (
                    agent_id, scenario_name, passed, violations_count, fingerprint,
                    result_json, findings_json, created_at, owner_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    result["scenario"]["name"],
                    int(result["passed"]),
                    len(result["violations"]),
                    result["fingerprint"],
                    json.dumps(result, sort_keys=True),
                    json.dumps(findings, sort_keys=True),
                    created_at,
                    owner_id,
                ),
            )
            return int(cursor.lastrowid)

    def get_agent(self, agent_id: int, owner_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, name, kind, label, created_at FROM agents WHERE id = ? AND owner_id = ?",
                (agent_id, owner_id),
            ).fetchone()
        return None if row is None else dict(row)

    def _run_row(self, row: sqlite3.Row, include_evidence: bool) -> dict[str, Any]:
        result = json.loads(row["result_json"])
        findings = json.loads(row["findings_json"])
        record = {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
            "agent_kind": row["agent_kind"],
            "execution_type": result.get(
                "execution_type",
                "REAL AGENT" if row["agent_kind"] == "agentverse" else "SIMULATED (MOCK AGENT)",
            ),
            "scenario_name": row["scenario_name"],
            "passed": bool(row["passed"]),
            "violations_count": row["violations_count"],
            "finding_count": len(findings),
            "fingerprint": row["fingerprint"],
            "created_at": row["created_at"],
            "violations": result["violations"],
            "findings": findings,
        }
        if include_evidence:
            record["result"] = result
        return record

    def get_run(self, run_id: int, owner_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT r.*, a.name AS agent_name, a.kind AS agent_kind
                FROM runs r JOIN agents a ON a.id = r.agent_id
                WHERE r.id = ? AND r.owner_id = ?
                """,
                (run_id, owner_id),
            ).fetchone()
        return None if row is None else self._run_row(row, include_evidence=True)

    def list_runs(self, owner_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*, a.name AS agent_name, a.kind AS agent_kind
                FROM runs r JOIN agents a ON a.id = r.agent_id
                WHERE r.owner_id = ?
                ORDER BY r.created_at DESC, r.id DESC LIMIT ?
                """,
                (owner_id, limit),
            ).fetchall()
        return [self._run_row(row, include_evidence=False) for row in rows]


def _read_json(environ: dict[str, Any]) -> dict[str, Any]:
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid content length") from None
    if not 0 <= length <= MAX_REQUEST_BYTES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body is too large or has invalid length")
    try:
        value = json.loads(environ["wsgi.input"].read(length))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be valid JSON") from None
    if not isinstance(value, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
    return value


PASSWORD_ITERATIONS = 310_000
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_ATTEMPTS = 8
_login_attempts: dict[str, list[float]] = {}
OPERATION_WINDOW_SECONDS = 60
OPERATION_MAX_ATTEMPTS = 30
_operation_attempts: dict[str, list[float]] = {}


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


_DUMMY_PASSWORD_HASH = _hash_password("invalid-login-timing-placeholder")


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        ).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _check_login_rate_limit(environ: dict[str, Any]) -> None:
    now = time.monotonic()
    address = str(environ.get("REMOTE_ADDR", "unknown"))
    recent = [
        started
        for started in _login_attempts.get(address, [])
        if now - started < LOGIN_WINDOW_SECONDS
    ]
    if len(recent) >= LOGIN_MAX_ATTEMPTS:
        _login_attempts[address] = recent
        raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "too many authentication attempts")
    recent.append(now)
    _login_attempts[address] = recent


def _check_operation_rate_limit(environ: dict[str, Any]) -> None:
    now = time.monotonic()
    address = str(environ.get("REMOTE_ADDR", "unknown"))
    recent = [
        started
        for started in _operation_attempts.get(address, [])
        if now - started < OPERATION_WINDOW_SECONDS
    ]
    if len(recent) >= OPERATION_MAX_ATTEMPTS:
        _operation_attempts[address] = recent
        raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "too many test operations")
    recent.append(now)
    _operation_attempts[address] = recent


class AftershockApp:
    def __init__(self, database_path: str | Path):
        self.database = Database(database_path)

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        try:
            status, content_type, body = self.dispatch(environ)
        except ApiError as error:
            status = error.status
            content_type = "application/json; charset=utf-8"
            body = json.dumps({"error": str(error)}).encode("utf-8")
        phrase = HTTPStatus(status).phrase
        start_response(
            f"{status} {phrase}",
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ]
            + environ.pop("aftershock.response_headers", []),
        )
        return [body]

    def dispatch(self, environ: dict[str, Any]) -> tuple[int, str, bytes]:
        method = environ["REQUEST_METHOD"].upper()
        path = environ["PATH_INFO"]

        if method == "GET" and path == "/":
            return HTTPStatus.OK, "text/html; charset=utf-8", INDEX_FILE.read_bytes()
        if method == "GET" and path == "/api/health":
            return self._json(HTTPStatus.OK, {"status": "ok"})
        if path in {"/api/auth/register", "/api/auth/login"} and method == "POST":
            self._validate_same_origin(environ)
            return self._authenticate(environ, path.rsplit("/", 1)[-1])
        if path == "/api/auth/logout" and method == "POST":
            self._validate_same_origin(environ)
            return self._logout(environ)
        if path == "/api/auth/me" and method == "GET":
            user = self._require_user(environ)
            return self._json(HTTPStatus.OK, {"user": user})
        if path.startswith("/api/"):
            user = self._require_user(environ)
            environ["aftershock.user"] = user
            if method not in {"GET", "HEAD"}:
                self._validate_same_origin(environ)
            if method == "POST" and (
                path in {"/api/agents", "/api/runs"}
                or path.startswith("/api/runs/")
            ):
                _check_operation_rate_limit(environ)
            if method == "GET" and path == "/api/agents":
                return self._json(
                    HTTPStatus.OK,
                    {"agents": self.database.list_agents(user["id"])},
                )
            if method == "POST" and path == "/api/agents":
                return self._register_agent(environ, user["id"])
            if method == "GET" and path == "/api/runs":
                return self._json(
                    HTTPStatus.OK,
                    {"runs": self.database.list_runs(user["id"])},
                )
            if method == "POST" and path == "/api/runs":
                return self._run_scenario(environ, user["id"])
            if method == "GET" and path == "/api/dashboard":
                return self._dashboard(user["id"])
            if method == "GET" and path == "/api/scenarios":
                return self._json(
                    HTTPStatus.OK,
                    {"scenarios": self.database.list_scenarios(user["id"])},
                )
            if method == "POST" and path == "/api/scenarios":
                return self._create_scenario(environ, user["id"])
            if path.startswith("/api/runs/"):
                return self._run_action(environ, path, method, user["id"])
        if path.startswith("/api/"):
            raise ApiError(HTTPStatus.NOT_FOUND, "API route not found")
        raise ApiError(HTTPStatus.NOT_FOUND, "page not found")

    def _authenticate(
        self, environ: dict[str, Any], action: str
    ) -> tuple[int, str, bytes]:
        _check_login_rate_limit(environ)
        data = _read_json(environ)
        if set(data) != {"username", "password"}:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "authentication requires username and password"
            )
        username, password = data["username"], data["password"]
        if (
            not isinstance(username, str)
            or not 3 <= len(username.strip()) <= 80
            or not username.strip().replace("-", "").replace("_", "").isalnum()
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "username must be 3 to 80 letters, digits, underscores, or hyphens",
            )
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "password must be 12 to 256 characters"
            )
        username = username.strip()
        if action == "register":
            try:
                user = self.database.create_user(username, _hash_password(password))
            except sqlite3.IntegrityError:
                raise ApiError(HTTPStatus.CONFLICT, "username is already registered") from None
            status = HTTPStatus.CREATED
        else:
            candidate = self.database.user_by_username(username)
            stored_hash = (
                candidate["password_hash"]
                if candidate is not None
                else _DUMMY_PASSWORD_HASH
            )
            password_matches = _verify_password(password, stored_hash)
            if candidate is None or not password_matches:
                raise ApiError(HTTPStatus.UNAUTHORIZED, "invalid username or password")
            user = {key: candidate[key] for key in ("id", "username")}
            status = HTTPStatus.OK
        self._start_session(environ, user["id"])
        self.database.audit(user["id"], f"auth.{action}", "user", user["id"])
        return self._json(status, {"user": user})

    def _start_session(self, environ: dict[str, Any], user_id: int) -> None:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires = datetime.now(timezone.utc) + timedelta(
            seconds=SESSION_MAX_AGE_SECONDS
        )
        self.database.create_session(token_hash, user_id, expires.isoformat())
        cookie = (
            f"aftershock_session={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_MAX_AGE_SECONDS}"
        )
        if environ.get("wsgi.url_scheme") == "https":
            cookie += "; Secure"
        environ["aftershock.response_headers"] = [("Set-Cookie", cookie)]

    def _read_session_token(self, environ: dict[str, Any]) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(environ.get("HTTP_COOKIE", ""))
        except Exception:
            return None
        morsel = cookie.get("aftershock_session")
        return None if morsel is None else morsel.value

    def _require_user(self, environ: dict[str, Any]) -> dict[str, Any]:
        token = self._read_session_token(environ)
        if token is None:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "sign in is required")
        user = self.database.session_user(
            hashlib.sha256(token.encode("ascii")).hexdigest()
        )
        if user is None:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "session expired; sign in again")
        return {"id": user["id"], "username": user["username"]}

    def _logout(self, environ: dict[str, Any]) -> tuple[int, str, bytes]:
        user = self._require_user(environ)
        token = self._read_session_token(environ)
        if token is not None:
            self.database.delete_session(
                hashlib.sha256(token.encode("ascii")).hexdigest()
            )
        self.database.audit(user["id"], "auth.logout", "user", user["id"])
        secure = "; Secure" if environ.get("wsgi.url_scheme") == "https" else ""
        environ["aftershock.response_headers"] = [
            (
                "Set-Cookie",
                f"aftershock_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}",
            )
        ]
        return self._json(HTTPStatus.OK, {"status": "signed out"})

    @staticmethod
    def _validate_same_origin(environ: dict[str, Any]) -> None:
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON content type required")
        origin = environ.get("HTTP_ORIGIN")
        if origin and urlsplit(origin).netloc != environ.get("HTTP_HOST"):
            raise ApiError(HTTPStatus.FORBIDDEN, "cross-origin request rejected")

    def _register_agent(
        self, environ: dict[str, Any], owner_id: int
    ) -> tuple[int, str, bytes]:
        data = _read_json(environ)
        if set(data) != {"name", "kind"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "agent must contain exactly name and kind")
        name, kind = data["name"], data["kind"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ApiError(HTTPStatus.BAD_REQUEST, "name must contain 1 to 80 characters")
        if not isinstance(kind, str) or kind not in AGENT_LABELS:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                f"kind must be one of: {', '.join(AGENT_LABELS)}",
            )
        agent = self.database.register_agent(name.strip(), kind, owner_id)
        self.database.audit(owner_id, "agent.register", "agent", agent["id"])
        return self._json(HTTPStatus.CREATED, {"agent": agent})

    def _run_scenario(
        self, environ: dict[str, Any], owner_id: int
    ) -> tuple[int, str, bytes]:
        data = _read_json(environ)
        if set(data) != {"agent_id", "scenario"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "run must contain exactly agent_id and scenario")
        agent_id = data["agent_id"]
        if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "agent_id must be a positive integer")
        agent = self.database.get_agent(agent_id, owner_id)
        if agent is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "registered agent not found")
        if agent["kind"] == "agentverse":
            return self._run_agentverse(agent_id, owner_id, data["scenario"])
        try:
            scenario = Scenario.from_dict(data["scenario"])
        except ValidationError as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(error)) from None
        result, findings = analyze(AGENT_TYPES[agent["kind"]], scenario)
        result_data = result.to_dict()
        result_data["execution_type"] = "SIMULATED (MOCK AGENT)"
        run_id = self.database.save_run(
            agent_id, owner_id, result_data, [f.to_dict() for f in findings]
        )
        self.database.audit(owner_id, "run.execute", "run", run_id)
        return self._json(
            HTTPStatus.CREATED,
            {"run": self.database.get_run(run_id, owner_id)},
        )

    def _run_agentverse(
        self, agent_id: int, owner_id: int, scenario_data: Any
    ) -> tuple[int, str, bytes]:
        if not isinstance(scenario_data, dict) or set(scenario_data) - {
            "message",
            "fault",
            "delay_ms",
        }:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "Agentverse scenario accepts message, fault, and optional delay_ms",
            )
        try:
            adapter = AgentverseAdapter()
        except AgentverseConfigurationError as error:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, str(error)) from None
        try:
            result = adapter.run(
                message=scenario_data.get("message"),
                fault=scenario_data.get("fault", "none"),
                delay_ms=scenario_data.get("delay_ms", 1000),
            )
        except ValueError as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(error)) from None
        except AgentverseRequestError as error:
            raise ApiError(HTTPStatus.BAD_GATEWAY, str(error)) from None
        findings = [
            {**violation, "status": "OBSERVED", "reproducible": False}
            for violation in result["violations"]
        ]
        run_id = self.database.save_run(agent_id, owner_id, result, findings)
        self.database.audit(owner_id, "run.execute", "run", run_id)
        return self._json(
            HTTPStatus.CREATED,
            {"run": self.database.get_run(run_id, owner_id)},
        )

    def _dashboard(self, owner_id: int) -> tuple[int, str, bytes]:
        runs = self.database.list_runs(owner_id)
        simulated = [run for run in runs if run["execution_type"].startswith("SIMULATED")]
        real = [run for run in runs if run["execution_type"].startswith("REAL AGENT")]
        return self._json(
            HTTPStatus.OK,
            {
                "agents": self.database.list_agents(owner_id),
                "runs": runs,
                "metrics": {
                    "agents_registered": len(self.database.list_agents(owner_id)),
                    "runs_saved": len(runs),
                    "simulated_runs": len(simulated),
                    "real_agent_runs": len(real),
                    "simulated_pass_rate": (
                        round(100 * sum(run["passed"] for run in simulated) / len(simulated), 1)
                        if simulated
                        else None
                    ),
                    "real_agent_reply_rate": (
                        round(100 * sum(run["passed"] for run in real) / len(real), 1)
                        if real
                        else None
                    ),
                },
            },
        )

    def _create_scenario(
        self, environ: dict[str, Any], owner_id: int
    ) -> tuple[int, str, bytes]:
        data = _read_json(environ)
        if set(data) != {"scenario"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "scenario is required")
        try:
            scenario = Scenario.from_dict(data["scenario"])
        except ValidationError as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(error)) from None
        stored = self.database.create_scenario(
            owner_id, scenario.name, scenario.to_dict()
        )
        self.database.audit(owner_id, "scenario.create", "scenario", stored["id"])
        return self._json(HTTPStatus.CREATED, {"scenario": stored})

    def _run_action(
        self, environ: dict[str, Any], path: str, method: str, owner_id: int
    ) -> tuple[int, str, bytes]:
        parts = path.strip("/").split("/")
        if len(parts) not in {3, 4} or not parts[2].isdecimal():
            raise ApiError(HTTPStatus.NOT_FOUND, "run not found")
        run_id = int(parts[2])
        run = self.database.get_run(run_id, owner_id)
        if run is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "run not found")
        if len(parts) == 3 and method == "GET":
            run["replays"] = self.database.list_replays(owner_id, run_id)
            run["suggestions"] = self.database.get_suggestions(owner_id, run_id) or []
            run["verifications"] = self.database.list_verifications(owner_id, run_id)
            return self._json(HTTPStatus.OK, {"run": run})
        if len(parts) != 4 or method != "POST":
            raise ApiError(HTTPStatus.NOT_FOUND, "run action not found")
        action = parts[3]
        if action == "replay":
            return self._replay_run(run, owner_id)
        if action == "fixes":
            return self._suggest_run_fixes(run, owner_id)
        if action == "verify":
            return self._verify_run(environ, run, owner_id)
        raise ApiError(HTTPStatus.NOT_FOUND, "run action not found")

    def _replay_run(
        self, run: dict[str, Any], owner_id: int
    ) -> tuple[int, str, bytes]:
        if run["agent_kind"] == "agentverse":
            raise ApiError(
                HTTPStatus.CONFLICT,
                "live Agentverse runs cannot be replayed automatically; use an explicit new test instead",
            )
        try:
            original = run["result"]
            scenario = Scenario.from_dict(original["scenario"])
            recorded = RunResult(
                scenario=scenario,
                agent_label=original["agent"],
                events=original["events"],
                violations=original["violations"],
                outcome=original["outcome"],
                error=original["error"],
                virtual_ms=original["virtual_ms"],
                fingerprint=original["fingerprint"],
            )
            replay_result = replay(AGENT_TYPES[run["agent_kind"]], recorded)
        except (KeyError, TypeError, ValidationError) as error:
            raise ApiError(
                HTTPStatus.CONFLICT, f"recorded run lacks replayable inputs: {error}"
            ) from None
        replayed = replay_result["run"]
        replay_result["run"] = replayed.to_dict()
        replay_id = self.database.save_replay(owner_id, run["id"], replay_result)
        self.database.audit(owner_id, "run.replay", "run", run["id"])
        return self._json(
            HTTPStatus.CREATED,
            {"replay_id": replay_id, "replay": replay_result},
        )

    def _suggest_run_fixes(
        self, run: dict[str, Any], owner_id: int
    ) -> tuple[int, str, bytes]:
        suggestions = suggest_fixes(run["findings"])
        self.database.save_suggestions(owner_id, run["id"], suggestions)
        self.database.audit(owner_id, "fix.suggest", "run", run["id"])
        return self._json(
            HTTPStatus.OK,
            {
                "source": "deterministic rule table",
                "suggestions": suggestions,
                "note": "Suggestions are hypotheses and never modify agent code.",
            },
        )

    def _verify_run(
        self, environ: dict[str, Any], run: dict[str, Any], owner_id: int
    ) -> tuple[int, str, bytes]:
        if run["agent_kind"] == "agentverse":
            raise ApiError(
                HTTPStatus.CONFLICT,
                "automatic verification is available only for local simulated agents",
            )
        data = _read_json(environ)
        if set(data) != {"candidate_kind", "approved"} or data["approved"] is not True:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "explicit approval and candidate_kind are required to run verification",
            )
        candidate_kind = data["candidate_kind"]
        if not isinstance(candidate_kind, str) or candidate_kind not in AGENT_TYPES:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "candidate_kind must be naive or resilient"
            )
        try:
            scenario = Scenario.from_dict(run["result"]["scenario"])
        except (KeyError, TypeError, ValidationError) as error:
            raise ApiError(
                HTTPStatus.CONFLICT, f"recorded run cannot be verified: {error}"
            ) from None
        verification = verify_fix(
            AGENT_TYPES[run["agent_kind"]],
            AGENT_TYPES[candidate_kind],
            scenario,
        )
        verification["execution_type"] = "SIMULATED"
        verification["candidate_kind"] = candidate_kind
        verification_id = self.database.save_verification(
            owner_id, run["id"], verification
        )
        self.database.audit(owner_id, "verification.run", "run", run["id"])
        return self._json(
            HTTPStatus.CREATED,
            {"verification_id": verification_id, "verification": verification},
        )

    @staticmethod
    def _json(status: HTTPStatus, value: dict[str, Any]) -> tuple[int, str, bytes]:
        return status, "application/json; charset=utf-8", json.dumps(value).encode("utf-8")


def create_app(database_path: str | Path | None = None) -> AftershockApp:
    path = database_path or os.environ.get("AFTERSHOCK_DATABASE") or DEFAULT_DATABASE
    return AftershockApp(path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    with make_server("127.0.0.1", port, create_app()) as server:
        print(f"AFTERSHOCK dashboard available at http://127.0.0.1:{port}")
        server.serve_forever()
