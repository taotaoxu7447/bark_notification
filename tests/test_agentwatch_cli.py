from __future__ import annotations

import json
import io
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest import mock

import agentwatch
import agentwatch_core
import codex_watch_notifier as notifier


class FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def read(self, limit: int = -1) -> bytes:
        del limit
        return self.payload


class FakeMacSecurityNative:
    def __init__(self) -> None:
        self.add_status = agentwatch_core.ERR_SEC_SUCCESS
        self.update_status = agentwatch_core.ERR_SEC_SUCCESS
        self.read_status = agentwatch_core.ERR_SEC_ITEM_NOT_FOUND
        self.delete_status = agentwatch_core.ERR_SEC_SUCCESS
        self.read_secret: bytearray | None = None
        self.add_calls: list[tuple[str, str, bytearray]] = []
        self.update_calls: list[tuple[str, str, bytearray]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def add(self, service: str, account: str, secret: bytearray) -> int:
        self.add_calls.append((service, account, secret))
        return self.add_status

    def update(self, service: str, account: str, secret: bytearray) -> int:
        self.update_calls.append((service, account, secret))
        return self.update_status

    def read(self, service: str, account: str) -> tuple[int, bytearray | None]:
        del service, account
        return self.read_status, self.read_secret

    def delete(self, service: str, account: str) -> int:
        self.delete_calls.append((service, account))
        return self.delete_status


class MachineIdentityTests(unittest.TestCase):
    def test_machine_identity_is_stable_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with mock.patch.object(agentwatch_core.platform_module, "node", return_value="test-mac"), mock.patch.object(
                agentwatch_core.platform_module, "system", return_value="Darwin"
            ):
                first = agentwatch_core.load_or_create_machine(root)
                second = agentwatch_core.load_or_create_machine(root)

            self.assertEqual(first, second)
            self.assertEqual("test-mac", first["computer_name"])
            self.assertEqual("macos", first["platform"])
            self.assertEqual(0, stat.S_IMODE((root / "machine.json").stat().st_mode) & 0o077)

    def test_read_only_machine_load_does_not_create_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            self.assertIsNone(agentwatch_core.load_machine(root))
            self.assertFalse((root / "machine.json").exists())


class ReadOnlyStatusAndPersistentConfigTests(unittest.TestCase):
    @staticmethod
    def claude_status() -> dict:
        return {
            "enabled": False,
            "configured": False,
            "events_path_safe": True,
            "active": False,
            "policy_active": False,
            "cli_detected": False,
            "cli_compatible": False,
        }

    def test_status_uses_persistent_api_base_without_creating_identity_or_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.config.mkdir(parents=True)
            env_path = paths.config / "env"
            env_path.write_text(
                "AGENTWATCH_API_BASE=https://private.example.test/api/v1\n"
                "BARK_URL=https://bark.example.test/device\n",
                encoding="utf-8",
            )
            service = mock.Mock()
            service.installed.return_value = False
            service.state.return_value = "not loaded"

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                agentwatch, "_installed_claude_hook_status", return_value=self.claude_status()
            ):
                result = agentwatch._status(paths, service)

            self.assertEqual("https://private.example.test/api/v1", result["api_base"])
            self.assertEqual("bark", result["delivery_mode"])
            self.assertEqual("", result["computer_id"])
            self.assertFalse((paths.config / "machine.json").exists())
            self.assertFalse((paths.config / "settings.json").exists())
            self.assertEqual([env_path], list(paths.config.iterdir()))

    def test_shell_api_base_overrides_persistent_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.config.mkdir(parents=True)
            (paths.config / "env").write_text(
                "AGENTWATCH_API_BASE=https://persistent.example.test/api/v1\n",
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {"AGENTWATCH_API_BASE": "https://shell.example.test/api/v1/"},
                clear=True,
            ):
                configured = agentwatch._configured_api_base(paths)

            self.assertEqual("https://shell.example.test/api/v1", configured)

    def test_non_utf8_persistent_env_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.config.mkdir(parents=True)
            (paths.config / "env").write_bytes(b"BARK_URL=https://example.test/\xff\n")

            with self.assertRaises(agentwatch_core.AgentWatchError):
                agentwatch._config_values(paths)


class CredentialStoreTests(unittest.TestCase):
    def test_linux_fallback_uses_0600_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = agentwatch_core.ComputerTokenStore(
                "computer-1", root, system_name="Linux", which=lambda _: None
            )
            store.save("secret-computer-token")

            self.assertEqual("secret-computer-token", store.load())
            self.assertEqual(0o600, stat.S_IMODE((root / "computer-token").stat().st_mode))
            store.delete()
            self.assertIsNone(store.load())

    def test_macos_keychain_native_add_and_update_zero_temporary_secret(self) -> None:
        native = FakeMacSecurityNative()
        keychain = agentwatch_core.MacOSKeychain(native)
        with mock.patch.object(agentwatch_core.subprocess, "run") as subprocess_run:
            keychain.save("computer-1", "first-secret-token")
        subprocess_run.assert_not_called()
        self.assertEqual(1, len(native.add_calls))
        self.assertEqual([], native.update_calls)
        self.assertTrue(all(value == 0 for value in native.add_calls[0][2]))

        native.add_status = agentwatch_core.ERR_SEC_DUPLICATE_ITEM
        keychain.save("computer-1", "updated-secret-token")
        self.assertEqual(1, len(native.update_calls))
        self.assertTrue(all(value == 0 for value in native.update_calls[0][2]))

    def test_macos_keychain_native_read_and_delete(self) -> None:
        native = FakeMacSecurityNative()
        keychain = agentwatch_core.MacOSKeychain(native)
        native.read_status = agentwatch_core.ERR_SEC_SUCCESS
        native.read_secret = bytearray(b"loaded-computer-token")

        self.assertEqual("loaded-computer-token", keychain.load("computer-1"))
        self.assertTrue(all(value == 0 for value in native.read_secret))
        keychain.delete("computer-1")
        self.assertEqual([(agentwatch_core.KEYCHAIN_SERVICE, "computer-1")], native.delete_calls)

        native.delete_status = agentwatch_core.ERR_SEC_ITEM_NOT_FOUND
        keychain.delete("computer-1")

    def test_macos_keychain_error_mapping_never_contains_token(self) -> None:
        native = FakeMacSecurityNative()
        native.add_status = agentwatch_core.ERR_SEC_INTERACTION_NOT_ALLOWED
        keychain = agentwatch_core.MacOSKeychain(native)
        token = "must-never-appear-in-error"

        with self.assertRaises(agentwatch_core.AgentWatchError) as caught:
            keychain.save("computer-1", token)

        self.assertIn("locked or unavailable", str(caught.exception))
        self.assertNotIn(token, str(caught.exception))

    def test_computer_token_store_uses_injected_macos_keychain(self) -> None:
        keychain = mock.Mock()
        keychain.load.return_value = "stored-token"
        store = agentwatch_core.ComputerTokenStore(
            "computer-1",
            Path("/tmp/unused"),
            system_name="Darwin",
            macos_keychain=keychain,
        )

        store.save("new-token")
        self.assertEqual("stored-token", store.load())
        store.delete()
        keychain.save.assert_called_once_with("computer-1", "new-token")
        keychain.delete.assert_called_once_with("computer-1")

    def test_linux_secret_service_failure_falls_back_and_can_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            failed = mock.Mock(returncode=1, stdout="", stderr="unavailable")
            with mock.patch.object(agentwatch_core.subprocess, "run", return_value=failed):
                store = agentwatch_core.ComputerTokenStore(
                    "computer-1", root, system_name="Linux", which=lambda _: "/usr/bin/secret-tool"
                )
                store.save("fallback-token")
                loaded = store.load()

            self.assertEqual("fallback-token", loaded)
            self.assertEqual("0600 private file", store.backend_name())


