from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402


class FakeTokenManager:
    def __init__(self) -> None:
        self.counter = 0
        self.finalized: list[server.IssuedNtfyToken] = []
        self.rolled_back: list[server.IssuedNtfyToken] = []
        self.revoked_devices: list[str] = []
        self.fail_revoke = False

    def issue(self, device_id: str) -> server.IssuedNtfyToken:
        self.counter += 1
        return server.IssuedNtfyToken(
            f"tk_{self.counter:029d}", f"agentwatch-{device_id}", ()
        )

    def finalize(self, issued: server.IssuedNtfyToken) -> None:
        self.finalized.append(issued)

    def rollback(self, issued: server.IssuedNtfyToken) -> None:
        self.rolled_back.append(issued)

    def revoke_device(self, device_id: str) -> int:
        if self.fail_revoke:
            raise server.ProvisioningError("simulated sanitized failure")
        self.revoked_devices.append(device_id)
        return 1


class FakePublisher:
    def __init__(self) -> None:
        self.calls = 0
        self.sources: list[str] = []
        self.targets: list[str] = []

    def publish_test(self, source: str, target: str) -> str:
        self.calls += 1
        self.sources.append(source)
        self.targets.append(target)
        return "aw1_server_test_0123456789abcdef"


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        config = server.Config(
            database_path=Path(self.temporary.name) / "registration.db",
            invite_code="correct-horse-battery-staple",
            publisher_token="tk_" + "p" * 29,
            max_request_body=1024,
            scrypt_n=2**10,
        )
        self.database = server.Database(config.database_path)
        self.database.initialize()
        self.tokens = FakeTokenManager()
        self.publisher = FakePublisher()
        self.application = server.AgentWatchApplication(
            config,
            self.database,
            self.tokens,
            self.publisher,
            hasher=server.PasswordHasher(n=2**10),
        )
        self.httpd = server.AgentWatchHTTPServer(("127.0.0.1", 0), self.application)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}{server.API_PREFIX}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        payload: dict[str, object] | None = None,
        token: str | None = None,
        method: str = "POST",
    ) -> tuple[int, dict[str, object]]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def register(self) -> dict[str, object]:
        status, response = self.request(
            "/register",
            {
                "username": "Alice.Example",
                "password": "a sufficiently long password",
                "invite_code": "correct-horse-battery-staple",
                "device_id": "device-12345678",
                "device_name": "OPPO Tablet",
            },
        )
        self.assertEqual(201, status)
        return response

    def test_register_returns_tokens_but_database_only_keeps_app_token_hash(self) -> None:
        response = self.register()
        self.assertEqual("alice.example", response["username"])
        self.assertTrue(str(response["ntfy_token"]).startswith("tk_"))
        self.assertEqual("wss", urllib.parse.urlparse(response["ntfy_ws_url"]).scheme)
        expected_target = "target_" + hashlib.sha256(b"device-12345678").hexdigest()[:24]
        self.assertEqual(expected_target, response["target_tag"])
        self.assertNotIn("device-12345678", str(response["target_tag"]))

        with self.database.connect() as connection:
            row = connection.execute("SELECT app_token_hash FROM devices").fetchone()
            columns = {
                item[1] for item in connection.execute("PRAGMA table_info(devices)").fetchall()
            }
        app_token = str(response["app_token"])
        self.assertEqual(hashlib.sha256(app_token.encode("ascii")).digest(), row[0])
        self.assertNotIn("app_token", columns)
        self.assertNotIn("ntfy_token", columns)

    def test_login_rejects_wrong_password_and_rotates_tokens_on_success(self) -> None:
        first = self.register()
        payload = {
            "username": "alice.example",
            "password": "this is the wrong password",
            "device_id": "device-12345678",
            "device_name": "OPPO Tablet",
        }
        status, response = self.request("/login", payload)
        self.assertEqual(401, status)
        self.assertEqual("invalid_credentials", response["error"])

        payload["password"] = "a sufficiently long password"
        status, second = self.request("/login", payload)
        self.assertEqual(200, status)
        self.assertNotEqual(first["app_token"], second["app_token"])
        self.assertNotEqual(first["ntfy_token"], second["ntfy_token"])

        status, _ = self.request("/test", {}, str(first["app_token"]))
        self.assertEqual(401, status)
        status, _ = self.request("/test", {}, str(second["app_token"]))
        self.assertEqual(200, status)

    def test_test_endpoint_uses_app_auth_and_fixed_publisher(self) -> None:
        credentials = self.register()
        status, response = self.request(
            "/test", {"source": "codex"}, str(credentials["app_token"])
        )
        self.assertEqual(200, status)
        self.assertEqual("aw1_server_test_0123456789abcdef", response["event_id"])
        self.assertEqual(response["event_id"], response["sequence_id"])
        self.assertEqual(1, self.publisher.calls)
        self.assertEqual(["codex"], self.publisher.sources)
        expected_target = server.device_target_tag("device-12345678")
        self.assertEqual([expected_target], self.publisher.targets)
        self.assertEqual(expected_target, response["target_tag"])
        self.assertIn("global:test", self.application.limiter._events)

    def test_logout_revokes_ntfy_tokens_then_deletes_device(self) -> None:
        credentials = self.register()
        app_token = str(credentials["app_token"])
        status, response = self.request("/logout", {}, app_token)
        self.assertEqual(200, status)
        self.assertTrue(response["ok"])
        self.assertEqual(["device-12345678"], self.tokens.revoked_devices)
        with self.database.connect() as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM devices").fetchone()[0])
        status, response = self.request("/test", {"source": "codex"}, app_token)
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", response["error"])

    def test_logout_revoke_failure_keeps_device_and_returns_error(self) -> None:
        credentials = self.register()
        self.tokens.fail_revoke = True
        status, response = self.request("/logout", {}, str(credentials["app_token"]))
        self.assertEqual(503, status)
        self.assertEqual("logout_failed", response["error"])
        with self.database.connect() as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM devices").fetchone()[0])

    def test_ack_stores_only_delivery_metadata_and_is_idempotent(self) -> None:
        credentials = self.register()
        with self.database.connect() as connection:
            device_row_id = connection.execute("SELECT id FROM devices").fetchone()[0]
            connection.execute(
                """
                INSERT INTO delivery_acks(device_row_id, event_id, acknowledged_at)
                VALUES (?, ?, ?)
                """,
                (device_row_id, "old-delivery", 1),
            )
            connection.commit()
        for _ in range(2):
            status, response = self.request(
                "/ack",
                {
                    "event_id": "aw1_host_task-123",
                    "message_id": "ntfy-message-1",
                    "source": "codex",
                    "received_at": 1785600000000,
                    "app_version": "1.0.0",
                },
                str(credentials["app_token"]),
            )
            self.assertEqual(202, status)
            self.assertTrue(response["ok"])
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM delivery_acks").fetchall()
            columns = {
                item[1]
                for item in connection.execute("PRAGMA table_info(delivery_acks)").fetchall()
            }
        self.assertEqual(1, len(rows))
        self.assertEqual("aw1_host_task-123", rows[0]["event_id"])
        self.assertNotIn("body", columns)
        self.assertNotIn("message", columns)
        self.assertNotIn("source", columns)

    def test_ack_accepts_legacy_sequence_id_and_rejects_conflict(self) -> None:
        credentials = self.register()
        app_token = str(credentials["app_token"])
        status, _ = self.request("/ack", {"sequence_id": "legacy-event-1"}, app_token)
        self.assertEqual(202, status)
        status, response = self.request(
            "/ack", {"event_id": "event-1", "sequence_id": "event-2"}, app_token
        )
        self.assertEqual(400, status)
        self.assertEqual("conflicting_event_id", response["error"])

    def test_rejects_invalid_invite_unknown_fields_and_oversized_body(self) -> None:
        registration = {
            "username": "alice",
            "password": "a sufficiently long password",
            "invite_code": "incorrect-invitation-code",
            "device_id": "device-12345678",
            "device_name": "Tablet",
        }
        status, response = self.request("/register", registration)
        self.assertEqual(403, status)
        self.assertEqual("invalid_invite", response["error"])

        registration["invite_code"] = "correct-horse-battery-staple"
        registration["unexpected"] = "not accepted"
        status, response = self.request("/register", registration)
        self.assertEqual(400, status)
        self.assertEqual("invalid_fields", response["error"])

        request = urllib.request.Request(
            self.base_url + "/register",
            data=b'{' + b'"padding":"' + b"x" * 1100 + b'"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(413, raised.exception.code)

    def test_health(self) -> None:
        status, response = self.request("/health", method="GET")
        self.assertEqual(200, status)
        self.assertTrue(response["ok"])


class NtfyTokenManagerTest(unittest.TestCase):
    def test_uses_clean_environment_and_rotates_same_device_label(self) -> None:
        commands: list[list[str]] = []
        environments: list[dict[str, str]] = []
        device_id = "device-12345678"
        label = server.NtfyTokenManager._label(device_id)
        old_token = "tk_" + "o" * 29
        new_token = "tk_" + "n" * 29

        def runner(
            command: list[str], environment: server.Mapping[str, str], _timeout: float
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            environments.append(dict(environment))
            if command[2] == "list":
                stdout = f"user agent-watch-subscriber\n- {old_token} ({label}), never expires\n"
            elif command[2] == "add":
                stdout = f"token {new_token} created for user agent-watch-subscriber, never expires\n"
            else:
                stdout = "token removed\n"
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with tempfile.TemporaryDirectory() as directory:
            config = server.Config(
                Path(directory) / "db.sqlite",
                "correct-horse-battery-staple",
                "tk_" + "p" * 29,
            )
            manager = server.NtfyTokenManager(config, runner)
            issued = manager.issue(device_id)
            manager.finalize(issued)
            revoked = manager.revoke_device(device_id)

        self.assertEqual(new_token, issued.value)
        self.assertEqual((old_token,), issued.previous_values)
        self.assertEqual("token", commands[0][1])
        self.assertIn(old_token, commands[-1])
        self.assertEqual(1, revoked)
        self.assertNotIn("AGENTWATCH_NTFY_PUBLISHER_TOKEN", environments[0])
        self.assertNotIn("AGENTWATCH_INVITE_CODE", environments[0])


class NtfyPublisherTest(unittest.TestCase):
    def test_fixed_test_notification_has_private_auth_source_and_sequence(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status = 200

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, limit: int) -> bytes:
                self.limit = limit
                return b'{"id":"test"}'

        def opener(request: urllib.request.Request, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        publisher_token = "tk_" + "p" * 29
        publisher = server.NtfyPublisher(
            "http://127.0.0.1:2586/agent-watch", publisher_token, opener
        )
        target = server.device_target_tag("device-12345678")
        sequence_id = publisher.publish_test("codex", target)
        request = captured["request"]
        self.assertIsInstance(request, urllib.request.Request)
        self.assertEqual(f"Bearer {publisher_token}", request.get_header("Authorization"))
        self.assertIn("source_codex", request.get_header("X-tags"))
        self.assertNotIn("source_agentwatch_test", request.get_header("X-tags"))
        self.assertIn(target, request.get_header("X-tags"))
        self.assertEqual(sequence_id, request.get_header("X-sequence-id"))
        self.assertIsNone(request.get_header("X-cache"))
        self.assertTrue(sequence_id.startswith("aw1_server_test_"))


class SecurityPrimitiveTest(unittest.TestCase):
    def test_password_hash_and_limiter(self) -> None:
        hasher = server.PasswordHasher(n=2**10)
        salt, password_hash = hasher.hash_password("correct password")
        self.assertTrue(hasher.verify("correct password", salt, password_hash))
        self.assertFalse(hasher.verify("incorrect password", salt, password_hash))
        self.assertFalse(hasher.verify("incorrect password", None, None))

        now = [10.0]
        limiter = server.SlidingWindowLimiter(clock=lambda: now[0])
        self.assertTrue(limiter.allow("key", 2, 60))
        self.assertTrue(limiter.allow("key", 2, 60))
        self.assertFalse(limiter.allow("key", 2, 60))
        now[0] = 71.0
        self.assertTrue(limiter.allow("key", 2, 60))

        bounded = server.SlidingWindowLimiter(clock=lambda: now[0], max_keys=2)
        self.assertTrue(bounded.allow("one", 1, 60))
        self.assertTrue(bounded.allow("two", 1, 60))
        self.assertTrue(bounded.allow("three", 1, 60))
        self.assertEqual({"two", "three"}, set(bounded._events))

    def test_unknown_routes_share_one_rate_limit_key(self) -> None:
        class CapturingLimiter:
            def __init__(self) -> None:
                self.keys: list[str] = []

            def allow(self, key: str, _limit: int, _window: float) -> bool:
                self.keys.append(key)
                return True

        limiter = CapturingLimiter()
        with tempfile.TemporaryDirectory() as directory:
            config = server.Config(
                Path(directory) / "db.sqlite",
                "correct-horse-battery-staple",
                "tk_" + "p" * 29,
                scrypt_n=2**10,
            )
            database = server.Database(config.database_path)
            database.initialize()
            application = server.AgentWatchApplication(
                config,
                database,
                FakeTokenManager(),
                FakePublisher(),
                hasher=server.PasswordHasher(n=2**10),
                limiter=limiter,
            )
            for path in ("/random-one", "/random-two"):
                with self.assertRaises(server.ApiError):
                    application.handle("GET", path, {}, b"", "192.0.2.1")
        dynamic_keys = [key for key in limiter.keys if not key.startswith("global:")]
        self.assertEqual(2, len(dynamic_keys))
        self.assertEqual(dynamic_keys[0], dynamic_keys[1])


if __name__ == "__main__":
    unittest.main()
