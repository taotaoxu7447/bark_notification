from __future__ import annotations

import hashlib
import io
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
from contextlib import redirect_stdout
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402
import maintenance  # noqa: E402


class FakeTokenManager:
    def __init__(self) -> None:
        self.counter = 0
        self.finalized: list[server.IssuedNtfyToken] = []
        self.rolled_back: list[server.IssuedNtfyToken] = []
        self.revoked_devices: list[str] = []
        self.revoked_private_devices: list[tuple[str, str]] = []
        self.provisioned_channels: list[server.ProvisionedNtfyChannel] = []
        self.rolled_back_channels: list[server.ProvisionedNtfyChannel] = []
        self.fail_revoke = False
        self.fail_issue = False
        self.legacy_counts: dict[str, int] = {}
        self.legacy_acls_reset = False

    def provision_channel(self, subscriber_user: str, topic: str) -> server.ProvisionedNtfyChannel:
        channel = server.ProvisionedNtfyChannel(subscriber_user, topic)
        self.provisioned_channels.append(channel)
        return channel

    def rollback_channel(self, channel: server.ProvisionedNtfyChannel) -> None:
        self.rolled_back_channels.append(channel)

    def issue(self, subscriber_user: str, device_id: str) -> server.IssuedNtfyToken:
        if self.fail_issue:
            raise server.ProvisioningError("simulated sanitized failure")
        self.counter += 1
        return server.IssuedNtfyToken(
            f"tk_{self.counter:029d}", f"agentwatch-{device_id}", ()
        )

    def finalize(self, _subscriber_user: str, issued: server.IssuedNtfyToken) -> None:
        self.finalized.append(issued)

    def rollback(self, _subscriber_user: str, issued: server.IssuedNtfyToken) -> None:
        self.rolled_back.append(issued)

    def revoke_device(self, subscriber_user: str, device_id: str) -> int:
        if self.fail_revoke:
            raise server.ProvisioningError("simulated sanitized failure")
        self.revoked_private_devices.append((subscriber_user, device_id))
        return 1

    def revoke_legacy_device(self, device_id: str) -> int:
        if self.fail_revoke:
            raise server.ProvisioningError("simulated sanitized failure")
        self.revoked_devices.append(device_id)
        return self.legacy_counts.pop(device_id, 1)

    def legacy_token_count(self, device_id: str) -> int:
        return self.legacy_counts.get(device_id, 0)

    def audit_channel_acl(self, _subscriber_user: str, _topic: str) -> tuple[bool, bool]:
        return True, True

    def reset_legacy_acls(self) -> None:
        self.legacy_acls_reset = True