class ApiContractTests(unittest.TestCase):
    def test_login_uses_plural_computers_endpoint_and_exact_fields(self) -> None:
        captured = {}

        def urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(200, {"computer_token": "one-time-token", "username": "alice"})

        machine = {
            "computer_id": "11111111-1111-4111-8111-111111111111",
            "computer_name": "Alice Mac",
            "platform": "macos",
        }
        with mock.patch.object(agentwatch_core.urllib.request, "urlopen", side_effect=urlopen):
            response = agentwatch_core.AgentWatchApi("https://example.test/api/v1").login(
                "alice", "password-only-in-request", machine
            )

        request = captured["request"]
        self.assertEqual("https://example.test/api/v1/computers/login", request.full_url)
        self.assertEqual(
            {
                "username": "alice",
                "password": "password-only-in-request",
                "computer_id": machine["computer_id"],
                "computer_name": "Alice Mac",
                "platform": "macos",
            },
            json.loads(request.data),
        )
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual("one-time-token", response["computer_token"])

    def test_publish_cannot_supply_topic_or_user(self) -> None:
        captured = {}

        def urlopen(request, timeout):
            del timeout
            captured["request"] = request
            return FakeResponse(202, {"ok": True, "event_id": "event-1"})

        with mock.patch.object(agentwatch_core.urllib.request, "urlopen", side_effect=urlopen):
            response = agentwatch_core.AgentWatchApi("https://example.test/api/v1").publish(
                "computer-token",
                event_id="event-1",
                source="codex",
                title="完成",
                body="任务已完成",
                priority="default",
            )

        request = captured["request"]
        payload = json.loads(request.data)
        self.assertEqual("https://example.test/api/v1/publish", request.full_url)
        self.assertEqual(
            {"event_id": "event-1", "source": "codex", "title": "完成", "body": "任务已完成", "priority": "default"},
            payload,
        )
        for forbidden in ("topic", "user", "username", "url", "tags", "icon"):
            self.assertNotIn(forbidden, payload)
        self.assertEqual("Bearer computer-token", request.headers["Authorization"])
        self.assertTrue(response["ok"])

    def test_logout_revokes_the_current_bearer_with_empty_body(self) -> None:
        captured = {}

        def urlopen(request, timeout):
            del timeout
            captured["request"] = request
            return FakeResponse(200, {"ok": True})

        with mock.patch.object(agentwatch_core.urllib.request, "urlopen", side_effect=urlopen):
            response = agentwatch_core.AgentWatchApi("https://example.test/api/v1").logout("computer-token")

        request = captured["request"]
        self.assertEqual("https://example.test/api/v1/computers/logout", request.full_url)
        self.assertEqual({}, json.loads(request.data))
        self.assertEqual("Bearer computer-token", request.headers["Authorization"])
        self.assertEqual("agentwatch-computer/0.3.0", request.headers["User-agent"])
        self.assertTrue(response["ok"])


