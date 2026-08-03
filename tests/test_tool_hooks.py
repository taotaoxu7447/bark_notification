from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

import agentwatch
import agentwatch_core
import codex_watch_notifier as notifier
import tool_hook_config


def valid_hook_payload(source: str = "pi") -> dict[str, object]:
    schema, event_name = agentwatch.TOOL_HOOK_SOURCE_SCHEMAS[source]
    return {
        "schema": schema,
        "event_name": event_name,
        "session_id": f"{source}-session-1",
        "event_id": f"{source}-event-1",
        "timestamp": (
            1785800000123 if source == "opencode" else "2026-08-04T12:00:00Z"
        ),
        "cwd": "/tmp/example-project",
        "parent_session": "",
        "outcome": "completed",
        "stop_reason": "stop",
        "message": "任务已完成。",
    }


def watcher_hook_record(
    source: str = "pi",
    *,
    session_id: str | None = None,
    event_id: str | None = None,
    parent_session: str = "",
    outcome: str = "completed",
    message: str = "任务已完成。",
) -> dict[str, object]:
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "schema": notifier.TOOL_HOOK_SCHEMA,
        "source": source,
        "event_name": {"pi": "agent_settled", "opencode": "session.idle"}[source],
        "session_id": session_id or f"{source}-session-1",
        "event_id": event_id or f"{source}-event-1",
        "timestamp": timestamp,
        "cwd": "/tmp/example-project",
        "parent_session": parent_session,
        "outcome": outcome,
        "stop_reason": "stop" if outcome == "completed" else outcome,
        "message": message,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "received_at": int(time.time()),
    }


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, title: str, body: str, event: dict) -> bool:
        del title, body
        self.events.append(event)
        return True


