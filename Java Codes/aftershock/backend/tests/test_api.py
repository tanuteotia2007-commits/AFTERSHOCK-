import json
import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults
from http.cookies import SimpleCookie

from aftershock.web import Database, create_app


TIMEOUT_SCENARIO = {
    "name": "payment-timeout-after-charge",
    "faults": [
        {
            "type": "timeout",
            "target": "payments.charge",
            "on_call": 1,
            "params": {"effect_applied": True},
        }
    ],
    "task": {"order_id": "timeout-demo-order", "skus": ["sku-a", "sku-b"], "amount": 4999},
    "seed": 7,
}


class ApiIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "aftershock.sqlite3"
        self.app = create_app(self.database_path)
        self.cookie = ""
        status, _ = self.request(
            "POST",
            "/api/auth/register",
            {"username": "test-user", "password": "correct-horse-123"},
        )
        self.assertEqual(status, 201)

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, method, path, value=None):
        body = b"" if value is None else json.dumps(value).encode("utf-8")
        environ = {}
        setup_testing_defaults(environ)
        environ.update(
            {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "wsgi.input": BytesIO(body),
                "REMOTE_ADDR": f"test-{id(self)}",
            }
        )
        if self.cookie:
            environ["HTTP_COOKIE"] = self.cookie
        response = {}

        def start_response(status, headers):
            response["status"] = int(status.split(" ", 1)[0])
            response["headers"] = headers

        response_body = b"".join(self.app(environ, start_response))
        cookies = SimpleCookie()
        for name, value in response.get("headers", []):
            if name.lower() == "set-cookie":
                cookies.load(value)
                if "aftershock_session" in cookies:
                    self.cookie = f"aftershock_session={cookies['aftershock_session'].value}"
        if response.get("status") == 200 and path == "/":
            return response["status"], response_body
        return response["status"], json.loads(response_body)

    def register(self, kind="naive"):
        status, body = self.request(
            "POST",
            "/api/agents",
            {"name": f"Demo {kind}", "kind": kind},
        )
        self.assertEqual(status, 201)
        return body["agent"]

    def test_register_mock_agent_and_list_it(self):
        agent = self.register()
        status, body = self.request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(body["agents"][0]["id"], agent["id"])
        self.assertEqual(body["agents"][0]["kind"], "naive")
        self.assertEqual(
            body["agents"][0]["label"],
            "SIMULATED · MOCK naive-order-agent v1",
        )

    def test_agentverse_type_requires_credentials_and_saves_live_result(self):
        agent = self.register("agentverse")
        with patch.dict(
            os.environ,
            {"AGENTVERSE_AGENT_ADDRESS": "", "AGENTVERSE_API_KEY": ""},
        ):
            status, body = self.request(
                "POST",
                "/api/runs",
                {
                    "agent_id": agent["id"],
                    "scenario": {"message": "Hello", "fault": "dropped_message"},
                },
            )
        self.assertEqual(status, 503)
        self.assertIn("AGENTVERSE_AGENT_ADDRESS", body["error"])
        self.assertIn("AGENTVERSE_API_KEY", body["error"])

        fake_result = {
            "scenario": {"name": "agentverse-dropped-message"},
            "agent": "Agentverse agent",
            "execution_type": "REAL AGENT + SIMULATED MESSAGE FAULT",
            "passed": False,
            "violations": [
                {
                    "code": "MESSAGE_DROPPED",
                    "severity": "high",
                    "message": "not sent",
                    "event_seqs": [1],
                }
            ],
            "outcome": {"responses": []},
            "error": "not sent",
            "elapsed_ms": 1,
            "fingerprint": "live-fingerprint",
            "events": [{"seq": 0, "kind": "fault_injected"}],
        }
        with (
            patch.dict(
                os.environ,
                {
                    "AGENTVERSE_AGENT_ADDRESS": "agent1example",
                    "AGENTVERSE_API_KEY": "test-key",
                },
            ),
            patch("aftershock.web.AgentverseAdapter") as adapter,
        ):
            adapter.return_value.run.return_value = fake_result
            status, body = self.request(
                "POST",
                "/api/runs",
                {
                    "agent_id": agent["id"],
                    "scenario": {"message": "Hello", "fault": "dropped_message"},
                },
            )
        self.assertEqual(status, 201)
        self.assertEqual(
            body["run"]["execution_type"],
            "REAL AGENT + SIMULATED MESSAGE FAULT",
        )
        self.assertEqual(body["run"]["findings"][0]["status"], "OBSERVED")

    def test_agent_table_migrates_existing_run_foreign_keys(self):
        database_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(database_path)
        connection.executescript(
            """
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
                kind TEXT NOT NULL CHECK(kind IN ('naive', 'resilient')),
                label TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY,
                agent_id INTEGER NOT NULL REFERENCES agents(id),
                scenario_name TEXT NOT NULL,
                passed INTEGER NOT NULL,
                violations_count INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL,
                findings_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO agents VALUES (1, 'Existing mock', 'naive', 'mock', 'now');
            INSERT INTO runs VALUES (1, 1, 'existing', 1, 0, 'fingerprint', '{}', '[]', 'now');
            """
        )
        connection.close()

        database = Database(database_path)
        user = database.create_user("legacy-owner", "not-used-in-test")
        live = database.register_agent("Hosted agent", "agentverse", user["id"])
        self.assertEqual(live["kind"], "agentverse")
        connection = sqlite3.connect(database_path)
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        finally:
            connection.close()

    def test_timeout_run_saves_reproduced_findings_and_event_evidence(self):
        agent = self.register()
        status, body = self.request(
            "POST",
            "/api/runs",
            {"agent_id": agent["id"], "scenario": TIMEOUT_SCENARIO},
        )
        self.assertEqual(status, 201)
        run = body["run"]
        self.assertFalse(run["passed"])
        self.assertIn("DUPLICATE_CHARGE", {finding["code"] for finding in run["findings"]})
        self.assertTrue(all(finding["status"] == "REPRODUCED" for finding in run["findings"]))
        self.assertTrue(
            any(
                event["kind"] == "fault_injected"
                and event.get("fault") == "timeout"
                and event.get("service") == "payments"
                for event in run["result"]["events"]
            )
        )
        self.assertTrue(
            any(
                event["kind"] == "effect_applied"
                and event.get("service") == "payments"
                and event.get("applied")
                for event in run["result"]["events"]
            )
        )

        self.app = create_app(self.database_path)
        status, detail = self.request("GET", f"/api/runs/{run['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["run"]["fingerprint"], run["fingerprint"])
        self.assertEqual(detail["run"]["result"]["events"], run["result"]["events"])

        status, dashboard = self.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["runs"][0]["id"], run["id"])
        self.assertEqual(dashboard["runs"][0]["findings"][0]["status"], "REPRODUCED")

    def test_rejects_unknown_agent_type_and_invalid_scenario(self):
        status, body = self.request(
            "POST",
            "/api/agents",
            {"name": "Unknown", "kind": "remote"},
        )
        self.assertEqual(status, 400)
        self.assertIn("kind must be one of", body["error"])

        agent = self.register()
        status, body = self.request(
            "POST",
            "/api/runs",
            {"agent_id": agent["id"], "scenario": {"name": "invalid"}},
        )
        self.assertEqual(status, 400)
        self.assertIn("task", body["error"])

        status, body = self.request(
            "POST",
            "/api/agents",
            {"name": "Invalid kind", "kind": []},
        )
        self.assertEqual(status, 400)
        self.assertIn("kind must be one of", body["error"])

    def test_dashboard_serves_page_that_uses_api(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        self.assertIn("Run failure scenario", page)
        self.assertIn('api("/api/dashboard")', page)

    def test_authentication_and_resource_ownership(self):
        agent = self.register()
        status, run_body = self.request(
            "POST",
            "/api/runs",
            {"agent_id": agent["id"], "scenario": TIMEOUT_SCENARIO},
        )
        self.assertEqual(status, 201)
        owner_cookie = self.cookie

        self.cookie = ""
        status, body = self.request("GET", "/api/agents")
        self.assertEqual(status, 401)
        self.assertIn("sign in", body["error"])

        status, _ = self.request(
            "POST",
            "/api/auth/register",
            {"username": "other-user", "password": "another-password-123"},
        )
        self.assertEqual(status, 201)
        self.assertNotEqual(self.cookie, owner_cookie)
        status, body = self.request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(body["agents"], [])
        status, body = self.request("GET", f"/api/runs/{run_body['run']['id']}")
        self.assertEqual(status, 404)

        status, body = self.request(
            "POST",
            "/api/auth/login",
            {"username": "test-user", "password": "wrong-password-123"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "invalid username or password")

    def test_replay_suggestions_and_approved_verification_are_persisted(self):
        agent = self.register()
        status, body = self.request(
            "POST",
            "/api/runs",
            {"agent_id": agent["id"], "scenario": TIMEOUT_SCENARIO},
        )
        self.assertEqual(status, 201)
        run_id = body["run"]["id"]

        status, replay_body = self.request(
            "POST", f"/api/runs/{run_id}/replay", {}
        )
        self.assertEqual(status, 201)
        self.assertTrue(replay_body["replay"]["identical"])

        status, suggestions = self.request(
            "POST", f"/api/runs/{run_id}/fixes", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(suggestions["suggestions"][0]["status"], "HYPOTHESIS")

        status, body = self.request(
            "POST",
            f"/api/runs/{run_id}/verify",
            {"candidate_kind": "resilient", "approved": False},
        )
        self.assertEqual(status, 400)
        status, verification = self.request(
            "POST",
            f"/api/runs/{run_id}/verify",
            {"candidate_kind": "resilient", "approved": True},
        )
        self.assertEqual(status, 201)
        self.assertEqual(verification["verification"]["status"], "VERIFIED_FIX")
        status, persisted = self.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertTrue(persisted["run"]["replays"])
        self.assertEqual(persisted["run"]["suggestions"][0]["status"], "HYPOTHESIS")
        self.assertEqual(
            persisted["run"]["verifications"][0]["result"]["status"], "VERIFIED_FIX"
        )

    def test_scenario_builder_persists_validated_scenario(self):
        status, body = self.request(
            "POST",
            "/api/scenarios",
            {"scenario": TIMEOUT_SCENARIO},
        )
        self.assertEqual(status, 201)
        status, response = self.request("GET", "/api/scenarios")
        self.assertEqual(status, 200)
        self.assertEqual(response["scenarios"][0]["id"], body["scenario"]["id"])

    def test_dashboard_metrics_and_audit_events_are_persisted_per_user(self):
        agent = self.register()
        status, _ = self.request(
            "POST",
            "/api/runs",
            {"agent_id": agent["id"], "scenario": TIMEOUT_SCENARIO},
        )
        self.assertEqual(status, 201)
        status, dashboard = self.request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["metrics"]["agents_registered"], 1)
        self.assertEqual(dashboard["metrics"]["runs_saved"], 1)
        self.assertEqual(dashboard["metrics"]["simulated_runs"], 1)
        self.assertEqual(dashboard["metrics"]["real_agent_runs"], 0)
        self.assertIsNotNone(dashboard["metrics"]["simulated_pass_rate"])
        self.assertIsNone(dashboard["metrics"]["real_agent_reply_rate"])

        connection = sqlite3.connect(self.database_path)
        try:
            actions = {
                row[0]
                for row in connection.execute(
                    "SELECT action FROM audit_events ORDER BY id"
                )
            }
        finally:
            connection.close()
        self.assertTrue(
            {"auth.register", "agent.register", "run.execute"}.issubset(actions)
        )


if __name__ == "__main__":
    unittest.main()