class ClaudeHookIngestorTests(unittest.TestCase):
    def test_stop_hook_appends_only_validated_fields_to_private_spool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transcript = root / "session.jsonl"
            transcript.write_text("one completed turn\n", encoding="utf-8")
            spool = root / "private" / "claude-hook-events.jsonl"
            payload = {
                "session_id": "session-123",
                "prompt_id": "prompt-456",
                "transcript_path": str(transcript),
                "cwd": "/tmp/project",
                "permission_mode": "default",
                "hook_event_name": "Stop",
                "stop_hook_active": False,
                "last_assistant_message": "任务已完成。",
                "background_tasks": [],
                "session_crons": [],
                "untrusted_extra": "must not be persisted",
            }

            with mock.patch.object(agentwatch.time, "time", return_value=123):
                appended = agentwatch.ingest_claude_hook_event(
                    io.StringIO(json.dumps(payload, ensure_ascii=False)),
                    events_path=spool,
                )

            self.assertTrue(appended)
            record = json.loads(spool.read_text(encoding="utf-8"))
            self.assertEqual(agentwatch.CLAUDE_HOOK_SCHEMA, record["schema"])
            self.assertEqual("Stop", record["hook_event_name"])
            self.assertEqual("prompt-456", record["prompt_id"])
            self.assertEqual(transcript.stat().st_size, record["transcript_size"])
            self.assertEqual(123, record["received_at"])
            self.assertFalse(record["stop_hook_active"])
            self.assertFalse(record["has_background_tasks"])
            self.assertFalse(record["has_session_crons"])
            self.assertEqual(
                agentwatch.hashlib.sha256("任务已完成。".encode("utf-8")).hexdigest(),
                record["last_assistant_message_sha256"],
            )
            self.assertNotIn("permission_mode", record)
            self.assertNotIn("untrusted_extra", record)
            self.assertEqual(0, stat.S_IMODE(spool.stat().st_mode) & 0o077)
            self.assertEqual(0, stat.S_IMODE(spool.parent.stat().st_mode) & 0o077)

    def test_stop_failure_and_inflight_state_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transcript = root / "session.jsonl"
            transcript.write_text("failure\n", encoding="utf-8")
            failure_spool = root / "failure.jsonl"
            failure = {
                "session_id": "session-123",
                "prompt_id": "prompt-error",
                "transcript_path": str(transcript),
                "cwd": "/tmp/project",
                "hook_event_name": "StopFailure",
                "error": "rate_limit",
                "error_details": "429 Too Many Requests",
                "last_assistant_message": "API Error: Rate limit reached",
            }
            inflight_spool = root / "inflight.jsonl"
            inflight = {
                "session_id": "session-123",
                "prompt_id": "prompt-running",
                "transcript_path": str(transcript),
                "cwd": "/tmp/project",
                "hook_event_name": "Stop",
                "last_assistant_message": "后台任务还在运行。",
                "background_tasks": [{"id": "task-1", "status": "running"}],
                "session_crons": [{"id": "cron-1"}],
            }

            self.assertTrue(
                agentwatch.ingest_claude_hook_event(io.StringIO(json.dumps(failure)), events_path=failure_spool)
            )
            self.assertTrue(
                agentwatch.ingest_claude_hook_event(io.StringIO(json.dumps(inflight)), events_path=inflight_spool)
            )

            failure_record = json.loads(failure_spool.read_text(encoding="utf-8"))
            inflight_record = json.loads(inflight_spool.read_text(encoding="utf-8"))
            self.assertEqual("rate_limit", failure_record["error"])
            self.assertFalse(failure_record["stop_hook_active"])
            self.assertFalse(failure_record["has_background_tasks"])
            self.assertTrue(inflight_record["has_background_tasks"])
            self.assertTrue(inflight_record["has_session_crons"])
            self.assertNotIn("task-1", inflight_spool.read_text(encoding="utf-8"))
            self.assertNotIn("cron-1", inflight_spool.read_text(encoding="utf-8"))

    def test_all_official_stop_failure_error_types_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transcript = root / "session.jsonl"
            transcript.write_text("failure\n", encoding="utf-8")
            official_errors = {
                "rate_limit",
                "overloaded",
                "authentication_failed",
                "oauth_org_not_allowed",
                "billing_error",
                "invalid_request",
                "model_not_found",
                "server_error",
                "max_output_tokens",
                "unknown",
            }
            self.assertEqual(official_errors, agentwatch.CLAUDE_STOP_FAILURE_ERRORS)
            for error_name in sorted(official_errors):
                with self.subTest(error=error_name):
                    spool = root / f"{error_name}.jsonl"
                    payload = {
                        "session_id": "session-123",
                        "prompt_id": f"prompt-{error_name}",
                        "transcript_path": str(transcript),
                        "cwd": "/tmp/project",
                        "hook_event_name": "StopFailure",
                        "error": error_name,
                        "error_details": "official error",
                        "last_assistant_message": "API request failed",
                    }
                    self.assertTrue(
                        agentwatch.ingest_claude_hook_event(
                            io.StringIO(json.dumps(payload)), events_path=spool
                        )
                    )
                    self.assertEqual(
                        error_name,
                        json.loads(spool.read_text(encoding="utf-8"))["error"],
                    )

    def test_invalid_and_subagent_payloads_are_ignored_but_continuation_stop_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spool = Path(temp_dir) / "events.jsonl"
            base = {
                "session_id": "session-123",
                "transcript_path": "/tmp/session.jsonl",
                "cwd": "/tmp/project",
                "last_assistant_message": "done",
            }
            subagent = dict(base, hook_event_name="SubagentStop", agent_id="agent-1")
            invalid_error = dict(base, hook_event_name="StopFailure", error="not-an-official-error")
            repeated_stop = dict(base, hook_event_name="Stop", stop_hook_active=True)
            invalid_stop_flag = dict(base, hook_event_name="Stop", stop_hook_active="true")

            self.assertFalse(
                agentwatch.ingest_claude_hook_event(io.StringIO(json.dumps(subagent)), events_path=spool)
            )
            self.assertFalse(
                agentwatch.ingest_claude_hook_event(io.StringIO(json.dumps(invalid_error)), events_path=spool)
            )
            self.assertTrue(
                agentwatch.ingest_claude_hook_event(io.StringIO(json.dumps(repeated_stop)), events_path=spool)
            )
            self.assertFalse(
                agentwatch.ingest_claude_hook_event(io.StringIO(json.dumps(invalid_stop_flag)), events_path=spool)
            )
            self.assertFalse(agentwatch.ingest_claude_hook_event(io.StringIO("{"), events_path=spool))
            record = json.loads(spool.read_text(encoding="utf-8"))
            self.assertTrue(record["stop_hook_active"])
            self.assertEqual("Stop", record["hook_event_name"])

    def test_claude_hook_command_never_blocks_or_prints_on_invalid_input(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(agentwatch.sys, "stdin", io.StringIO("{")), mock.patch(
            "sys.stdout", output
        ), mock.patch("sys.stderr", errors):
            result = agentwatch.main(["claude-hook"])

        self.assertEqual(0, result)
        self.assertEqual("", output.getvalue())
        self.assertEqual("", errors.getvalue())

    def test_hook_spool_symlink_is_silently_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "important.txt"
            target.write_text("keep intact\n", encoding="utf-8")
            spool = root / "events.jsonl"
            spool.symlink_to(target)
            payload = {
                "session_id": "session-123",
                "prompt_id": "prompt-123",
                "transcript_path": str(root / "session.jsonl"),
                "cwd": "/tmp/project",
                "hook_event_name": "Stop",
                "stop_hook_active": False,
                "last_assistant_message": "done",
                "background_tasks": [],
                "session_crons": [],
            }
            output = io.StringIO()
            errors = io.StringIO()

            with mock.patch.object(
                agentwatch.sys, "stdin", io.StringIO(json.dumps(payload))
            ), mock.patch("sys.stdout", output), mock.patch("sys.stderr", errors):
                result = agentwatch.main(
                    ["claude-hook", "--events-file", str(spool)]
                )

            self.assertEqual(0, result)
            self.assertEqual("keep intact\n", target.read_text(encoding="utf-8"))
            self.assertEqual("", output.getvalue())
            self.assertEqual("", errors.getvalue())


class ClaudeHookInstallerIntegrationTests(unittest.TestCase):
    def test_install_update_and_uninstall_preserve_other_claude_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            claude_settings = paths.home / ".claude" / "settings.json"
            claude_settings.parent.mkdir(parents=True)
            existing = {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "rtk rewrite"}],
                        }
                    ]
                },
                "keep": {"future": True},
            }
            claude_settings.write_text(json.dumps(existing), encoding="utf-8")
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "active"
            store = mock.Mock()
            store.backend_name.return_value = "test"

            with mock.patch.dict(
                os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True
            ), mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(agentwatch, "ComputerTokenStore", return_value=store), mock.patch(
                "sys.stdout", new_callable=io.StringIO
            ):
                installed = agentwatch.main(
                    ["install", "--delivery", "bark", "--json", "--no-login"]
                )
                updated = agentwatch.main(["update", "--json"])

            self.assertEqual(0, installed)
            self.assertEqual(0, updated)
            configured = json.loads(claude_settings.read_text(encoding="utf-8"))
            self.assertEqual(existing["hooks"]["PreToolUse"], configured["hooks"]["PreToolUse"])
            self.assertEqual({"future": True}, configured["keep"])
            for event_name in ("Stop", "StopFailure"):
                self.assertEqual(1, len(configured["hooks"][event_name]))
                self.assertEqual(1, len(configured["hooks"][event_name][0]["hooks"]))

            with mock.patch.dict(
                os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True
            ), mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(agentwatch, "ComputerTokenStore", return_value=store), mock.patch(
                "sys.stdout", new_callable=io.StringIO
            ):
                uninstalled = agentwatch.main(["uninstall", "--json"])

            self.assertEqual(0, uninstalled)
            remaining = json.loads(claude_settings.read_text(encoding="utf-8"))
            self.assertEqual(existing, remaining)
            service.uninstall.assert_called_once_with()

    def test_claude_hook_command_accepts_explicit_installer_spool_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transcript = root / "session.jsonl"
            transcript.write_text("turn\n", encoding="utf-8")
            spool = root / "custom-config" / "claude-hook-events.jsonl"
            payload = {
                "session_id": "session-123",
                "prompt_id": "prompt-123",
                "transcript_path": str(transcript),
                "cwd": "/tmp/project",
                "hook_event_name": "Stop",
                "stop_hook_active": False,
                "last_assistant_message": "done",
                "background_tasks": [],
                "session_crons": [],
            }

            handler = agentwatch.build_claude_hook_handler(
                agentwatch.sys.executable,
                root / "runtime" / "agentwatch.py",
                spool,
            )
            # The first argument is the script path consumed by Python itself;
            # pass the exact remaining generated args through the real parser.
            with mock.patch.object(agentwatch.sys, "stdin", io.StringIO(json.dumps(payload))):
                result = agentwatch.main(handler["args"][1:])

            self.assertEqual(0, result)
            self.assertEqual("prompt-123", json.loads(spool.read_text(encoding="utf-8"))["prompt_id"])

    def test_registration_migrates_custom_claude_scope_without_duplicate_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            custom_config = root / "custom-claude"
            custom_settings = custom_config / "settings.json"
            default_settings = paths.home / ".claude" / "settings.json"

            with mock.patch.dict(
                os.environ,
                {
                    "AGENTWATCH_CONFIG_DIR": str(paths.config),
                    "CLAUDE_CONFIG_DIR": str(custom_config),
                },
                clear=True,
            ):
                agentwatch._configure_installed_claude_hooks(paths)

            registration = paths.config / agentwatch.CLAUDE_HOOK_REGISTRATION_FILE_NAME
            self.assertEqual(0o600, stat.S_IMODE(registration.stat().st_mode))
            self.assertEqual(
                str(custom_settings),
                json.loads(registration.read_text(encoding="utf-8"))["settings_path"],
            )

            with mock.patch.dict(
                os.environ,
                {"AGENTWATCH_CONFIG_DIR": str(paths.config)},
                clear=True,
            ):
                before = agentwatch._installed_claude_hook_status(paths)
                self.assertTrue(before["needs_reconcile"])
                self.assertFalse(before["active"])
                agentwatch._configure_installed_claude_hooks(paths)
                after = agentwatch._installed_claude_hook_status(paths)

            old_payload = json.loads(custom_settings.read_text(encoding="utf-8"))
            self.assertNotIn("hooks", old_payload)
            new_payload = json.loads(default_settings.read_text(encoding="utf-8"))
            for event_name in ("Stop", "StopFailure"):
                handlers = [
                    handler
                    for group in new_payload["hooks"][event_name]
                    for handler in group["hooks"]
                ]
                self.assertEqual(1, len(handlers))
            self.assertFalse(after["needs_reconcile"])
            self.assertEqual(
                str(default_settings),
                json.loads(registration.read_text(encoding="utf-8"))["settings_path"],
            )

    def test_registration_symlink_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.config.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text('{"keep":true}', encoding="utf-8")
            registration = paths.config / agentwatch.CLAUDE_HOOK_REGISTRATION_FILE_NAME
            registration.symlink_to(outside)

            with self.assertRaises(agentwatch_core.AgentWatchError):
                agentwatch._preflight_installed_claude_hooks(paths)

            self.assertEqual({"keep": True}, json.loads(outside.read_text(encoding="utf-8")))

    def test_status_requires_claude_version_with_full_stop_payload_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            agentwatch._configure_installed_claude_hooks(paths)

            old_version = mock.Mock(
                returncode=0,
                stdout="2.1.195 (Claude Code)\n",
                stderr="",
            )
            supported_version = mock.Mock(
                returncode=0,
                stdout="2.1.196 (Claude Code)\n",
                stderr="",
            )
            with mock.patch.object(
                agentwatch.shutil, "which", return_value="/usr/local/bin/claude"
            ), mock.patch.object(agentwatch, "_run", return_value=old_version):
                old_status = agentwatch._installed_claude_hook_status(paths)
            with mock.patch.object(
                agentwatch.shutil, "which", return_value="/usr/local/bin/claude"
            ), mock.patch.object(agentwatch, "_run", return_value=supported_version):
                supported_status = agentwatch._installed_claude_hook_status(paths)

            self.assertTrue(old_status["configured"])
            self.assertEqual("2.1.195", old_status["cli_version"])
            self.assertFalse(old_status["cli_compatible"])
            self.assertFalse(old_status["active"])
            self.assertEqual("2.1.196", supported_status["minimum_cli_version"])
            self.assertTrue(supported_status["cli_compatible"])
            self.assertTrue(supported_status["active"])

    def test_malformed_claude_settings_uninstall_removes_service_but_keeps_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.runtime.mkdir(parents=True)
            paths.launcher.parent.mkdir(parents=True)
            paths.launcher.write_text("launcher", encoding="utf-8")
            runtime_script = paths.runtime / "agentwatch.py"
            runtime_script.write_text("# runtime\n", encoding="utf-8")
            settings = paths.home / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{invalid", encoding="utf-8")
            service = mock.Mock()
            output = io.StringIO()

            with mock.patch.dict(
                os.environ,
                {"AGENTWATCH_CONFIG_DIR": str(paths.config)},
                clear=True,
            ), mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["uninstall", "--json"])

            self.assertEqual(1, result)
            service.uninstall.assert_called_once_with()
            self.assertTrue(runtime_script.exists())
            self.assertTrue(paths.launcher.exists())
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["partial"])
            self.assertTrue(payload["runtime_preserved"])
            self.assertEqual("claude_hook_cleanup_failed", payload["error"])