class ToolHookIngestTests(unittest.TestCase):
    @staticmethod
    def _stdin(raw: bytes) -> io.TextIOWrapper:
        return io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")

    def test_valid_payload_is_normalized_into_one_private_atomic_queue_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            events_dir = Path(temp_dir) / "private-events"
            payload = valid_hook_payload("opencode")
            payload["session_id"] = "  opencode-session-1  "
            payload["cwd"] = "  /tmp/example-project  "
            payload["message"] = "\n  完成正文  \n"
            stdin = self._stdin(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

            with mock.patch.object(agentwatch.sys, "stdin", stdin), mock.patch.object(
                agentwatch.time, "time", return_value=1785800000
            ):
                written = agentwatch.ingest_tool_hook_event("opencode", events_dir)

            self.assertEqual(events_dir, written.parent)
            self.assertEqual([written], list(events_dir.glob("*.json")))
            self.assertEqual([], list(events_dir.glob(".*.tmp")))
            record = json.loads(written.read_text(encoding="utf-8"))
            self.assertEqual(record, notifier.read_owned_tool_hook_event(written))
            self.assertEqual(notifier.TOOL_HOOK_SCHEMA, record["schema"])
            self.assertEqual("opencode", record["source"])
            self.assertEqual("session.idle", record["event_name"])
            self.assertEqual("opencode-session-1", record["session_id"])
            self.assertEqual("/tmp/example-project", record["cwd"])
            self.assertEqual(payload["message"], record["message"])
            self.assertEqual(
                hashlib.sha256(str(payload["message"]).encode("utf-8")).hexdigest(),
                record["message_sha256"],
            )
            self.assertEqual(1785800000, record["received_at"])
            if os.name != "nt":
                self.assertEqual(0, stat.S_IMODE(events_dir.stat().st_mode) & 0o077)
                self.assertEqual(0, stat.S_IMODE(written.stat().st_mode) & 0o077)

    def test_strict_contract_rejects_wrong_shape_types_limits_and_timestamps(self) -> None:
        base = valid_hook_payload("pi")
        invalid_payloads: list[tuple[str, dict[str, object]]] = []

        extra = dict(base, unexpected="not allowed")
        invalid_payloads.append(("extra field", extra))
        missing = dict(base)
        missing.pop("event_id")
        invalid_payloads.append(("missing field", missing))
        invalid_payloads.append(("wrong schema", dict(base, schema="wrong")))
        invalid_payloads.append(("wrong event", dict(base, event_name="session.idle")))
        invalid_payloads.append(("empty session", dict(base, session_id="  ")))
        invalid_payloads.append(("non-string cwd", dict(base, cwd=123)))
        invalid_payloads.append(("oversized stop reason", dict(base, stop_reason="x" * 129)))
        invalid_payloads.append(
            ("oversized message", dict(base, message="x" * (64 * 1024 + 1)))
        )
        invalid_payloads.append(("wrong outcome", dict(base, outcome="running")))
        invalid_payloads.append(("boolean timestamp", dict(base, timestamp=True)))
        invalid_payloads.append(("zero timestamp", dict(base, timestamp=0)))
        invalid_payloads.append(("invalid timestamp text", dict(base, timestamp="not-a-time")))
        invalid_payloads.append(("NaN timestamp", dict(base, timestamp=float("nan"))))
        invalid_payloads.append(("infinite timestamp", dict(base, timestamp=float("inf"))))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, payload in invalid_payloads:
                with self.subTest(name=name):
                    events_dir = root / name.replace(" ", "-")
                    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    stdin = self._stdin(raw)
                    with mock.patch.object(agentwatch.sys, "stdin", stdin), self.assertRaises(
                        agentwatch_core.AgentWatchError
                    ):
                        agentwatch.ingest_tool_hook_event("pi", events_dir)
                    self.assertFalse(events_dir.exists())

    def test_invalid_source_raw_json_utf8_and_input_size_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid_raw = json.dumps(valid_hook_payload("pi")).encode("utf-8")
            stdin = self._stdin(valid_raw)
            with mock.patch.object(agentwatch.sys, "stdin", stdin), self.assertRaises(
                agentwatch_core.AgentWatchError
            ):
                agentwatch.ingest_tool_hook_event("unknown", root / "unknown")

            for name, raw in (("empty", b""), ("json", b"{"), ("utf8", b"\xff")):
                with self.subTest(name=name):
                    stdin = self._stdin(raw)
                    with mock.patch.object(agentwatch.sys, "stdin", stdin), self.assertRaises(
                        agentwatch_core.AgentWatchError
                    ):
                        agentwatch.ingest_tool_hook_event("pi", root / name)

            stdin = self._stdin(b"x" * 17)
            with mock.patch.object(agentwatch.sys, "stdin", stdin), mock.patch.object(
                agentwatch, "TOOL_HOOK_INPUT_LIMIT_BYTES", 16
            ), self.assertRaises(agentwatch_core.AgentWatchError):
                agentwatch.ingest_tool_hook_event("pi", root / "oversized")

    def test_hidden_cli_writes_valid_event_and_silently_ignores_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events_dir = root / "events"
            output = io.StringIO()
            errors = io.StringIO()
            stdin = self._stdin(
                json.dumps(valid_hook_payload("pi"), ensure_ascii=False).encode("utf-8")
            )
            with mock.patch.object(agentwatch.sys, "stdin", stdin), mock.patch(
                "sys.stdout", output
            ), mock.patch("sys.stderr", errors):
                result = agentwatch.main(
                    ["tool-hook", "--source", "pi", "--events-dir", str(events_dir)]
                )

            self.assertEqual(0, result)
            self.assertEqual("", output.getvalue())
            self.assertEqual("", errors.getvalue())
            self.assertEqual(1, len(list(events_dir.glob("*.json"))))

            invalid_dir = root / "invalid"
            output = io.StringIO()
            errors = io.StringIO()
            stdin = self._stdin(b"{")
            with mock.patch.object(agentwatch.sys, "stdin", stdin), mock.patch(
                "sys.stdout", output
            ), mock.patch("sys.stderr", errors):
                result = agentwatch.main(
                    ["tool-hook", "--source", "pi", "--events-dir", str(invalid_dir)]
                )

            self.assertEqual(0, result)
            self.assertEqual("", output.getvalue())
            self.assertEqual("", errors.getvalue())
            self.assertFalse(invalid_dir.exists())

            strict_output = io.StringIO()
            strict_errors = io.StringIO()
            stdin = self._stdin(b"{")
            with mock.patch.object(agentwatch.sys, "stdin", stdin), mock.patch(
                "sys.stdout", strict_output
            ), mock.patch("sys.stderr", strict_errors):
                strict_result = agentwatch.main(
                    [
                        "tool-hook",
                        "--source",
                        "opencode",
                        "--events-dir",
                        str(root / "strict-invalid"),
                        "--require-persist",
                    ]
                )

            self.assertEqual(1, strict_result)
            self.assertEqual("", strict_output.getvalue())
            self.assertEqual("", strict_errors.getvalue())


class ToolHookRegistrationTests(unittest.TestCase):
    def test_opencode_requires_awaited_dispose_lifecycle_version(self) -> None:
        old_version = mock.Mock(returncode=0, stdout="opencode 1.15.10\n", stderr="")
        supported_version = mock.Mock(
            returncode=0,
            stdout="opencode 1.15.11\n",
            stderr="",
        )
        with mock.patch.object(
            agentwatch.shutil,
            "which",
            return_value="/usr/local/bin/opencode",
        ), mock.patch.object(agentwatch, "_run", return_value=old_version):
            old_status = agentwatch._semver_cli_status(
                "opencode",
                agentwatch.MIN_OPENCODE_PLUGIN_VERSION,
            )
        with mock.patch.object(
            agentwatch.shutil,
            "which",
            return_value="/usr/local/bin/opencode",
        ), mock.patch.object(agentwatch, "_run", return_value=supported_version):
            supported_status = agentwatch._semver_cli_status(
                "opencode",
                agentwatch.MIN_OPENCODE_PLUGIN_VERSION,
            )

        self.assertEqual("1.15.11", old_status["minimum_cli_version"])
        self.assertFalse(old_status["cli_compatible"])
        self.assertTrue(supported_status["cli_compatible"])

    def test_registration_configures_both_tools_idempotently_and_uninstalls_only_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            pi_root = root / "pi-agent"
            opencode_root = root / "opencode"
            pi_path = pi_root / "extensions" / tool_hook_config.PI_EXTENSION_FILE_NAME
            opencode_path = (
                opencode_root / "plugins" / tool_hook_config.OPENCODE_PLUGIN_FILE_NAME
            )
            unrelated = opencode_root / "plugins" / "user-plugin.js"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("// keep me\n", encoding="utf-8")
            environment = {
                "PI_CODING_AGENT_DIR": str(pi_root),
                "OPENCODE_CONFIG_DIR": str(opencode_root),
            }

            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                agentwatch.shutil, "which", return_value=None
            ):
                agentwatch._preflight_installed_tool_hooks(paths)
                self.assertTrue(agentwatch._configure_installed_tool_hooks(paths))
                self.assertFalse(agentwatch._configure_installed_tool_hooks(paths))
                status = agentwatch._installed_tool_hook_status(paths)

                registration_path = (
                    paths.config / tool_hook_config.INTEGRATION_REGISTRATION_FILE_NAME
                )
                registration = json.loads(registration_path.read_text(encoding="utf-8"))
                self.assertEqual(agentwatch.TOOL_HOOK_REGISTRATION_VERSION, registration["version"])
                self.assertEqual({"pi", "opencode"}, set(registration["integrations"]))
                for source, integration_path in (
                    ("pi", pi_path),
                    ("opencode", opencode_path),
                ):
                    entry = registration["integrations"][source]
                    self.assertEqual(agentwatch.TOOL_HOOK_MANAGED_IDS[source], entry["managed_id"])
                    self.assertEqual(str(integration_path), entry["path"])
                    self.assertEqual("", entry["pending_sha256"])
                    self.assertEqual(
                        hashlib.sha256(integration_path.read_bytes()).hexdigest(),
                        entry["installed_sha256"],
                    )
                self.assertTrue(pi_path.read_text(encoding="utf-8").startswith(
                    tool_hook_config.PI_MANAGED_MARKER + "\n"
                ))
                self.assertTrue(opencode_path.read_text(encoding="utf-8").startswith(
                    tool_hook_config.OPENCODE_MANAGED_MARKER + "\n"
                ))
                self.assertIn(str(paths.runtime / "agentwatch.py"), pi_path.read_text(encoding="utf-8"))
                self.assertIn(str(paths.runtime / "agentwatch.py"), opencode_path.read_text(encoding="utf-8"))
                self.assertTrue(status["pi"]["active"])
                self.assertTrue(status["opencode"]["active"])
                if os.name != "nt":
                    self.assertEqual(
                        0,
                        stat.S_IMODE(registration_path.stat().st_mode) & 0o077,
                    )

                self.assertTrue(
                    agentwatch._configure_installed_tool_hooks(paths, enabled=False)
                )

            self.assertFalse(pi_path.exists())
            self.assertFalse(opencode_path.exists())
            self.assertFalse(registration_path.exists())
            self.assertEqual("// keep me\n", unrelated.read_text(encoding="utf-8"))

    def test_registration_migrates_a_changed_tool_directory_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            first_root = root / "pi-first"
            second_root = root / "pi-second"
            first_path = (
                first_root / "extensions" / tool_hook_config.PI_EXTENSION_FILE_NAME
            )
            second_path = (
                second_root / "extensions" / tool_hook_config.PI_EXTENSION_FILE_NAME
            )

            with mock.patch.object(agentwatch.shutil, "which", return_value=None):
                with mock.patch.dict(
                    os.environ, {"PI_CODING_AGENT_DIR": str(first_root)}, clear=True
                ):
                    agentwatch._preflight_installed_tool_hooks(paths)
                    agentwatch._configure_installed_tool_hooks(paths)
                with mock.patch.dict(
                    os.environ, {"PI_CODING_AGENT_DIR": str(second_root)}, clear=True
                ):
                    agentwatch._preflight_installed_tool_hooks(paths)
                    agentwatch._configure_installed_tool_hooks(paths)

            registration = json.loads(
                (
                    paths.config / tool_hook_config.INTEGRATION_REGISTRATION_FILE_NAME
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(first_path.exists())
            self.assertTrue(second_path.exists())
            self.assertEqual(
                str(second_path),
                registration["integrations"]["pi"]["path"],
            )

    def test_pending_registration_is_finalized_after_an_interrupted_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                agentwatch.shutil, "which", return_value=None
            ):
                agentwatch._configure_installed_tool_hooks(paths)
                registered = agentwatch._load_tool_hook_registration(paths)
                pi_entry = dict(registered["pi"])
                installed_digest = pi_entry["installed_sha256"]
                pi_entry["installed_sha256"] = ""
                pi_entry["pending_sha256"] = installed_digest
                registered["pi"] = pi_entry
                agentwatch._save_tool_hook_registration(paths, registered)

                self.assertFalse(agentwatch._configure_installed_tool_hooks(paths))
                recovered = agentwatch._load_tool_hook_registration(paths)["pi"]
                status = agentwatch._installed_tool_hook_status(paths)["pi"]

            self.assertEqual(installed_digest, recovered["installed_sha256"])
            self.assertEqual("", recovered["pending_sha256"])
            self.assertTrue(status["registered"])
            self.assertTrue(status["active"])

    def test_marker_preserving_edit_blocks_update_and_uninstall_without_removing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            paths.runtime.mkdir(parents=True)
            runtime_script = paths.runtime / "agentwatch.py"
            runtime_script.write_text("# runtime must remain\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                agentwatch.shutil, "which", return_value=None
            ):
                agentwatch._configure_installed_tool_hooks(paths)
            pi_path = tool_hook_config.pi_extension_path(paths.home, {})
            tampered = pi_path.read_text(encoding="utf-8") + "// user-modified after install\n"
            pi_path.write_text(tampered, encoding="utf-8")

            service = mock.Mock()
            update_output = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                agentwatch, "InstallPaths", return_value=paths
            ), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch, "_preflight_installed_claude_hooks"
            ), mock.patch.object(
                agentwatch.shutil, "which", return_value=None
            ), mock.patch.object(
                agentwatch, "install_runtime"
            ) as install_runtime, mock.patch("sys.stdout", update_output):
                update_result = agentwatch.main(["update", "--json"])

            self.assertEqual(1, update_result)
            install_runtime.assert_not_called()
            self.assertEqual("local_error", json.loads(update_output.getvalue())["error"])
            self.assertTrue(runtime_script.exists())
            self.assertEqual(tampered, pi_path.read_text(encoding="utf-8"))

            uninstall_output = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                agentwatch, "InstallPaths", return_value=paths
            ), mock.patch.object(
                agentwatch, "ServiceManager", return_value=service
            ), mock.patch.object(
                agentwatch, "_configure_installed_claude_hooks", return_value=False
            ), mock.patch("sys.stdout", uninstall_output):
                uninstall_result = agentwatch.main(["uninstall", "--json"])

            self.assertEqual(1, uninstall_result)
            service.uninstall.assert_called_once_with()
            uninstall_payload = json.loads(uninstall_output.getvalue())
            self.assertEqual("integration_cleanup_failed", uninstall_payload["error"])
            self.assertTrue(uninstall_payload["tool_hook_cleanup_failed"])
            self.assertTrue(uninstall_payload["runtime_preserved"])
            self.assertTrue(runtime_script.exists())
            self.assertEqual(tampered, pi_path.read_text(encoding="utf-8"))

    def test_unregistered_agentwatch_named_file_is_not_adopted_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root / "config", root / "home")
            pi_path = tool_hook_config.pi_extension_path(paths.home, {})
            content = tool_hook_config.build_pi_extension(
                Path("/old/python"),
                Path("/old/agentwatch.py"),
                Path("/old/events"),
            )
            pi_path.parent.mkdir(parents=True)
            pi_path.write_text(content, encoding="utf-8")
            registration_path = (
                paths.config / tool_hook_config.INTEGRATION_REGISTRATION_FILE_NAME
            )

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                agentwatch.shutil, "which", return_value=None
            ):
                with self.assertRaises(agentwatch_core.AgentWatchError):
                    agentwatch._preflight_installed_tool_hooks(paths)
                with self.assertRaises(agentwatch_core.AgentWatchError):
                    agentwatch._configure_installed_tool_hooks(paths, enabled=False)

            self.assertFalse(registration_path.exists())
            self.assertEqual(content, pi_path.read_text(encoding="utf-8"))


