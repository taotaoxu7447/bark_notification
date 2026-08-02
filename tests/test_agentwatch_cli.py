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

    def test_macos_keychain_secret_uses_stdin_not_process_arguments(self) -> None:
        completed = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(agentwatch_core.subprocess, "run", return_value=completed) as run:
            store = agentwatch_core.ComputerTokenStore("computer-1", Path("/tmp/unused"), system_name="Darwin")
            store.save("secret-computer-token")

        args, kwargs = run.call_args
        self.assertNotIn("secret-computer-token", args[0])
        self.assertEqual("-w", args[0][-1])
        self.assertEqual("secret-computer-token\nsecret-computer-token\n", kwargs["input"])
        self.assertIs(kwargs["stdout"], agentwatch_core.subprocess.DEVNULL)

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
            event_id="aw2_11111111111141118111111111111111_stable.event",
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
        self.assertLessEqual(len(generated), 128)
        self.assertRegex(generated, re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,126}[A-Za-z0-9])?\Z"))

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


if __name__ == "__main__":
    unittest.main()