class FakePublisher:
    def __init__(self) -> None:
        self.calls = 0
        self.sources: list[str] = []
        self.targets: list[str] = []
        self.topics: list[str] = []
        self.events: list[dict[str, object]] = []
        self.test_failure: server.PublishError | None = None
        self.event_failure: server.PublishError | None = None

    def publish_test(self, topic: str, source: str, target: str) -> str:
        if self.test_failure is not None:
            raise self.test_failure
        self.calls += 1
        self.topics.append(topic)
        self.sources.append(source)
        self.targets.append(target)
        return "aw1_server_test_0123456789abcdef"

    def publish_event(
        self,
        topic: str,
        event_id: str,
        source: str,
        title: str,
        message: bytes,
        priority: str,
    ) -> None:
        if self.event_failure is not None:
            raise self.event_failure
        self.calls += 1
        self.topics.append(topic)
        self.events.append(
            {
                "event_id": event_id,
                "source": source,
                "title": title,
                "message": message,
                "priority": priority,
            }
        )


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        config = server.Config(
            database_path=Path(self.temporary.name) / "registration.db",
            invite_code="correct-horse-battery-staple",
            publisher_token="tk_" + "p" * 29,
            max_request_body=16 * 1024,
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
        return self.register_user("Alice.Example", "device-12345678", "OPPO Tablet")

    def register_user(
        self, username: str, device_id: str, device_name: str
    ) -> dict[str, object]:
        status, response = self.request(
            "/register",
            {
                "username": username,
                "password": "a sufficiently long password",
                "invite_code": "correct-horse-battery-staple",
                "device_id": device_id,
                "device_name": device_name,
            },
        )
        self.assertEqual(201, status)
        return response

    def computer_login(
        self,
        username: str = "alice.example",
        computer_id: str = "computer-12345678",
        computer_name: str = "Alice Mac",
        platform: str = "macos",
    ) -> dict[str, object]:
        status, response = self.request(
            "/computers/login",
            {
                "username": username,
                "password": "a sufficiently long password",
                "computer_id": computer_id,
                "computer_name": computer_name,
                "platform": platform,
            },
        )
        self.assertEqual(200, status)
        return response

    def test_register_returns_tokens_but_database_only_keeps_app_token_hash(self) -> None:
        response = self.register()
        self.assertEqual("alice.example", response["username"])
        self.assertTrue(str(response["ntfy_token"]).startswith("tk_"))
        self.assertEqual("wss", urllib.parse.urlparse(response["ntfy_ws_url"]).scheme)
        self.assertRegex(str(response["ntfy_topic"]), server.PRIVATE_TOPIC_PATTERN)
        self.assertTrue(str(response["ntfy_url"]).endswith("/" + str(response["ntfy_topic"])))
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
        self.assertEqual([credentials["ntfy_topic"]], self.publisher.topics)
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
        self.assertEqual(1, len(self.tokens.revoked_private_devices))
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
            data=b'{' + b'"padding":"' + b"x" * 17000 + b'"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(413, raised.exception.code)

    def test_private_channels_computers_and_cross_account_isolation(self) -> None:
        alice = self.register()
        bob = self.register_user("bob.example", "device-bob-12345", "Bob Phone")
        self.assertNotEqual(alice["ntfy_topic"], bob["ntfy_topic"])
        with self.database.connect() as connection:
            principals = connection.execute(
                "SELECT ntfy_subscriber_user FROM users ORDER BY username"
            ).fetchall()
        self.assertEqual(2, len({row[0] for row in principals}))

        alice_computer = self.computer_login()
        bob_computer = self.computer_login(
            "bob.example", "computer-bob-12345", "Bob PC", "windows"
        )
        self.assertNotEqual(alice_computer["computer_token"], bob_computer["computer_token"])

        publish = {
            "event_id": "aw2_alice_event-1",
            "source": "codex",
            "title": "Codex task complete",
            "body": "The requested task is complete.",
        }
        status, response = self.request(
            "/publish", publish, str(alice_computer["computer_token"])
        )
        self.assertEqual(202, status)

        self.assertEqual(publish["event_id"], response["event_id"])
        self.assertEqual(alice["ntfy_topic"], self.publisher.topics[-1])
        envelope = json.loads(self.publisher.events[-1]["message"])
        self.assertEqual("agentwatch_event_v2", envelope["schema"])
        self.assertEqual("computer-12345678", envelope["computer_id"])
        self.assertEqual("Alice Mac", envelope["computer_name"])
        self.assertIsInstance(envelope["sent_at"], int)

        status, response = self.request(
            "/publish", {**publish, "topic": bob["ntfy_topic"]}, str(alice_computer["computer_token"])
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_fields", response["error"])

        status, response = self.request("/computers", token=str(alice["app_token"]), method="GET")
        self.assertEqual(200, status)
        self.assertEqual(["computer-12345678"], [item["computer_id"] for item in response["computers"]])
        status, response = self.request(
            "/computers/revoke",
            {"computer_id": "computer-bob-12345"},
            str(alice["app_token"]),
        )
        self.assertEqual(404, status)
        self.assertEqual("computer_not_found", response["error"])

        status, _ = self.request(
            "/computers/revoke",
            {"computer_id": "computer-bob-12345"},
            str(bob["app_token"]),
        )
        self.assertEqual(200, status)
        status, response = self.request(
            "/publish",
            {**publish, "event_id": "aw2_bob_event-1"},
            str(bob_computer["computer_token"]),
        )
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", response["error"])
        status, _ = self.request(
            "/publish",
            {**publish, "event_id": "aw2_alice_event-2"},
            str(alice_computer["computer_token"]),
        )
        self.assertEqual(202, status)

    def test_publish_failures_log_only_safe_classification_and_keep_502_contract(self) -> None:
        mobile = self.register()
        computer = self.computer_login()
        publish = {
            "event_id": "sensitive-event-id-123",
            "source": "codex",
            "title": "SENSITIVE_TITLE_DO_NOT_LOG",
            "body": "SENSITIVE_BODY_DO_NOT_LOG",
        }
        self.publisher.event_failure = server.PublishError("http_5xx", 503)
        with self.assertLogs("agentwatch-registration", level="WARNING") as event_logs:
            status, response = self.request(
                "/publish", publish, str(computer["computer_token"])
            )

        self.assertEqual(502, status)
        self.assertEqual(
            {"error": "publish_failed", "message": "Notification could not be published"},
            response,
        )
        event_log = "\n".join(event_logs.output)
        self.assertIn("operation=event category=http_5xx http_status=503", event_log)

        self.publisher.test_failure = server.PublishError("timeout")
        with self.assertLogs("agentwatch-registration", level="WARNING") as test_logs:
            status, response = self.request(
                "/test", {"source": "codex"}, str(mobile["app_token"])
            )

        self.assertEqual(502, status)
        self.assertEqual(
            {"error": "publish_failed", "message": "Test notification could not be published"},
            response,
        )
        test_log = "\n".join(test_logs.output)
        self.assertIn("operation=test category=timeout", test_log)
        combined = event_log + "\n" + test_log
        for sensitive in (
            str(mobile["ntfy_topic"]),
            str(mobile["app_token"]),
            str(computer["computer_token"]),
            "alice.example",
            publish["event_id"],
            publish["title"],
            publish["body"],
            "tk_" + "p" * 29,
            json.dumps(publish),
        ):
            self.assertNotIn(str(sensitive), combined)

    def test_computer_token_is_hashed_expires_logs_out_and_rotates(self) -> None:
        self.register()
        first = self.computer_login()
        token = str(first["computer_token"])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT token_hash, expires_at FROM computers WHERE computer_id = ?",
                ("computer-12345678",),
            ).fetchone()
            columns = {item[1] for item in connection.execute("PRAGMA table_info(computers)")}
        self.assertEqual(hashlib.sha256(token.encode("ascii")).digest(), row["token_hash"])
        self.assertNotIn("computer_token", columns)
        self.assertEqual(first["expires_at"], row["expires_at"])

        second = self.computer_login(computer_name="Renamed Mac")
        self.assertNotEqual(token, second["computer_token"])
        status, _ = self.request(
            "/publish",
            {"event_id": "event-old", "source": "codex", "title": "Done", "body": "Done"},
            token,
        )
        self.assertEqual(401, status)
        current = str(second["computer_token"])
        status, _ = self.request("/computers/logout", {}, current)
        self.assertEqual(200, status)
        status, _ = self.request("/computers/logout", {}, current)
        self.assertEqual(401, status)

        third = self.computer_login()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE computers SET expires_at = 1 WHERE computer_id = ?",
                ("computer-12345678",),
            )
            connection.commit()
        status, response = self.request(
            "/publish",
            {"event_id": "event-expired", "source": "kimi", "title": "Done", "body": "Done"},
            str(third["computer_token"]),
        )
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", response["error"])

    def test_publish_validates_size_and_is_rate_limited_per_computer(self) -> None:
        self.register()
        computer = self.computer_login()
        token = str(computer["computer_token"])
        oversized = {
            "event_id": "large-event",
            "source": "grok",
            "title": "Large",
            "body": "界" * 2000,
        }
        status, response = self.request("/publish", oversized, token)
        self.assertEqual(413, status)
        self.assertEqual("message_too_large", response["error"])
        payload = {
            "event_id": "rate-event",
            "source": "zcode",
            "title": "Complete",
            "body": "Complete",
        }
        # The rejected oversized authenticated attempt also consumes one slot.
        for index in range(119):
            payload["event_id"] = f"rate-event-{index}"
            status, _ = self.request("/publish", payload, token)
            self.assertEqual(202, status)
        payload["event_id"] = "rate-event-blocked"
        status, response = self.request("/publish", payload, token)
        self.assertEqual(429, status)
        self.assertEqual("rate_limited", response["error"])

    def test_publish_rejects_ntfy_incompatible_event_ids_before_upstream(self) -> None:
        self.register()
        computer = self.computer_login()
        token = str(computer["computer_token"])
        base_payload = {
            "source": "zcode",
            "title": "Complete",
            "body": "Complete",
        }

        for event_id in ("a" * 65, "event.with.dot", "event:with:colon"):
            with self.subTest(event_id=event_id):
                status, response = self.request(
                    "/publish", {**base_payload, "event_id": event_id}, token
                )
                self.assertEqual(400, status)
                self.assertEqual("invalid_event_id", response["error"])

        # Rejection happens before NtfyPublisher, so it cannot be transformed
        # into the generic publish_failed 502 contract.
        self.assertEqual(0, self.publisher.calls)

        compatible = "aw2_" + "m" * 32 + "_" + "e" * 27
        self.assertEqual(64, len(compatible))
        status, response = self.request(
            "/publish", {**base_payload, "event_id": compatible}, token
        )
        self.assertEqual(202, status)
        self.assertTrue(response["ok"])
        self.assertEqual(1, self.publisher.calls)

    def test_publish_rate_limit_is_aggregated_across_account_computers(self) -> None:
        self.register()
        first = self.computer_login(
            computer_id="computer-first-1", computer_name="First Mac"
        )
        second = self.computer_login(
            computer_id="computer-second-2", computer_name="Second Mac"
        )
        tokens = [str(first["computer_token"]), str(second["computer_token"])]
        payload = {
            "event_id": "account-rate-0",
            "source": "codex",
            "title": "Complete",
            "body": "Complete",
        }
        for index in range(120):
            payload["event_id"] = f"account-rate-{index}"
            status, _ = self.request("/publish", payload, tokens[index % 2])
            self.assertEqual(202, status)
        payload["event_id"] = "account-rate-blocked"
        status, response = self.request("/publish", payload, tokens[1])
        self.assertEqual(429, status)
        self.assertEqual("rate_limited", response["error"])

    def test_health(self) -> None:
        status, response = self.request("/health", method="GET")
        self.assertEqual(200, status)
        self.assertTrue(response["ok"])


