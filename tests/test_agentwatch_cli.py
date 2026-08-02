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
        self.assertTrue(response["ok"])


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
            with mock.patch.object(agentwatch, "_run", return_value=completed) as run:
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
            with mock.patch.object(agentwatch, "_run", return_value=completed) as run:
                agentwatch.ServiceManager(paths, system_name="Windows").install(authenticated=False)

            commands = [call.args[0] for call in run.call_args_list]
            register = next(command for command in commands if command[0] == "powershell.exe")
            registration_script = register[-1]
            self.assertIn("-WindowStyle Hidden", registration_script)
            self.assertIn("-RestartCount 999", registration_script)
            self.assertIn(["schtasks.exe", "/Change", "/TN", agentwatch.WINDOWS_TASK, "/Disable"], commands)
            wrapper = (paths.runtime / "run_notifier.ps1").read_text(encoding="utf-8")
            self.assertIn("task.out.log", wrapper)
            self.assertIn("task.err.log", wrapper)

    def test_logout_network_failure_keeps_local_token_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            store = mock.Mock()
            store.load.return_value = "still-valid-token"
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

    def test_logout_401_is_already_revoked_and_deletes_local_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            store = mock.Mock()
            store.load.return_value = "revoked-token"
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
            for filename in agentwatch.RUNTIME_FILES[:3]:
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
            store.load.return_value = "computer-token"
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
            for filename in agentwatch.RUNTIME_FILES[:3]:
                (paths.runtime / filename).write_text("# test\n", encoding="utf-8")
            (paths.config / "env").write_text("BARK_URL=https://example.invalid/bark\n", encoding="utf-8")
            agentwatch_core.save_delivery_mode("both", paths.config)
            service = mock.Mock()
            service.installed.return_value = True
            service.state.return_value = "active"
            store = mock.Mock()
            store.load.return_value = None
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
