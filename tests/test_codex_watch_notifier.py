import json
import os
import tempfile
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