class ToolHookWatcherTests(unittest.TestCase):
    @staticmethod
    def _write(path: Path, record: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.parent.chmod(0o700)
        path.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            path.chmod(0o600)

    @staticmethod
    def _owned_path(
        root: Path,
        record: dict[str, object],
        *,
        created: int,
        nonce: str,
    ) -> Path:
        identity = hashlib.sha256(
            (
                f"{record['source']}\0{record['session_id']}\0{record['event_id']}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        return root / (
            f"{created}-{record['source']}-{identity}-{nonce}.json"
        )

    def test_first_discovery_baselines_existing_queue_items_at_eof(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "events"
            record = watcher_hook_record("pi")
            old_path = self._owned_path(
                root,
                record,
                created=1785800000000000000,
                nonce="00000001",
            )
            self._write(old_path, record)
            state = {"files": {}, "sent": {}, "delivery_attempts": {}}
            delivery = RecordingNotifier()
            log = mock.Mock()

            self.assertTrue(
                notifier.initialize_tool_hook_events(
                    state,
                    root,
                    process_existing=False,
                    log=log,
                )
            )
            self.assertEqual(
                old_path.stat().st_size,
                state["files"][str(old_path)]["offset"],
            )
            self.assertFalse(
                notifier.initialize_tool_hook_events(
                    state,
                    root,
                    process_existing=False,
                    log=log,
                )
            )
            self.assertEqual(
                0,
                notifier.process_tool_hook_event_file(
                    old_path, state, delivery, log
                ),
            )

            self.assertEqual([], delivery.events)
            self.assertFalse(old_path.exists())
            self.assertNotIn(str(old_path), state["files"])

    def test_new_queue_item_is_sent_once_and_duplicate_item_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "events"
            root.mkdir()
            state = {
                "files": {},
                "sent": {},
                "delivery_attempts": {},
                "tool_hooks_initialized": str(root),
            }
            delivery = RecordingNotifier()
            record = watcher_hook_record("opencode")
            first = self._owned_path(
                root,
                record,
                created=1785800000000000001,
                nonce="00000001",
            )
            duplicate = self._owned_path(
                root,
                record,
                created=1785800000000000002,
                nonce="00000002",
            )
            self._write(first, record)

            with mock.patch.dict(
                os.environ,
                {"OPENCODE_WATCH_ENABLED": "1", "CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "3600"},
            ):
                first_count = notifier.process_tool_hook_event_file(
                    first, state, delivery, mock.Mock()
                )
                self._write(duplicate, record)
                duplicate_count = notifier.process_tool_hook_event_file(
                    duplicate, state, delivery, mock.Mock()
                )

            self.assertEqual(1, first_count)
            self.assertEqual(0, duplicate_count)
            self.assertEqual(1, len(delivery.events))
            self.assertEqual("opencode_turn_completed", delivery.events[0]["event_type"])
            self.assertFalse(first.exists())
            self.assertFalse(duplicate.exists())
            self.assertEqual({}, state["files"])
            self.assertEqual(1, len(state["sent"]))

    def test_parent_sessions_are_suppressed_by_default(self) -> None:
        path = Path("/private/tool-event.json")
        pi_child = watcher_hook_record("pi", parent_session="pi-parent")
        opencode_child = watcher_hook_record(
            "opencode", parent_session="opencode-parent"
        )
        environment = {
            "PI_WATCH_ENABLED": "1",
            "OPENCODE_WATCH_ENABLED": "1",
            "PI_WATCH_NOTIFY_FORKED_SESSIONS": "0",
        }

        with mock.patch.dict(os.environ, environment):
            self.assertIsNone(
                notifier.trigger_from_tool_hook_record(path, 0, pi_child)
            )
            self.assertIsNone(
                notifier.trigger_from_tool_hook_record(path, 0, opencode_child)
            )
            with mock.patch.dict(
                os.environ, {"PI_WATCH_NOTIFY_FORKED_SESSIONS": "1"}
            ):
                self.assertIsNotNone(
                    notifier.trigger_from_tool_hook_record(path, 0, pi_child)
                )
                self.assertIsNone(
                    notifier.trigger_from_tool_hook_record(path, 0, opencode_child)
                )

    def test_stable_id_uses_official_session_event_and_outcome_not_mutable_text(self) -> None:
        path = Path("/private/tool-event.json")
        first_record = watcher_hook_record("pi", message="first result")
        changed_record = watcher_hook_record("pi", message="changed result")
        changed_record["timestamp"] = "2026-08-04T12:01:00Z"
        changed_record["cwd"] = "/tmp/renamed-project"
        different_event = watcher_hook_record("pi", event_id="pi-event-2")

        with mock.patch.dict(os.environ, {"PI_WATCH_ENABLED": "1"}):
            first = notifier.trigger_from_tool_hook_record(path, 0, first_record)
            changed = notifier.trigger_from_tool_hook_record(path, 1, changed_record)
            other = notifier.trigger_from_tool_hook_record(path, 2, different_event)

        self.assertIsNotNone(first)
        self.assertIsNotNone(changed)
        self.assertIsNotNone(other)
        self.assertEqual(first["stable_id"], changed["stable_id"])
        self.assertNotEqual(first["stable_id"], other["stable_id"])

    def test_validation_preserves_message_whitespace_and_rejects_hash_tampering(self) -> None:
        message = "\n  final answer  \n"
        record = watcher_hook_record("opencode", message=message)

        parsed = notifier.validated_tool_hook_record(record)

        self.assertIsNotNone(parsed)
        self.assertEqual(message, parsed["message"])
        tampered = dict(record, message_sha256="0" * 64)
        self.assertIsNone(notifier.validated_tool_hook_record(tampered))

    def test_foreign_or_malformed_json_is_not_enumerated_processed_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "events"
            record = watcher_hook_record("pi")
            foreign_name = root / "foreign.json"
            self._write(foreign_name, record)

            bad_hash_record = dict(record, message_sha256="0" * 64)
            malformed_owned_name = self._owned_path(
                root,
                bad_hash_record,
                created=1785800000000000003,
                nonce="00000003",
            )
            self._write(malformed_owned_name, bad_hash_record)
            state = {"files": {}, "sent": {}, "delivery_attempts": {}}
            delivery = RecordingNotifier()

            self.assertEqual([], notifier.tool_hook_event_files(root))
            for path in (foreign_name, malformed_owned_name):
                with self.subTest(path=path.name):
                    self.assertEqual(
                        0,
                        notifier.process_tool_hook_event_file(
                            path, state, delivery, mock.Mock()
                        ),
                    )
                    self.assertTrue(path.exists())

            self.assertEqual({}, state["files"])
            self.assertEqual([], delivery.events)


if __name__ == "__main__":
    unittest.main()