class PrivateNotifierTests(unittest.TestCase):
    def test_private_session_ignores_legacy_ntfy_and_publishes_exact_event(self) -> None:
        machine = {
            "computer_id": "11111111-1111-4111-8111-111111111111",
            "computer_name": "test",
            "platform": "macos",
        }
        token_store = mock.Mock()
        token_store.load.return_value = "private-token"
        api = mock.Mock()
        api.publish.return_value = {"ok": True}
        event = {
            "event_type": "kimi_turn_completed",
            "stable_id": "stable-event",
            "ntfy_url": "https://legacy.invalid/shared-topic",
            "ntfy_tags": "target_someone_else",
        }
        environment = {"NTFY_URL": "https://legacy.invalid/shared-topic", "NTFY_TOKEN": "legacy-token"}
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            notifier, "load_or_create_machine", return_value=machine
        ), mock.patch.object(notifier, "ComputerTokenStore", return_value=token_store), mock.patch.object(
            notifier, "AgentWatchApi", return_value=api
        ):
            delivery = notifier.Notifier(False, notifier.Logger(None))
            sent = delivery.send("Kimi 已完成", "正文", event)

        self.assertTrue(sent)
        self.assertIn("agentwatch", delivery.channels)
        self.assertNotIn("ntfy", delivery.channels)
        api.publish.assert_called_once_with(
            "private-token",
            event_id="aw2_11111111111141118111111111111111_stable-event",
            source="kimi",
            title="Kimi 已完成",
            body="正文",
            priority="default",
        )

    def test_retry_reuses_private_event_id(self) -> None:
        event = {"event_type": "codex_task_complete", "stable_id": "same-event"}
        computer_id = "11111111-1111-4111-8111-111111111111"
        self.assertEqual(
            agentwatch_core.stable_event_id(event, computer_id),
            agentwatch_core.stable_event_id(event, computer_id),
        )
        generated = agentwatch_core.stable_event_id(event, computer_id)
        self.assertLessEqual(len(generated), 64)
        self.assertRegex(generated, re.compile(r"[-_A-Za-z0-9]{1,64}\Z"))

    def test_private_event_id_preserves_short_ntfy_compatible_stable_id(self) -> None:
        computer_id = "11111111-1111-4111-8111-111111111111"
        generated = agentwatch_core.stable_event_id(
            {"event_type": "zcode_turn_completed", "stable_id": "AbC-123_test"},
            computer_id,
        )

        self.assertEqual(
            "aw2_11111111111141118111111111111111_abc-123_test",
            generated,
        )

    def test_private_event_id_hashes_missing_or_ntfy_incompatible_stable_id(self) -> None:
        computer_id = "11111111-1111-4111-8111-111111111111"
        events = [
            {"event_type": "codex_test", "timestamp": "2026-08-02T15:36:20Z"},
            {"event_type": "zcode_test", "timestamp": "2026-08-02T15:36:21Z"},
            {"event_type": "kimi_test", "timestamp": "2026-08-02T15:36:22Z"},
            {"event_type": "grok_test", "timestamp": "2026-08-02T15:36:23Z"},
            {"event_type": "codex_task_complete", "stable_id": "event.with.dot"},
            {"event_type": "codex_task_complete", "stable_id": "event:with:colon"},
            {"event_type": "codex_task_complete", "stable_id": "x" * 200},
        ]

        generated = [agentwatch_core.stable_event_id(event, computer_id) for event in events]
        self.assertEqual(len(generated), len(set(generated)))
        for event_id in generated:
            self.assertLessEqual(len(event_id), 64)
            self.assertRegex(event_id, re.compile(r"[-_A-Za-z0-9]{1,64}\Z"))
            self.assertEqual(61, len(event_id))

    def test_local_macos_success_cannot_mask_private_publish_failure(self) -> None:
        machine = {
            "computer_id": "11111111-1111-4111-8111-111111111111",
            "computer_name": "test",
            "platform": "macos",
        }
        token_store = mock.Mock()
        token_store.load.return_value = "private-token"
        with mock.patch.object(notifier, "load_or_create_machine", return_value=machine), mock.patch.object(
            notifier, "ComputerTokenStore", return_value=token_store
        ):
            delivery = notifier.Notifier(False, notifier.Logger(None))
        delivery.channels = ["agentwatch", "macos"]
        with mock.patch.object(delivery, "_send_agentwatch", return_value=False), mock.patch.object(
            delivery, "_send_macos", return_value=True
        ):
            sent = delivery.send("title", "body", {"event_type": "codex_task_complete"})

        self.assertFalse(sent)
        self.assertEqual({"macos"}, delivery.last_successful_channels)

    def test_retry_sends_only_channels_that_failed_first_round(self) -> None:
        machine = {
            "computer_id": "11111111-1111-4111-8111-111111111111",
            "computer_name": "test",
            "platform": "macos",
        }
        token_store = mock.Mock()
        token_store.load.return_value = "private-token"
        with mock.patch.object(notifier, "load_or_create_machine", return_value=machine), mock.patch.object(
            notifier, "ComputerTokenStore", return_value=token_store
        ):
            delivery = notifier.Notifier(False, notifier.Logger(None))
        delivery.channels = ["bark", "agentwatch", "macos"]
        state = {"sent": {}, "delivery_attempts": {}, "delivery_stats": {}}
        record = {"offset": 0}
        event = {
            "event_type": "codex_task_complete",
            "notification_title": "title",
            "notification_body": "body",
        }
        with mock.patch.object(delivery, "_send_bark", return_value=True) as bark, mock.patch.object(
            delivery, "_send_agentwatch", side_effect=[False, True]
        ) as private_publish, mock.patch.object(delivery, "_send_macos", return_value=True) as local:
            first = notifier.deliver_event_with_bounded_retry(
                state=state,
                rec=record,
                notifier=delivery,
                log=notifier.Logger(None),
                event=event,
                stable_id="stable-event",
                source="Codex",
                path=Path("/tmp/test-rollout.jsonl"),
                line_offset=0,
                line_end=10,
            )
            state["delivery_attempts"]["stable-event"]["next_retry_at"] = 0
            second = notifier.deliver_event_with_bounded_retry(
                state=state,
                rec=record,
                notifier=delivery,
                log=notifier.Logger(None),
                event=event,
                stable_id="stable-event",
                source="Codex",
                path=Path("/tmp/test-rollout.jsonl"),
                line_offset=0,
                line_end=10,
            )

        self.assertEqual("retry_scheduled", first)
        self.assertEqual("sent", second)
        self.assertEqual(1, bark.call_count)
        self.assertEqual(1, local.call_count)
        self.assertEqual(2, private_publish.call_count)