class MigrationTest(unittest.TestCase):
    def test_v01_schema_migrates_to_v2_idempotently_and_session_upgrade_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            hasher = server.PasswordHasher(n=2**10)
            salt, password_hash = hasher.hash_password("a sufficiently long password")
            app_token = "a" * 43
            app_hash = hashlib.sha256(app_token.encode("ascii")).digest()
            second_app_token = "b" * 43
            second_app_hash = hashlib.sha256(second_app_token.encode("ascii")).digest()
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                PRAGMA user_version = 0;
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE BINARY,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE devices (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL UNIQUE COLLATE BINARY,
                    device_name TEXT NOT NULL,
                    app_token_hash BLOB NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    last_login_at INTEGER NOT NULL
                );
                CREATE TABLE delivery_acks (
                    id INTEGER PRIMARY KEY,
                    device_row_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL,
                    acknowledged_at INTEGER NOT NULL,
                    UNIQUE(device_row_id, event_id)
                );
                """
            )
            cursor = connection.execute(
                "INSERT INTO users(username,password_salt,password_hash,created_at) VALUES(?,?,?,1)",
                ("legacy.user", salt, password_hash),
            )
            connection.execute(
                """
                INSERT INTO devices(
                    user_id,device_id,device_name,app_token_hash,created_at,last_login_at
                ) VALUES(?,?,?,?,1,1)
                """,
                (cursor.lastrowid, "legacy-device-1", "Legacy Tablet", app_hash),
            )
            connection.execute(
                """
                INSERT INTO devices(
                    user_id,device_id,device_name,app_token_hash,created_at,last_login_at
                ) VALUES(?,?,?,?,1,1)
                """,
                (cursor.lastrowid, "legacy-device-2", "Legacy Phone", second_app_hash),
            )
            connection.commit()
            connection.close()

            database = server.Database(path)
            database.initialize()
            database.initialize()
            with database.connect() as migrated:
                version = migrated.execute("PRAGMA user_version").fetchone()[0]
                user = migrated.execute("SELECT * FROM users").fetchone()
                computer_table = migrated.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='computers'"
                ).fetchone()
            self.assertEqual(2, version)
            self.assertIsNone(user["private_topic"])
            self.assertIsNone(user["ntfy_subscriber_user"])
            with database.connect() as migrated:
                self.assertEqual(
                    [None, None],
                    [
                        row["private_ready_at"]
                        for row in migrated.execute(
                            "SELECT private_ready_at FROM devices ORDER BY device_id"
                        )
                    ],
                )
            self.assertIsNotNone(computer_table)

            config = server.Config(
                path,
                "correct-horse-battery-staple",
                "tk_" + "p" * 29,
                scrypt_n=2**10,
            )
            tokens = FakeTokenManager()
            tokens.fail_issue = True
            application = server.AgentWatchApplication(
                config, database, tokens, FakePublisher(), hasher=hasher
            )
            headers = {"authorization": f"Bearer {app_token}"}
            with self.assertRaises(server.ApiError) as failed:
                application.handle(
                    "POST", f"{server.API_PREFIX}/session/upgrade", headers, b"{}", "192.0.2.10"
                )
            self.assertEqual("provisioning_failed", failed.exception.code)
            self.assertEqual([], tokens.revoked_devices)
            with database.connect() as migrated:
                self.assertEqual(
                    app_hash,
                    migrated.execute("SELECT app_token_hash FROM devices").fetchone()[0],
                )
                channel = migrated.execute(
                    "SELECT private_topic,ntfy_subscriber_user FROM users"
                ).fetchone()
                unready = migrated.execute(
                    "SELECT count(*) FROM devices WHERE private_ready_at IS NULL"
                ).fetchone()[0]
            self.assertIsNone(channel["private_topic"])
            self.assertIsNone(channel["ntfy_subscriber_user"])
            self.assertEqual(2, unready)
            self.assertEqual(1, len(tokens.rolled_back_channels))

            computer_login = json.dumps(
                {
                    "username": "legacy.user",
                    "password": "a sufficiently long password",
                    "computer_id": "legacy-computer-1",
                    "computer_name": "Legacy Mac",
                    "platform": "macos",
                }
            ).encode()
            with self.assertRaises(server.ApiError) as computer_blocked:
                application.handle(
                    "POST",
                    f"{server.API_PREFIX}/computers/login",
                    {},
                    computer_login,
                    "192.0.2.10",
                )
            self.assertEqual("app_upgrade_required", computer_blocked.exception.code)

            tokens.fail_issue = False
            response = application.handle(
                "POST", f"{server.API_PREFIX}/session/upgrade", headers, b"{}", "192.0.2.10"
            )
            self.assertEqual(200, response.status)
            self.assertRegex(response.payload["ntfy_topic"], server.PRIVATE_TOPIC_PATTERN)
            self.assertEqual(app_token, response.payload["app_token"])
            self.assertEqual(["legacy-device-1"], tokens.revoked_devices)
            with self.assertRaises(server.ApiError) as one_device_left:
                application.handle(
                    "POST",
                    f"{server.API_PREFIX}/computers/login",
                    {},
                    computer_login,
                    "192.0.2.10",
                )
            self.assertEqual("app_upgrade_required", one_device_left.exception.code)

            second_response = application.handle(
                "POST",
                f"{server.API_PREFIX}/login",
                {},
                json.dumps(
                    {
                        "username": "legacy.user",
                        "password": "a sufficiently long password",
                        "device_id": "legacy-device-2",
                        "device_name": "Legacy Phone",
                    }
                ).encode(),
                "192.0.2.11",
            )
            self.assertEqual(200, second_response.status)
            computer_response = application.handle(
                "POST",
                f"{server.API_PREFIX}/computers/login",
                {},
                computer_login,
                "192.0.2.10",
            )
            self.assertEqual(200, computer_response.status)
            with database.connect() as migrated:
                self.assertEqual(
                    0,
                    migrated.execute(
                        "SELECT count(*) FROM devices WHERE private_ready_at IS NULL"
                    ).fetchone()[0],
                )


class MaintenanceTest(unittest.TestCase):
    def test_private_audit_and_exact_computer_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = server.Database(Path(directory) / "registration.db")
            database.initialize()
            with database.connect() as connection:
                user = connection.execute(
                    """
                    INSERT INTO users(
                        username,password_salt,password_hash,created_at,
                        private_topic,ntfy_subscriber_user,channel_created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        "alice",
                        b"s" * 16,
                        b"h" * 32,
                        1,
                        "aw-" + "a" * 32,
                        "awu" + "b" * 24,
                        1,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO computers(
                        user_id,computer_id,computer_name,platform,token_hash,
                        created_at,last_login_at,last_seen_at,expires_at,revoked_at
                    ) VALUES(?,?,?,?,?,?,?,?,0,NULL)
                    """,
                    (
                        user.lastrowid,
                        "computer-12345678",
                        "Mac",
                        "macos",
                        b"t" * 32,
                        1,
                        1,
                        1,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO devices(
                        user_id,device_id,device_name,app_token_hash,
                        created_at,last_login_at,private_ready_at
                    ) VALUES(?,?,?,?,1,1,NULL)
                    """,
                    (user.lastrowid, "device-12345678", "Tablet", b"a" * 32),
                )
                connection.commit()
            manager = FakeTokenManager()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, maintenance.audit_private_channels(database, manager))
                self.assertEqual(0, maintenance.list_computers(database, "alice"))
                with self.assertRaises(ValueError):
                    maintenance.revoke_computer(database, "bob", "computer-12345678")
                self.assertEqual(
                    0,
                    maintenance.revoke_computer(
                        database, "alice", "computer-12345678"
                    ),
                )
                with self.assertRaises(ValueError):
                    maintenance.reset_legacy_acls(database, manager, True)
                with database.connect() as connection:
                    connection.execute("UPDATE devices SET private_ready_at = 1")
                    connection.commit()
                self.assertEqual(0, maintenance.reset_legacy_acls(database, manager, True))
            with database.connect() as connection:
                row = connection.execute("SELECT token_hash,revoked_at FROM computers").fetchone()
            self.assertNotEqual(b"t" * 32, row["token_hash"])
            self.assertIsNotNone(row["revoked_at"])
            self.assertTrue(manager.legacy_acls_reset)


