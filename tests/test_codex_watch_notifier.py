import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import codex_watch_notifier as notifier


class CodexSessionFilteringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.index_patch = mock.patch.dict(
            os.environ,
            {"CODEX_SESSION_INDEX": str(self.root / "missing-session-index.jsonl")},
        )
        self.index_patch.start()

    def tearDown(self) -> None:
        self.index_patch.stop()
        self.temp_dir.cleanup()

    def write_rollout(self, *records: dict) -> Path:
        path = self.root / "rollout-test.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                json.dump(record, handle, ensure_ascii=False)
                handle.write("\n")
        return path

    @staticmethod
    def session_meta(thread_source: str, parent_thread_id: str = "") -> dict:
        payload = {
            "id": f"{thread_source}-thread",
            "cwd": "/tmp/project",
            "source": "vscode",
            "thread_source": thread_source,
        }
        if parent_thread_id:
            payload["parent_thread_id"] = parent_thread_id
        return {"type": "session_meta", "payload": payload}

    @staticmethod
    def task_complete() -> dict:
        return {
            "timestamp": "2026-07-12T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "任务已完成。",
            },
        }

    @staticmethod
    def turn_aborted() -> dict:
        return {
            "timestamp": "2026-07-12T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "turn_aborted",
                "turn_id": "turn-1",
                "reason": "stopped",
            },
        }

    def test_main_session_task_complete_is_not_filtered(self) -> None:
        path = self.write_rollout(self.session_meta("user"))

        event = notifier.trigger_from_record(path, 1, self.task_complete(), set())

        self.assertIsNotNone(event)
        self.assertEqual("user-thread", event["thread_id"])

    def test_subagent_task_complete_is_filtered_by_default(self) -> None:
        path = self.write_rollout(self.session_meta("subagent", parent_thread_id="parent"))

        with mock.patch.dict(os.environ, {"CODEX_WATCH_NOTIFY_SUBAGENTS": "0"}):
            event = notifier.trigger_from_record(path, 1, self.task_complete(), set())

        self.assertIsNone(event)

    def test_subagent_abort_is_filtered_by_default(self) -> None:
        path = self.write_rollout(self.session_meta("subagent", parent_thread_id="parent"))

        with mock.patch.dict(os.environ, {"CODEX_WATCH_NOTIFY_SUBAGENTS": "0"}):
            event = notifier.trigger_from_record(path, 1, self.turn_aborted(), set())

        self.assertIsNone(event)

    def test_subagent_notification_can_be_enabled(self) -> None:
        path = self.write_rollout(self.session_meta("subagent", parent_thread_id="parent"))

        with mock.patch.dict(os.environ, {"CODEX_WATCH_NOTIFY_SUBAGENTS": "1"}):
            event = notifier.trigger_from_record(path, 1, self.task_complete(), set())

        self.assertIsNotNone(event)
        self.assertEqual("subagent-thread", event["thread_id"])

    def test_missing_metadata_is_not_filtered(self) -> None:
        path = self.write_rollout({"type": "event_msg", "payload": {"type": "task_started"}})

        with mock.patch.dict(os.environ, {"CODEX_WATCH_NOTIFY_SUBAGENTS": "0"}):
            event = notifier.trigger_from_record(path, 1, self.task_complete(), set())

        self.assertIsNotNone(event)

    def test_first_session_meta_defines_rollout_identity(self) -> None:
        path = self.write_rollout(
            self.session_meta("subagent", parent_thread_id="parent"),
            self.session_meta("user"),
        )

        meta = notifier.load_session_meta(path)
        with mock.patch.dict(os.environ, {"CODEX_WATCH_NOTIFY_SUBAGENTS": "0"}):
            event = notifier.trigger_from_record(path, 1, self.task_complete(), set())

        self.assertEqual("subagent", meta["thread_source"])
        self.assertEqual("parent", meta["parent_thread_id"])
        self.assertIsNone(event)

    def test_process_file_logs_subagent_suppression_once_and_never_sends(self) -> None:
        path = self.write_rollout(
            self.session_meta("subagent", parent_thread_id="parent"),
            self.task_complete(),
        )
        state = {"files": {}, "sent": {}}
        messages = []

        class RecordingNotifier:
            def __init__(self) -> None:
                self.send_count = 0

            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body, event
                self.send_count += 1
                return True

        recording_notifier = RecordingNotifier()
        with mock.patch.dict(os.environ, {"CODEX_WATCH_NOTIFY_SUBAGENTS": "0"}):
            notifier.process_file(path, state, recording_notifier, set(), messages.append)
            with path.open("a", encoding="utf-8") as handle:
                json.dump(self.task_complete(), handle, ensure_ascii=False)
                handle.write("\n")
            notifier.process_file(path, state, recording_notifier, set(), messages.append)

        suppression_messages = [message for message in messages if "subagent notifications suppressed" in message]
        self.assertEqual(0, recording_notifier.send_count)
        self.assertEqual(1, len(suppression_messages))
        self.assertEqual(path.stat().st_size, state["files"][str(path)]["offset"])


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_main_session_only_policy_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = SimpleNamespace(
                state=str(root / "state.json"),
                log=str(root / "notifier.log"),
                sessions_root=[str(root / "sessions")],
                include_archived=False,
                disable_zcode=True,
                zcode_log_root=str(root / "zcode"),
            )
            output = io.StringIO()
            environment = {
                "CODEX_WATCH_ENV": str(root / "missing.env"),
                "CODEX_WATCH_MACOS_NOTIFICATION": "0",
                "CODEX_WATCH_NOTIFY_SUBAGENTS": "0",
            }

            with mock.patch.dict(os.environ, environment), mock.patch.object(
                notifier.platform, "system", return_value="Linux"
            ):
                with contextlib.redirect_stdout(output):
                    result = notifier.doctor(args, notifier.Logger(None))

        self.assertEqual(0, result)
        self.assertIn("Codex subagent notifications: main sessions only", output.getvalue())


class KimiWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_session(self, agent_id: str = "main", agent_type: str = "main") -> Path:
        session_dir = self.root / "wd_project" / "session_kimi-123"
        wire_path = session_dir / "agents" / agent_id / "wire.jsonl"
        wire_path.parent.mkdir(parents=True)
        state = {
            "title": "Kimi 测试任务",
            "workDir": "/tmp/kimi-project",
            "agents": {
                agent_id: {
                    "type": agent_type,
                    "parentAgentId": None if agent_type == "main" else "main",
                }
            },
        }
        (session_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        message_record = {
            "type": "context.append_loop_event",
            "time": "2026-07-29T00:00:00Z",
            "event": {
                "type": "content.part",
                "turnId": "turn-7",
                "step": 2,
                "part": {"type": "text", "text": "Kimi 任务已完成。"},
            },
        }
        end_record = {
            "type": "context.append_loop_event",
            "time": "2026-07-29T00:00:01Z",
            "event": {
                "type": "step.end",
                "turnId": "turn-7",
                "step": 2,
                "finishReason": "end_turn",
            },
        }
        with wire_path.open("w", encoding="utf-8") as handle:
            for record in (message_record, end_record):
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return wire_path

    def test_main_end_turn_creates_notification(self) -> None:
        path = self.write_session()
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])

        event = notifier.trigger_from_kimi_record(path, path.stat().st_size, record)

        self.assertIsNotNone(event)
        self.assertEqual("kimi_turn_completed", event["event_type"])
        self.assertEqual("Kimi Code", event["bark_group"])
        self.assertEqual(notifier.DEFAULT_KIMI_BARK_ICON, event["bark_icon"])
        self.assertEqual("Kimi 测试任务", event["session_title"])
        self.assertIn("Kimi 任务已完成", event["message"])

    def test_tool_use_step_is_ignored(self) -> None:
        path = self.write_session()
        record = {
            "type": "context.append_loop_event",
            "event": {
                "type": "step.end",
                "turnId": "turn-7",
                "step": 1,
                "finishReason": "tool_use",
            },
        }

        event = notifier.trigger_from_kimi_record(path, 1, record)

        self.assertIsNone(event)

    def test_subagent_is_silent_by_default_and_can_be_enabled(self) -> None:
        path = self.write_session(agent_id="agent-0", agent_type="subagent")
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])

        with mock.patch.dict(os.environ, {"KIMI_WATCH_NOTIFY_SUBAGENTS": "0"}):
            silent_event = notifier.trigger_from_kimi_record(path, path.stat().st_size, record)
        with mock.patch.dict(os.environ, {"KIMI_WATCH_NOTIFY_SUBAGENTS": "1"}):
            enabled_event = notifier.trigger_from_kimi_record(path, path.stat().st_size, record)

        self.assertIsNone(silent_event)
        self.assertIsNotNone(enabled_event)

    def test_incremental_processor_sends_end_turn_once(self) -> None:
        path = self.write_session()
        state = {"files": {}, "sent": {}}
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        with mock.patch.dict(os.environ, {"CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0"}):
            notifier.process_external_file(
                path,
                state,
                RecordingNotifier(),
                notifier.Logger(None),
                "Kimi Code",
                notifier.trigger_from_kimi_record,
            )
            notifier.process_external_file(
                path,
                state,
                RecordingNotifier(),
                notifier.Logger(None),
                "Kimi Code",
                notifier.trigger_from_kimi_record,
            )

        self.assertEqual(1, len(sent_events))
        self.assertEqual(path.stat().st_size, state["files"][str(path)]["offset"])


class GrokWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_session(self, parent_session_id: str = "") -> Path:
        session_dir = self.root / "%2Ftmp%2Fgrok-project" / "grok-session-123"
        session_dir.mkdir(parents=True)
        summary = {
            "info": {"id": "grok-session-123", "cwd": "/tmp/grok-project"},
            "generated_title": "Grok 测试任务",
            "session_summary": "Grok 测试任务",
        }
        if parent_session_id:
            summary["parent_session_id"] = parent_session_id
        (session_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        assistant_message = {
            "type": "assistant",
            "content": "Grok 任务已完成。",
        }
        (session_dir / "chat_history.jsonl").write_text(
            json.dumps(assistant_message, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        events_path = session_dir / "events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "type": "turn_ended",
                    "outcome": "completed",
                    "ts": "2026-07-29T00:00:01Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return events_path

    def test_main_completed_turn_creates_notification(self) -> None:
        path = self.write_session()
        record = json.loads(path.read_text(encoding="utf-8").strip())

        event = notifier.trigger_from_grok_record(path, 0, record)

        self.assertIsNotNone(event)
        self.assertEqual("grok_turn_completed", event["event_type"])
        self.assertEqual("Grok Build", event["bark_group"])
        self.assertEqual(notifier.DEFAULT_GROK_BARK_ICON, event["bark_icon"])
        self.assertEqual("Grok 测试任务", event["session_title"])
        self.assertIn("Grok 任务已完成", event["message"])

    def test_error_and_cancelled_outcomes_create_attention_events(self) -> None:
        path = self.write_session()

        error_event = notifier.trigger_from_grok_record(
            path,
            1,
            {"type": "turn_ended", "outcome": "error", "ts": "2026-07-29T00:00:01Z"},
        )
        cancelled_event = notifier.trigger_from_grok_record(
            path,
            2,
            {"type": "turn_ended", "outcome": "cancelled", "ts": "2026-07-29T00:00:02Z"},
        )

        self.assertEqual("需要处理", error_event["status"])
        self.assertEqual("已取消", cancelled_event["status"])

    def test_child_session_is_silent_by_default_and_can_be_enabled(self) -> None:
        path = self.write_session(parent_session_id="parent-session")
        record = json.loads(path.read_text(encoding="utf-8").strip())

        with mock.patch.dict(os.environ, {"GROK_WATCH_NOTIFY_SUBAGENTS": "0"}):
            silent_event = notifier.trigger_from_grok_record(path, 0, record)
        with mock.patch.dict(os.environ, {"GROK_WATCH_NOTIFY_SUBAGENTS": "1"}):
            enabled_event = notifier.trigger_from_grok_record(path, 0, record)

        self.assertIsNone(silent_event)
        self.assertIsNotNone(enabled_event)

    def test_incremental_processor_sends_completed_turn_once(self) -> None:
        path = self.write_session()
        state = {"files": {}, "sent": {}}
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        with mock.patch.dict(os.environ, {"CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0"}):
            notifier.process_external_file(
                path,
                state,
                RecordingNotifier(),
                notifier.Logger(None),
                "Grok Build",
                notifier.trigger_from_grok_record,
            )
            notifier.process_external_file(
                path,
                state,
                RecordingNotifier(),
                notifier.Logger(None),
                "Grok Build",
                notifier.trigger_from_grok_record,
            )

        self.assertEqual(1, len(sent_events))
        self.assertEqual(path.stat().st_size, state["files"][str(path)]["offset"])


if __name__ == "__main__":
    unittest.main()