class CliSafetyTests(unittest.TestCase):
    def test_password_command_line_option_is_rejected(self) -> None:
        parser = agentwatch.build_parser()
        with mock.patch("sys.stderr") as stderr, self.assertRaises(SystemExit):
            parser.parse_args(["login", "--password", "must-not-be-accepted"])
        rendered = "".join(str(call.args[0]) for call in stderr.write.call_args_list)
        self.assertNotIn("must-not-be-accepted", rendered)

    def test_login_rejects_noninteractive_password_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            service = mock.Mock()
            with mock.patch.object(agentwatch.sys.stdin, "isatty", return_value=False), mock.patch.object(
                agentwatch.getpass, "getpass"
            ) as getpass_prompt, self.assertRaises(agentwatch_core.AgentWatchError):
                agentwatch._login("alice", paths, service)
            getpass_prompt.assert_not_called()

    def test_login_keyboard_interrupt_after_server_token_revokes_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            service = mock.Mock()
            store = mock.Mock()
            store.save.side_effect = KeyboardInterrupt()
            api = mock.Mock()
            api.login.return_value = {"computer_token": "fresh-token", "username": "alice"}
            api.logout.return_value = {"ok": True}
            with mock.patch.object(agentwatch.sys.stdin, "isatty", return_value=True), mock.patch.object(
                agentwatch.getpass, "getpass", return_value="account-password"
            ), mock.patch.object(agentwatch, "AgentWatchApi", return_value=api), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), self.assertRaises(KeyboardInterrupt):
                agentwatch._login("alice", paths, service)

            api.logout.assert_called_once_with("fresh-token")
            store.delete.assert_called_once_with()
            service.stop.assert_called_once_with()

    def test_login_cleanup_preserves_local_token_when_server_revoke_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            service = mock.Mock()
            store = mock.Mock()
            service.start.side_effect = agentwatch_core.AgentWatchError("service failed")
            api = mock.Mock()
            api.login.return_value = {"computer_token": "fresh-token", "username": "alice"}
            api.logout.side_effect = agentwatch_core.AgentWatchError("network unavailable")
            with mock.patch.object(agentwatch.sys.stdin, "isatty", return_value=True), mock.patch.object(
                agentwatch.getpass, "getpass", return_value="account-password"
            ), mock.patch.object(agentwatch, "AgentWatchApi", return_value=api), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), self.assertRaises(agentwatch_core.AgentWatchError):
                agentwatch._login("alice", paths, service)

            api.logout.assert_called_once_with("fresh-token")
            store.delete.assert_not_called()
            service.stop.assert_called_once_with()

    def test_json_option_works_before_or_after_command(self) -> None:
        parser = agentwatch.build_parser()
        self.assertTrue(parser.parse_args(["--json", "status"]).json)
        self.assertTrue(parser.parse_args(["status", "--json"]).json)

    def test_install_runtime_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            source = Path(agentwatch.__file__).resolve().parent

            agentwatch.install_runtime(paths, source)
            first = {
                name: (paths.runtime / name).read_bytes()
                for name in agentwatch.RUNTIME_FILES
            }
            agentwatch.install_runtime(paths, source)
            second = {
                name: (paths.runtime / name).read_bytes()
                for name in agentwatch.RUNTIME_FILES
            }

            self.assertEqual(first, second)
            self.assertTrue(paths.launcher.exists())

    def test_install_refuses_runtime_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            source = Path(agentwatch.__file__).resolve().parent
            paths.runtime.mkdir(parents=True)
            outside = root / "outside.py"
            outside.write_text("do not overwrite", encoding="utf-8")
            (paths.runtime / "agentwatch.py").symlink_to(outside)

            with self.assertRaises(agentwatch_core.AgentWatchError):
                agentwatch.install_runtime(paths, source)

            self.assertEqual("do not overwrite", outside.read_text(encoding="utf-8"))

    def test_unauthenticated_linux_install_keeps_service_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.runtime.mkdir(parents=True)
            completed = mock.Mock(returncode=0, stdout="", stderr="")

            def run_command(command):
                if command[:3] == ["systemctl", "--user", "show"]:
                    if paths.linux_unit.exists():
                        return mock.Mock(
                            returncode=0,
                            stdout=(
                                "LoadState=loaded\nActiveState=inactive\n"
                                "UnitFileState=disabled\n"
                            ),
                            stderr="",
                        )
                    return mock.Mock(
                        returncode=0,
                        stdout=(
                            "LoadState=not-found\nActiveState=inactive\n"
                            "UnitFileState=\n"
                        ),
                        stderr="",
                    )
                return completed

            with mock.patch.object(agentwatch, "_run", side_effect=run_command) as run:
                agentwatch.ServiceManager(paths, system_name="Linux").install(authenticated=False)

            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(["systemctl", "--user", "disable", agentwatch.LINUX_UNIT], commands)
            self.assertNotIn(["systemctl", "--user", "enable", "--now", agentwatch.LINUX_UNIT], commands)

    def test_windows_task_is_hidden_logged_restartable_and_disabled_before_login(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with mock.patch.object(agentwatch.platform, "system", return_value="Windows"):
                paths = agentwatch.InstallPaths(root / "config", root / "home")
                agentwatch.install_runtime(paths, Path(agentwatch.__file__).resolve().parent)
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            registered = False

            def run_command(command):
                nonlocal registered
                if command[0] == "powershell.exe" and "Get-ScheduledTask" in command[-1]:
                    return mock.Mock(
                        returncode=0,
                        stdout=(
                            "agentwatch:present:disabled:false\n"
                            if registered
                            else "agentwatch:absent\n"
                        ),
                        stderr="",
                    )
                if command[0] == "powershell.exe" and "Register-ScheduledTask" in command[-1]:
                    registered = True
                return completed

            with mock.patch.object(agentwatch, "_run", side_effect=run_command) as run:
                agentwatch.ServiceManager(paths, system_name="Windows").install(authenticated=False)

            commands = [call.args[0] for call in run.call_args_list]
            register = next(
                command
                for command in commands
                if command[0] == "powershell.exe" and "Register-ScheduledTask" in command[-1]
            )
            registration_script = register[-1]
            self.assertIn("-WindowStyle Hidden", registration_script)
            self.assertIn("-RestartCount 999", registration_script)
            self.assertIn(["schtasks.exe", "/Change", "/TN", agentwatch.WINDOWS_TASK, "/Disable"], commands)
            wrapper = (paths.runtime / "run_notifier.ps1").read_text(encoding="utf-8")
            self.assertIn("task.out.log", wrapper)
            self.assertIn("task.err.log", wrapper)

    def test_disabled_install_rejects_unconfirmed_stop_on_every_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            success = mock.Mock(returncode=0, stdout="", stderr="")
            cases = {
                "Darwin": lambda command: (
                    mock.Mock(returncode=0, stdout="state = running\n", stderr="")
                    if command[:2] == ["launchctl", "print"]
                    else success
                ),
                "Linux": lambda command: (
                    mock.Mock(
                        returncode=0,
                        stdout=(
                            "LoadState=loaded\nActiveState=active\n"
                            "UnitFileState=enabled\n"
                        ),
                        stderr="",
                    )
                    if command[:3] == ["systemctl", "--user", "show"]
                    else success
                ),
                "Windows": lambda command: (
                    mock.Mock(
                        returncode=0,
                        stdout="agentwatch:present:running:true\n",
                        stderr="",
                    )
                    if command[0] == "powershell.exe"
                    else success
                ),
            }
            for system_name, run_command in cases.items():
                with self.subTest(system_name=system_name):
                    paths = agentwatch.InstallPaths(
                        root / f"config-{system_name}",
                        root / f"home-{system_name}",
                    )
                    paths.runtime.mkdir(parents=True)
                    with mock.patch.object(
                        agentwatch, "_run", side_effect=run_command
                    ), mock.patch.object(
                        agentwatch, "SERVICE_STATE_TIMEOUT_SECONDS", 0
                    ), self.assertRaises(agentwatch_core.AgentWatchError):
                        agentwatch.ServiceManager(
                            paths, system_name=system_name
                        ).install(should_start=False)

    def test_start_requires_enabled_and_running_on_every_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            success = mock.Mock(returncode=0, stdout="", stderr="")
            cases = {
                "Darwin": lambda command: (
                    mock.Mock(returncode=0, stdout="state = running\n", stderr="")
                    if command[:2] == ["launchctl", "print"]
                    else mock.Mock(
                        returncode=0,
                        stdout=(
                            'disabled services = {\n'
                            f'  "{agentwatch.MACOS_LABEL}" => true\n'
                            '}\n'
                        ),
                        stderr="",
                    )
                    if command[:2] == ["launchctl", "print-disabled"]
                    else success
                ),
                "Linux": lambda command: (
                    mock.Mock(
                        returncode=0,
                        stdout=(
                            "LoadState=loaded\nActiveState=active\n"
                            "UnitFileState=disabled\n"
                        ),
                        stderr="",
                    )
                    if command[:3] == ["systemctl", "--user", "show"]
                    else success
                ),
                "Windows": lambda command: (
                    mock.Mock(
                        returncode=0,
                        stdout="agentwatch:present:running:false\n",
                        stderr="",
                    )
                    if command[0] == "powershell.exe"
                    else success
                ),
            }
            for system_name, run_command in cases.items():
                with self.subTest(system_name=system_name):
                    paths = agentwatch.InstallPaths(
                        root / f"config-{system_name}",
                        root / f"home-{system_name}",
                    )
                    with mock.patch.object(
                        agentwatch, "_run", side_effect=run_command
                    ), mock.patch.object(
                        agentwatch, "SERVICE_STATE_TIMEOUT_SECONDS", 0
                    ), self.assertRaises(agentwatch_core.AgentWatchError):
                        agentwatch.ServiceManager(
                            paths, system_name=system_name
                        ).start()

    def test_start_bounded_poll_allows_asynchronous_windows_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            success = mock.Mock(returncode=0, stdout="", stderr="")
            snapshots = iter(
                [
                    "agentwatch:present:ready:true\n",
                    "agentwatch:present:running:true\n",
                ]
            )

            def run_command(command):
                if command[0] == "powershell.exe":
                    return mock.Mock(
                        returncode=0,
                        stdout=next(snapshots),
                        stderr="",
                    )
                return success

            with mock.patch.object(
                agentwatch, "_run", side_effect=run_command
            ), mock.patch.object(
                agentwatch.time, "sleep"
            ) as sleep:
                agentwatch.ServiceManager(paths, system_name="Windows").start()

            sleep.assert_called_once_with(agentwatch.SERVICE_STATE_POLL_SECONDS)

    def _assert_uninstall_service_failure_preserves_runtime(
        self,
        *,
        system_name: str,
        run_side_effect,
        prepare_service_definition=None,
    ) -> tuple[dict, list[list[str]]]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.runtime.mkdir(parents=True)
            paths.launcher.parent.mkdir(parents=True)
            runtime_script = paths.runtime / "agentwatch.py"
            runtime_script.write_text("# must remain\n", encoding="utf-8")
            paths.launcher.write_text("launcher\n", encoding="utf-8")
            if prepare_service_definition is not None:
                prepare_service_definition(paths)
            service = agentwatch.ServiceManager(paths, system_name=system_name)
            output = io.StringIO()
            with mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch, "_configure_installed_claude_hooks", return_value={}
            ), mock.patch.object(
                agentwatch, "_run", side_effect=run_side_effect
            ) as run, mock.patch.object(
                agentwatch, "SERVICE_STATE_TIMEOUT_SECONDS", 0
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["uninstall", "--json"])

            self.assertEqual(1, result)
            self.assertTrue(runtime_script.exists())
            self.assertTrue(paths.launcher.exists())
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["partial"])
            self.assertEqual("service_cleanup_failed", payload["error"])
            self.assertFalse(payload["service_removed"])
            self.assertTrue(payload["runtime_preserved"])
            return payload, [call.args[0] for call in run.call_args_list]

    def test_macos_uninstall_stop_failure_with_loaded_agent_preserves_runtime(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="operation failed")
        loaded = mock.Mock(returncode=0, stdout="state = running\n", stderr="")

        def run(command):
            if command[:2] == ["launchctl", "print"]:
                return loaded
            return failed

        def prepare(paths):
            paths.macos_plist.parent.mkdir(parents=True)
            paths.macos_plist.write_text("plist", encoding="utf-8")

        _payload, commands = self._assert_uninstall_service_failure_preserves_runtime(
            system_name="Darwin",
            run_side_effect=run,
            prepare_service_definition=prepare,
        )
        self.assertTrue(any(command[:2] == ["launchctl", "bootout"] for command in commands))
        self.assertTrue(any(command[:2] == ["launchctl", "print"] for command in commands))

    def test_linux_uninstall_stop_failure_with_active_unit_preserves_runtime(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="operation failed")
        active = mock.Mock(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n",
            stderr="",
        )

        def run(command):
            if command[:3] == ["systemctl", "--user", "show"]:
                return active
            return failed

        def prepare(paths):
            paths.linux_unit.parent.mkdir(parents=True)
            paths.linux_unit.write_text("[Service]\n", encoding="utf-8")

        _payload, commands = self._assert_uninstall_service_failure_preserves_runtime(
            system_name="Linux",
            run_side_effect=run,
            prepare_service_definition=prepare,
        )
        self.assertIn(
            ["systemctl", "--user", "stop", agentwatch.LINUX_UNIT], commands
        )
        self.assertTrue(any(command[:3] == ["systemctl", "--user", "show"] for command in commands))

    def test_windows_uninstall_delete_failure_with_present_task_preserves_runtime(self) -> None:
        success = mock.Mock(returncode=0, stdout="", stderr="")
        delete_failed = mock.Mock(returncode=1, stdout="", stderr="access denied")
        present = mock.Mock(
            returncode=0,
            stdout="agentwatch:present:ready:false\n",
            stderr="",
        )

        def run(command):
            if command[0] == "powershell.exe":
                return present
            if command[:2] == ["schtasks.exe", "/Delete"]:
                return delete_failed
            return success

        _payload, commands = self._assert_uninstall_service_failure_preserves_runtime(
            system_name="Windows",
            run_side_effect=run,
        )
        self.assertIn(
            ["schtasks.exe", "/Delete", "/TN", agentwatch.WINDOWS_TASK, "/F"],
            commands,
        )
        self.assertGreaterEqual(
            sum(command[0] == "powershell.exe" for command in commands), 2
        )

    def test_uninstall_combines_service_and_claude_hook_cleanup_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.runtime.mkdir(parents=True)
            paths.launcher.parent.mkdir(parents=True)
            runtime_script = paths.runtime / "agentwatch.py"
            runtime_script.write_text("# must remain\n", encoding="utf-8")
            paths.launcher.write_text("launcher\n", encoding="utf-8")
            service = mock.Mock()
            service.uninstall.side_effect = agentwatch_core.AgentWatchError("service remains")
            output = io.StringIO()
            with mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch,
                "_configure_installed_claude_hooks",
                side_effect=agentwatch_core.AgentWatchError("settings locked"),
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["uninstall", "--json"])

            self.assertEqual(1, result)
            self.assertTrue(runtime_script.exists())
            self.assertTrue(paths.launcher.exists())
            payload = json.loads(output.getvalue())
            self.assertEqual("service_cleanup_failed", payload["error"])
            self.assertTrue(payload["claude_hook_cleanup_failed"])
            self.assertEqual(
                {"service", "claude_hook"}, set(payload["cleanup_errors"])
            )

    def test_logout_network_failure_keeps_local_token_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            store = mock.Mock()
            store.load_strict.return_value = "still-valid-token"
            service = mock.Mock()
            api = mock.Mock()
            api.logout.side_effect = agentwatch_core.AgentWatchError("network unavailable")
            output = io.StringIO()
            with mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(agentwatch, "ComputerTokenStore", return_value=store), mock.patch.object(
                agentwatch, "AgentWatchApi", return_value=api
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["logout", "--json"])

            self.assertEqual(1, result)
            store.delete.assert_not_called()
            service.stop.assert_not_called()
            self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_logout_credential_backend_outage_does_not_claim_server_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            store = mock.Mock()
            store.load_strict.side_effect = agentwatch_core.AgentWatchError(
                "credential backend unavailable"
            )
            service = mock.Mock()
            api = mock.Mock()
            output = io.StringIO()
            with mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(
                agentwatch, "AgentWatchApi", return_value=api
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["logout", "--json"])

            self.assertEqual(1, result)
            store.load_strict.assert_called_once_with()
            store.load.assert_not_called()
            api.logout.assert_not_called()
            store.delete.assert_not_called()
            service.start.assert_not_called()
            service.stop.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["ok"])
            self.assertFalse(payload.get("server_revoked", False))

    def test_logout_local_secret_clear_failure_is_partial_and_preserves_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.runtime.mkdir(parents=True)
            runtime_script = paths.runtime / "agentwatch.py"
            runtime_script.write_text("# must remain\n", encoding="utf-8")
            store = mock.Mock()
            store.load_strict.return_value = "computer-token"
            store.delete.side_effect = agentwatch_core.AgentWatchError(
                "credential clear failed"
            )
            service = mock.Mock()
            api = mock.Mock()
            api.logout.return_value = {"ok": True}
            output = io.StringIO()
            with mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(
                agentwatch, "AgentWatchApi", return_value=api
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["logout", "--json"])

            self.assertEqual(1, result)
            api.logout.assert_called_once_with("computer-token")
            store.delete.assert_called_once_with()
            self.assertTrue(runtime_script.exists())
            service.start.assert_not_called()
            service.stop.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["partial"])
            self.assertTrue(payload["server_revoked"])
            self.assertFalse(payload["local_token_deleted"])
            self.assertTrue(payload["runtime_preserved"])
            self.assertEqual("local_token_cleanup_failed", payload["error"])

    def test_logout_401_is_already_revoked_and_deletes_local_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            store = mock.Mock()
            store.load_strict.return_value = "revoked-token"
            service = mock.Mock()
            api = mock.Mock()
            api.logout.side_effect = agentwatch_core.ApiError(401, "unauthorized", "revoked")
            output = io.StringIO()
            with mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(agentwatch, "ComputerTokenStore", return_value=store), mock.patch.object(
                agentwatch, "AgentWatchApi", return_value=api
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["logout", "--json"])

            self.assertEqual(0, result)
            store.delete.assert_called_once_with()
            service.stop.assert_called_once_with()
            self.assertTrue(json.loads(output.getvalue())["server_revoked"])

    def test_logout_without_local_token_does_not_claim_server_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            store = mock.Mock()
            store.load_strict.return_value = None
            service = mock.Mock()
            api = mock.Mock()
            output = io.StringIO()
            with mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(
                agentwatch, "AgentWatchApi", return_value=api
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["logout", "--json"])

            self.assertEqual(0, result)
            api.logout.assert_not_called()
            store.delete.assert_called_once_with()
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["server_revoke_required"])
            self.assertFalse(payload["server_revoked"])