class NtfyTokenManagerTest(unittest.TestCase):
    def test_acl_audit_requires_exact_single_subscriber_topic(self) -> None:
        topic = "aw-" + "a" * 32
        subscriber = "awu" + "b" * 24
        mode = {
            "subscriber_extra": False,
            "publisher_wildcard": False,
            "publisher_extra_wildcard": False,
        }

        def runner(
            command: list[str], _environment: server.Mapping[str, str], _timeout: float
        ) -> subprocess.CompletedProcess[str]:
            if command[1:3] == ["access", subscriber]:
                lines = [f"user {subscriber} (user)", f"- read-only access to topic {topic}"]
                if mode["subscriber_extra"]:
                    lines.append("- read-only access to topic agent-watch")
                return subprocess.CompletedProcess(command, 0, "\n".join(lines), "")
            if command[1:3] == ["access", "agent-watch-publisher"]:
                publisher_topic = topic + "*" if mode["publisher_wildcard"] else topic
                lines = [
                    "user agent-watch-publisher (user)",
                    f"- write-only access to topic {publisher_topic}",
                    "- write-only access to topic aw-ffffffffffffffffffffffffffffffff",
                ]
                if mode["publisher_extra_wildcard"]:
                    lines.append("- write-only access to topic aw-*")
                return subprocess.CompletedProcess(command, 0, "\n".join(lines), "")
            return subprocess.CompletedProcess(command, 1, "", "unexpected command")

        with tempfile.TemporaryDirectory() as directory:
            config = server.Config(
                Path(directory) / "db.sqlite",
                "correct-horse-battery-staple",
                "tk_" + "p" * 29,
            )
            manager = server.NtfyTokenManager(config, runner)
            self.assertEqual((True, True), manager.audit_channel_acl(subscriber, topic))
            mode["subscriber_extra"] = True
            self.assertEqual((False, True), manager.audit_channel_acl(subscriber, topic))
            mode["subscriber_extra"] = False
            mode["publisher_wildcard"] = True
            self.assertEqual((True, False), manager.audit_channel_acl(subscriber, topic))
            mode["publisher_wildcard"] = False
            mode["publisher_extra_wildcard"] = True
            self.assertEqual((True, False), manager.audit_channel_acl(subscriber, topic))

    def test_private_principal_password_is_env_only_and_acl_is_per_topic(self) -> None:
        commands: list[list[str]] = []
        environments: list[dict[str, str]] = []

        def runner(
            command: list[str], environment: server.Mapping[str, str], _timeout: float
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            environments.append(dict(environment))
            return subprocess.CompletedProcess(command, 0, "ok\n", "")

        with tempfile.TemporaryDirectory() as directory:
            config = server.Config(
                Path(directory) / "db.sqlite",
                "correct-horse-battery-staple",
                "tk_" + "p" * 29,
            )
            manager = server.NtfyTokenManager(config, runner)
            principal = "awu" + "a" * 24
            topic = "aw-" + "b" * 32
            manager.provision_channel(principal, topic)

        self.assertEqual(
            [config.ntfy_binary, "user", "add", "--role=user", principal], commands[0]
        )
        password = environments[0].get("NTFY_PASSWORD", "")
        self.assertGreaterEqual(len(password), 32)
        self.assertFalse(any(password in argument for command in commands for argument in command))
        self.assertNotIn("AGENTWATCH_INVITE_CODE", environments[0])
        self.assertNotIn("AGENTWATCH_NTFY_PUBLISHER_TOKEN", environments[0])
        self.assertEqual(
            [config.ntfy_binary, "access", principal, topic, "ro"], commands[1]
        )
        self.assertEqual(
            [config.ntfy_binary, "access", config.ntfy_publisher_user, topic, "wo"],
            commands[2],
        )
        self.assertNotIn("NTFY_PASSWORD", environments[1])

    def test_private_channel_acl_failure_rolls_back_exact_user_and_topic(self) -> None:
        commands: list[list[str]] = []
        publisher_access_seen = False

        def runner(
            command: list[str], _environment: server.Mapping[str, str], _timeout: float
        ) -> subprocess.CompletedProcess[str]:
            nonlocal publisher_access_seen
            commands.append(command)
            if (
                command[1:3] == ["access", "agent-watch-publisher"]
                and "--reset" not in command
                and not publisher_access_seen
            ):
                publisher_access_seen = True
                return subprocess.CompletedProcess(command, 1, "", "sensitive output ignored")
            return subprocess.CompletedProcess(command, 0, "ok\n", "")

        with tempfile.TemporaryDirectory() as directory:
            config = server.Config(
                Path(directory) / "db.sqlite",
                "correct-horse-battery-staple",
                "tk_" + "p" * 29,
            )
            manager = server.NtfyTokenManager(config, runner)
            principal = "awu" + "c" * 24
            topic = "aw-" + "d" * 32
            with self.assertRaises(server.ProvisioningError) as raised:
                manager.provision_channel(principal, topic)
        self.assertEqual(
            "ntfy command rejected the token operation", str(raised.exception)
        )
        self.assertIn(
            [config.ntfy_binary, "access", "--reset", config.ntfy_publisher_user, topic],
            commands,
        )
        self.assertIn([config.ntfy_binary, "user", "del", principal], commands)

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
            subscriber = "awu" + "a" * 24
            issued = manager.issue(subscriber, device_id)
            manager.finalize(subscriber, issued)
            revoked = manager.revoke_device(subscriber, device_id)

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
        topic = "aw-" + "a" * 32
        sequence_id = publisher.publish_test(topic, "codex", target)
        request = captured["request"]
        self.assertIsInstance(request, urllib.request.Request)
        self.assertEqual(f"Bearer {publisher_token}", request.get_header("Authorization"))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertIn("source_codex", query["tags"][0])
        self.assertNotIn("source_agentwatch_test", query["tags"][0])
        self.assertIn(target, query["tags"][0])
        self.assertEqual(sequence_id, request.get_header("X-sequence-id"))
        self.assertIsNone(request.get_header("X-cache"))
        envelope = json.loads(request.data)
        self.assertEqual("agentwatch_event_v2", envelope["schema"])
        self.assertEqual(sequence_id, envelope["event_id"])
        self.assertEqual("server-test", envelope["computer_id"])
        self.assertTrue(sequence_id.startswith("aw2_server_test_"))

        event_id = "aw2_machine_task-1"
        message = json.dumps(
            {
                "schema": "agentwatch_event_v2",
                "event_id": event_id,
                "source": "kimi",
                "title": "Done",
                "body": "Done",
                "computer_id": "machine-1",
                "computer_name": "Mac",
                "sent_at": 123,
            },
            separators=(",", ":"),
        ).encode()
        publisher.publish_event(topic, event_id, "kimi", "Done", message, "high")
        request = captured["request"]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(event_id, request.get_header("X-sequence-id"))
        self.assertEqual("agentwatch_v2,source_kimi", query["tags"][0])
        self.assertEqual("high", query["priority"][0])
        self.assertEqual(message, request.data)

    def test_upstream_failures_use_only_bounded_safe_classifications(self) -> None:
        sensitive_exception = "SENSITIVE_EXCEPTION_DO_NOT_LOG"
        sensitive_url = "http://127.0.0.1:2586/SENSITIVE_LEGACY_PATH"
        publisher_token = "tk_" + "s" * 29
        topic = "aw-" + "b" * 32
        event_id = "sensitive-event-id-456"
        title = "SENSITIVE_UPSTREAM_TITLE"
        message = b"SENSITIVE_UPSTREAM_BODY"

        class StatusResponse:
            def __init__(self, status: int) -> None:
                self.status = status

            def __enter__(self) -> "StatusResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return b"SENSITIVE_UPSTREAM_RESPONSE"

        def raising(error: Exception):
            def opener(_request: urllib.request.Request, timeout: float) -> StatusResponse:
                del timeout
                raise error

            return opener

        def response(status: int):
            def opener(_request: urllib.request.Request, timeout: float) -> StatusResponse:
                del timeout
                return StatusResponse(status)

            return opener

        cases = (
            (
                "timeout",
                raising(urllib.error.URLError(TimeoutError(sensitive_exception))),
                "timeout",
                None,
            ),
            (
                "connection_error",
                raising(urllib.error.URLError(OSError(sensitive_exception))),
                "connection_error",
                None,
            ),
            (
                "http_4xx",
                raising(
                    urllib.error.HTTPError(
                        sensitive_url,
                        403,
                        sensitive_exception,
                        {},
                        io.BytesIO(b"SENSITIVE_HTTP_BODY"),
                    )
                ),
                "http_4xx",
                403,
            ),
            ("http_5xx", response(503), "http_5xx", 503),
            ("other", raising(RuntimeError(sensitive_exception)), "other", None),
        )
        for name, opener, expected_category, expected_status in cases:
            with self.subTest(name=name):
                publisher = server.NtfyPublisher(sensitive_url, publisher_token, opener)
                with self.assertRaises(server.PublishError) as raised:
                    publisher.publish_event(
                        topic,
                        event_id,
                        "codex",
                        title,
                        message,
                        "default",
                    )
                failure = raised.exception
                self.assertEqual(expected_category, failure.category)
                self.assertEqual(expected_status, failure.http_status)
                rendered = str(failure)
                self.assertIn(f"category={expected_category}", rendered)
                for sensitive in (
                    sensitive_exception,
                    sensitive_url,
                    publisher_token,
                    topic,
                    event_id,
                    title,
                    message.decode("ascii"),
                    "SENSITIVE_HTTP_BODY",
                    "SENSITIVE_UPSTREAM_RESPONSE",
                ):
                    self.assertNotIn(sensitive, rendered)


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