class DeliveryModeTests(unittest.TestCase):
    def test_settings_round_trip_is_atomic_private_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agentwatch_core.save_delivery_mode("both", root)

            settings = root / "settings.json"
            self.assertEqual("both", agentwatch_core.load_delivery_mode(root))
            self.assertEqual(0o600, stat.S_IMODE(settings.stat().st_mode))
            self.assertEqual(
                {"version": 1, "delivery_mode": "both"},
                json.loads(settings.read_text(encoding="utf-8")),
            )
            self.assertEqual([], list(root.glob(".settings.json.*")))
            with self.assertRaises(agentwatch_core.AgentWatchError):
                agentwatch_core.save_delivery_mode("ntfy", root)

    def test_resolver_reports_operational_degraded_both(self) -> None:
        state = agentwatch_core.resolve_delivery("both", True, False)

        self.assertEqual(["bark"], state["effective_channels"])
        self.assertEqual(["agentwatch"], state["missing_channels"])
        self.assertTrue(state["operational"])
        self.assertFalse(state["fully_configured"])
        self.assertTrue(state["degraded"])

    def test_bark_only_notifier_never_reads_agentwatch_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agentwatch_core.save_delivery_mode("bark", root)
            environment = {
                "AGENTWATCH_CONFIG_DIR": str(root),
                "BARK_URL": "https://example.invalid/bark",
                "CODEX_WATCH_MACOS_NOTIFICATION": "0",
            }
            machine = {"computer_id": "computer-1", "computer_name": "test", "platform": "macos"}
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                notifier, "load_or_create_machine", return_value=machine
            ), mock.patch.object(notifier, "ComputerTokenStore") as token_store:
                delivery = notifier.Notifier(False, notifier.Logger(None))

            token_store.assert_not_called()
            self.assertEqual(["bark"], delivery.channels)
            self.assertTrue(delivery.delivery["fully_configured"])

    def test_both_without_android_login_runs_bark_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agentwatch_core.save_delivery_mode("both", root)
            environment = {
                "AGENTWATCH_CONFIG_DIR": str(root),
                "BARK_URL": "https://example.invalid/bark",
                "CODEX_WATCH_MACOS_NOTIFICATION": "0",
            }
            machine = {"computer_id": "computer-1", "computer_name": "test", "platform": "macos"}
            store = mock.Mock()
            store.load.return_value = None
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                notifier, "load_or_create_machine", return_value=machine
            ), mock.patch.object(notifier, "ComputerTokenStore", return_value=store):
                delivery = notifier.Notifier(False, notifier.Logger(None))
                with mock.patch.object(delivery, "_send_bark", return_value=True) as bark_send:
                    sent = delivery.send("title", "body", {"event_type": "codex_task_complete"})

            self.assertTrue(sent)
            self.assertEqual(["bark"], delivery.channels)
            self.assertTrue(delivery.delivery["degraded"])
            bark_send.assert_called_once()

    def test_agentwatch_only_ignores_stale_bark_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agentwatch_core.save_delivery_mode("agentwatch", root)
            environment = {
                "AGENTWATCH_CONFIG_DIR": str(root),
                "BARK_URL": "https://example.invalid/stale-bark",
                "CODEX_WATCH_MACOS_NOTIFICATION": "0",
            }
            machine = {"computer_id": "computer-1", "computer_name": "test", "platform": "macos"}
            store = mock.Mock()
            store.load.return_value = "private-token"
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                notifier, "load_or_create_machine", return_value=machine
            ), mock.patch.object(notifier, "ComputerTokenStore", return_value=store):
                delivery = notifier.Notifier(False, notifier.Logger(None))

            self.assertEqual(["agentwatch"], delivery.channels)
            self.assertNotIn("bark", delivery.channels)

    def test_revoked_agentwatch_in_both_finishes_via_bark_without_retry_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agentwatch_core.save_delivery_mode("both", root)
            environment = {
                "AGENTWATCH_CONFIG_DIR": str(root),
                "BARK_URL": "https://example.invalid/bark",
                "CODEX_WATCH_MACOS_NOTIFICATION": "0",
            }
            machine = {"computer_id": "computer-1", "computer_name": "test", "platform": "macos"}
            store = mock.Mock()
            store.load.return_value = "revoked-token"
            api = mock.Mock()
            api.publish.side_effect = agentwatch_core.ApiError(401, "unauthorized", "revoked")
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                notifier, "load_or_create_machine", return_value=machine
            ), mock.patch.object(notifier, "ComputerTokenStore", return_value=store), mock.patch.object(
                notifier, "AgentWatchApi", return_value=api
            ):
                delivery = notifier.Notifier(False, notifier.Logger(None))
                with mock.patch.object(delivery, "_send_bark", return_value=True) as bark_send:
                    first = delivery.send("title", "body", {"event_type": "codex_task_complete"})
                    second = delivery.send("title 2", "body 2", {"event_type": "codex_task_complete"})

            self.assertTrue(first)
            self.assertTrue(second)
            self.assertIn("agentwatch", delivery.disabled_channels)
            self.assertEqual(1, api.publish.call_count)
            self.assertEqual(2, bark_send.call_count)
            store.delete.assert_called_once_with()

    def test_headless_install_without_inferable_mode_requires_explicit_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            service = mock.Mock()
            service.installed.return_value = False
            service.state.return_value = "stopped"
            store = mock.Mock()
            store.load.return_value = None
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True), mock.patch.object(
                agentwatch, "InstallPaths", return_value=paths
            ), mock.patch.object(agentwatch, "ServiceManager", return_value=service), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["install", "--json"])

            self.assertEqual(1, result)
            self.assertEqual("delivery_mode_required", json.loads(output.getvalue())["error"])
            service.install.assert_not_called()
            self.assertFalse(paths.runtime.exists())
            self.assertFalse((paths.home / ".claude" / "settings.json").exists())

    def test_invalid_claude_settings_preflight_prevents_install_and_update_runtime_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            settings = paths.home / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{invalid", encoding="utf-8")
            service = mock.Mock()
            output = io.StringIO()

            with mock.patch.dict(
                os.environ,
                {"AGENTWATCH_CONFIG_DIR": str(paths.config)},
                clear=True,
            ), mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(agentwatch, "install_runtime") as install_runtime, mock.patch(
                "sys.stdout", output
            ):
                install_result = agentwatch.main(
                    ["install", "--delivery", "bark", "--json", "--no-login"]
                )
                update_result = agentwatch.main(["update", "--json"])

            self.assertEqual(1, install_result)
            self.assertEqual(1, update_result)
            install_runtime.assert_not_called()
            service.install.assert_not_called()
            self.assertEqual("{invalid", settings.read_text(encoding="utf-8"))

    def test_explicit_bark_install_starts_without_login_or_keychain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.config.mkdir(parents=True)
            (paths.config / "env").write_text("BARK_URL=https://example.invalid/bark\n", encoding="utf-8")
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "active"
            store = mock.Mock()
            store.backend_name.return_value = "test"
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True), mock.patch.object(
                agentwatch, "InstallPaths", return_value=paths
            ), mock.patch.object(agentwatch, "ServiceManager", return_value=service), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(agentwatch, "install_runtime"), mock.patch.object(
                agentwatch, "_login"
            ) as login, mock.patch("sys.stdout", output):
                result = agentwatch.main(["install", "--delivery", "bark", "--json"])

            self.assertEqual(0, result)
            service.install.assert_called_once_with(should_start=True)
            store.load.assert_not_called()
            login.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertEqual("bark", payload["delivery_mode"])
            self.assertTrue(payload["operational"])

    def test_bark_two_stage_install_then_update_starts_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "stopped"
            store = mock.Mock()
            store.backend_name.return_value = "test"
            install_output = io.StringIO()
            human_output = io.StringIO()
            update_output = io.StringIO()
            with mock.patch.dict(
                os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True
            ), mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(
                agentwatch, "install_runtime"
            ):
                with mock.patch("sys.stdout", install_output):
                    installed = agentwatch.main(
                        ["install", "--delivery", "bark", "--json", "--no-login"]
                    )

                self.assertEqual(0, installed)
                service.install.assert_called_once_with(should_start=False)
                first_payload = json.loads(install_output.getvalue())
                self.assertFalse(first_payload["operational"])
                self.assertTrue(first_payload["bark_configuration_required"])
                self.assertIn("agentwatch update", first_payload["message"])

                service.reset_mock()
                with mock.patch("sys.stdout", human_output):
                    human_install = agentwatch.main(
                        ["install", "--delivery", "bark", "--no-login"]
                    )
                self.assertEqual(0, human_install)
                service.install.assert_called_once_with(should_start=False)
                self.assertIn("agentwatch update", human_output.getvalue())

                (paths.config / "env").write_text(
                    "BARK_URL=https://example.invalid/private-bark\n", encoding="utf-8"
                )
                service.reset_mock()
                service.state.return_value = "active"
                with mock.patch("sys.stdout", update_output):
                    updated = agentwatch.main(["update", "--json"])

            self.assertEqual(0, updated)
            service.install.assert_called_once_with(should_start=True)
            second_payload = json.loads(update_output.getvalue())
            self.assertTrue(second_payload["operational"])
            self.assertEqual(["bark"], second_payload["effective_channels"])
            store.load.assert_not_called()

    def test_both_two_stage_install_then_update_runs_bark_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "stopped"
            store = mock.Mock()
            store.load.return_value = None
            store.load_strict.return_value = None
            store.load_read_only.return_value = None
            store.backend_name.return_value = "test"
            install_output = io.StringIO()
            update_output = io.StringIO()
            with mock.patch.dict(
                os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True
            ), mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(
                agentwatch, "install_runtime"
            ):
                with mock.patch("sys.stdout", install_output):
                    installed = agentwatch.main(
                        ["install", "--delivery", "both", "--json", "--no-login"]
                    )

                self.assertEqual(0, installed)
                service.install.assert_called_once_with(should_start=False)
                first_payload = json.loads(install_output.getvalue())
                self.assertFalse(first_payload["operational"])
                self.assertTrue(first_payload["login_required"])
                self.assertTrue(first_payload["bark_configuration_required"])
                self.assertIn("agentwatch login", first_payload["message"])
                self.assertIn("agentwatch update", first_payload["message"])

                (paths.config / "env").write_text(
                    "BARK_KEY=private-bark-key\n", encoding="utf-8"
                )
                service.reset_mock()
                service.state.return_value = "active"
                with mock.patch("sys.stdout", update_output):
                    updated = agentwatch.main(["update", "--json"])

            self.assertEqual(0, updated)
            service.install.assert_called_once_with(should_start=True)
            second_payload = json.loads(update_output.getvalue())
            self.assertTrue(second_payload["operational"])
            self.assertTrue(second_payload["degraded"])
            self.assertTrue(second_payload["login_required"])
            self.assertEqual(["bark"], second_payload["effective_channels"])

    def test_both_agentwatch_only_update_restarts_to_pick_up_new_bark(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "active"
            store = mock.Mock()
            store.load.return_value = "computer-token"
            store.load_strict.return_value = "computer-token"
            store.load_read_only.return_value = "computer-token"
            store.backend_name.return_value = "test"
            install_output = io.StringIO()
            update_output = io.StringIO()
            with mock.patch.dict(
                os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True
            ), mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(
                agentwatch, "install_runtime"
            ):
                with mock.patch("sys.stdout", install_output):
                    installed = agentwatch.main(
                        ["install", "--delivery", "both", "--json", "--no-login"]
                    )

                self.assertEqual(0, installed)
                service.install.assert_called_once_with(should_start=True)
                first_payload = json.loads(install_output.getvalue())
                self.assertEqual(["agentwatch"], first_payload["effective_channels"])
                self.assertTrue(first_payload["bark_configuration_required"])
                self.assertIn("agentwatch update", first_payload["message"])

                (paths.config / "env").write_text(
                    "BARK_URL=https://example.invalid/private-bark\n", encoding="utf-8"
                )
                service.reset_mock()
                with mock.patch("sys.stdout", update_output):
                    updated = agentwatch.main(["update", "--json"])

            self.assertEqual(0, updated)
            service.install.assert_called_once_with(should_start=True)
            second_payload = json.loads(update_output.getvalue())
            self.assertTrue(second_payload["fully_configured"])
            self.assertEqual(["bark", "agentwatch"], second_payload["effective_channels"])

    def test_shell_receiver_values_do_not_mark_daemon_operational(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "stopped"
            store = mock.Mock()
            store.backend_name.return_value = "test"
            output = io.StringIO()
            shell_environment = {
                "AGENTWATCH_CONFIG_DIR": str(paths.config),
                "BARK_URL": "https://example.invalid/shell-only",
                "BARK_KEY": "shell-only-key",
                "NTFY_URL": "https://example.invalid/legacy-shell-only",
            }
            with mock.patch.dict(os.environ, shell_environment, clear=True), mock.patch.object(
                agentwatch, "InstallPaths", return_value=paths
            ), mock.patch.object(agentwatch, "ServiceManager", return_value=service), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(agentwatch, "install_runtime"), mock.patch("sys.stdout", output):
                result = agentwatch.main(
                    ["install", "--delivery", "bark", "--json", "--no-login"]
                )

            self.assertEqual(0, result)
            service.install.assert_called_once_with(should_start=False)
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["bark_configured"])
            self.assertFalse(payload["operational"])
            self.assertFalse(payload["legacy_ntfy_ignored"])

    def test_windows_ready_task_is_not_reported_as_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.config.mkdir(parents=True)
            paths.runtime.mkdir(parents=True)
            for filename in agentwatch.RUNTIME_FILES[:-1]:
                (paths.runtime / filename).write_text("# test\n", encoding="utf-8")
            (paths.config / "env").write_text(
                "BARK_URL=https://example.invalid/private-bark\n", encoding="utf-8"
            )
            agentwatch_core.save_delivery_mode("bark", paths.config)
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "ready"
            store = mock.Mock()
            store.backend_name.return_value = "test"
            status_output = io.StringIO()
            doctor_output = io.StringIO()
            with mock.patch.dict(
                os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True
            ), mock.patch.object(agentwatch, "InstallPaths", return_value=paths), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(agentwatch, "ComputerTokenStore", return_value=store):
                with mock.patch("sys.stdout", status_output):
                    status_result = agentwatch.main(["status", "--json"])
                with mock.patch("sys.stdout", doctor_output):
                    doctor_result = agentwatch.main(["doctor", "--json"])

            self.assertNotIn("ready", agentwatch.RUNNING_SERVICE_STATES)
            self.assertEqual(1, status_result)
            self.assertTrue(json.loads(status_output.getvalue())["operational"])
            self.assertEqual(1, doctor_result)
            doctor_payload = json.loads(doctor_output.getvalue())
            self.assertFalse(doctor_payload["checks"]["service_running"])
            self.assertFalse(doctor_payload["ok"])

    def test_logout_from_both_keeps_ready_bark_service_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.config.mkdir(parents=True)
            (paths.config / "env").write_text("BARK_URL=https://example.invalid/bark\n", encoding="utf-8")
            agentwatch_core.save_delivery_mode("both", paths.config)
            service = mock.Mock()
            store = mock.Mock()
            store.load_strict.return_value = "computer-token"
            api = mock.Mock()
            api.logout.return_value = {"ok": True}
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True), mock.patch.object(
                agentwatch, "InstallPaths", return_value=paths
            ), mock.patch.object(agentwatch, "ServiceManager", return_value=service), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(agentwatch, "AgentWatchApi", return_value=api), mock.patch("sys.stdout", output):
                result = agentwatch.main(["logout", "--json"])

            self.assertEqual(0, result)
            service.start.assert_called_once_with()
            service.stop.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertEqual(["bark"], payload["effective_channels"])
            self.assertTrue(payload["degraded"])

    def test_status_succeeds_for_running_operational_bark_without_android_login(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.config.mkdir(parents=True)
            (paths.config / "env").write_text("BARK_KEY=private-key\n", encoding="utf-8")
            agentwatch_core.save_delivery_mode("bark", paths.config)
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "active"
            store = mock.Mock()
            store.backend_name.return_value = "test"
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True), mock.patch.object(
                agentwatch, "InstallPaths", return_value=paths
            ), mock.patch.object(agentwatch, "ServiceManager", return_value=service), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch("sys.stdout", output):
                result = agentwatch.main(["status", "--json"])

            self.assertEqual(0, result)
            store.load.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["authenticated"])
            self.assertTrue(payload["operational"])
            self.assertTrue(payload["fully_configured"])

    def test_doctor_allows_both_degraded_to_ready_bark_without_server_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.config.mkdir(parents=True)
            paths.runtime.mkdir(parents=True)
            for filename in agentwatch.RUNTIME_FILES[:-1]:
                (paths.runtime / filename).write_text("# test\n", encoding="utf-8")
            (paths.config / "env").write_text("BARK_URL=https://example.invalid/bark\n", encoding="utf-8")
            agentwatch_core.save_delivery_mode("both", paths.config)
            agentwatch._configure_installed_claude_hooks(paths)
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "active"
            store = mock.Mock()
            store.load.return_value = None
            store.load_read_only.return_value = None
            store.backend_name.return_value = "test"
            api = mock.Mock()
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENTWATCH_CONFIG_DIR": str(paths.config)}, clear=True), mock.patch.object(
                agentwatch, "InstallPaths", return_value=paths
            ), mock.patch.object(agentwatch, "ServiceManager", return_value=service), mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), mock.patch.object(agentwatch, "AgentWatchApi", return_value=api), mock.patch("sys.stdout", output):
                result = agentwatch.main(["doctor", "--json"])

            self.assertEqual(0, result)
            api.health.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["operational"])
            self.assertTrue(payload["degraded"])
            self.assertEqual(["agentwatch"], payload["missing_channels"])


if __name__ == "__main__":
    unittest.main()
