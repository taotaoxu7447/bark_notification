from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import codex_watch_notifier as notifier


class SequenceNotifier:
    def __init__(self, *results: bool) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def send(self, title: str, body: str, event: dict) -> bool:
        del title, body
        self.calls.append(str(event.get("stable_id") or ""))
        if not self.results:
            raise AssertionError("unexpected delivery attempt")
        return self.results.pop(0)


class NotifierDeliveryContractTests(unittest.TestCase):
    def test_delivery_limits_are_hard_clamped(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "NOTIFY_DELIVERY_MAX_ATTEMPTS": "999",
                "NOTIFY_DELIVERY_RETRY_DELAY_SECONDS": "0",
            },
        ):
            self.assertEqual(2, notifier.delivery_max_attempts())
            self.assertEqual(30, notifier.delivery_retry_delay_seconds())

    def test_channel_exception_is_not_retried_inline(self) -> None:
        delivery = notifier.Notifier(False, notifier.Logger(None))
        delivery.channels = ["bark", "ntfy"]

        with mock.patch.object(delivery, "_send_bark", side_effect=RuntimeError("bark down")) as bark_send, mock.patch.object(
            delivery,
            "_send_ntfy",
            side_effect=RuntimeError("ntfy down"),
        ) as ntfy_send:
            sent = delivery.send("title", "body", {"event_type": "codex_task_complete"})

        self.assertFalse(sent)
        self.assertEqual(1, bark_send.call_count)
        self.assertEqual(1, ntfy_send.call_count)

    def test_bark_payload_contains_stable_id(self) -> None:
        with mock.patch.dict(os.environ, {"BARK_URL": "https://example.invalid/push"}):
            delivery = notifier.Notifier(False, notifier.Logger(None))
            with mock.patch.object(delivery, "_http_post", return_value=True) as http_post:
                sent = delivery._send_bark(
                    "title",
                    "body",
                    {"stable_id": "stable-123", "bark_group": "Codex"},
                )

        self.assertTrue(sent)
        encoded_payload = http_post.call_args.args[1]
        payload = urllib.parse.parse_qs(encoded_payload.decode("utf-8"))
        self.assertEqual(["agent-watch-stable-123"], payload["id"])

    def test_ntfy_payload_carries_stable_sequence_and_source(self) -> None:
        environment = {
            "NTFY_URL": "https://example.invalid/agent-watch",
            "NTFY_TOKEN": "publisher-token",
            "AGENT_WATCH_PUBLISHER_ID": "mac-a1b2",
        }
        event = {
            "event_type": "kimi_turn_completed",
            "stable_id": "abc123",
            "ntfy_tags": "robot,computer",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            delivery = notifier.Notifier(False, notifier.Logger(None))
            with mock.patch.object(delivery, "_http_post", return_value=True) as http_post:
                sent = delivery._send_ntfy("Kimi Code 已完成", "body", event)

        self.assertTrue(sent)
        url, _payload, _content_type, headers = http_post.call_args.args
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(["aw1_mac-a1b2_abc123"], [headers["X-Sequence-ID"]])
        self.assertIn("agentwatch_v1", query["tags"][0])
        self.assertIn("source_kimi", query["tags"][0])
        self.assertEqual("Bearer publisher-token", headers["Authorization"])

    def test_ntfy_retry_reuses_the_same_sequence_id(self) -> None:
        event = {"event_type": "codex_task_complete", "stable_id": "same-event"}
        with mock.patch.dict(
            os.environ,
            {
                "NTFY_URL": "https://example.invalid/agent-watch",
                "AGENT_WATCH_PUBLISHER_ID": "host-01",
            },
            clear=False,
        ):
            first = notifier.ntfy_sequence_id(event)
            second = notifier.ntfy_sequence_id(event)

        self.assertEqual("aw1_host-01_same-event", first)
        self.assertEqual(first, second)

    def test_epoch_millisecond_timestamp_is_rendered_as_local_time(self) -> None:
        rendered = notifier.utc_to_local(1_785_282_889_000)

        self.assertIn("2026-07-29", rendered)
        self.assertNotIn("1785282889000", rendered)

    def test_single_instance_lock_blocks_a_second_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "state.json.lock"
            first = notifier.SingleInstanceLock(lock_path)
            second = notifier.SingleInstanceLock(lock_path)

            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


class DeliveryRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_path = self.root / "state.json"
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "CODEX_SESSION_INDEX": str(self.root / "missing-session-index.jsonl"),
                "CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0",
                "NOTIFY_DELIVERY_MAX_ATTEMPTS": "2",
                "NOTIFY_DELIVERY_RETRY_DELAY_SECONDS": "60",
            },
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def new_state() -> dict:
        return {
            "version": notifier.STATE_VERSION,
            "initialized": True,
            "files": {},
            "sent": {},
            "delivery_attempts": {},
            "delivery_stats": {},
        }

    @staticmethod
    def external_trigger(path: Path, offset: int, record: dict) -> dict | None:
        del path, offset
        if not record.get("notify"):
            return None
        stable_id = str(record["id"])
        return {
            "event_type": "test_turn_completed",
            "timestamp": None,
            "session_id": "test-session",
            "stable_id": stable_id,
            "notification_title": f"event {stable_id}",
            "notification_body": "done",
        }

    def write_external_events(self, *event_ids: str) -> Path:
        path = self.root / "events.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for event_id in event_ids:
                handle.write(json.dumps({"notify": True, "id": event_id}) + "\n")
        return path

    def checkpoint_for(self, state: dict):
        return lambda: notifier.save_state(self.state_path, state)

    def process_external(self, path: Path, state: dict, delivery: SequenceNotifier) -> int:
        return notifier.process_external_file(
            path,
            state,
            delivery,
            notifier.Logger(None),
            "Test Tool",
            self.external_trigger,
            self.checkpoint_for(state),
        )

    def test_external_failure_waits_across_restart_then_second_attempt_succeeds(self) -> None:
        path = self.write_external_events("A")
        state = self.new_state()
        first_delivery = SequenceNotifier(False)

        with mock.patch.object(notifier.time, "time", return_value=100):
            sent = self.process_external(path, state, first_delivery)

        self.assertEqual(0, sent)
        self.assertEqual(["A"], first_delivery.calls)
        self.assertEqual(0, state["files"][str(path)]["offset"])
        self.assertEqual("retry_wait", state["delivery_attempts"]["A"]["status"])
        self.assertEqual(1, state["delivery_attempts"]["A"]["attempts"])
        self.assertEqual(160, state["delivery_attempts"]["A"]["next_retry_at"])

        restarted_state = notifier.load_state(self.state_path)
        second_delivery = SequenceNotifier(True)
        with mock.patch.object(notifier.time, "time", return_value=159):
            sent = self.process_external(path, restarted_state, second_delivery)
        self.assertEqual(0, sent)
        self.assertEqual([], second_delivery.calls)
        self.assertEqual(0, restarted_state["files"][str(path)]["offset"])

        with mock.patch.object(notifier.time, "time", return_value=160):
            sent = self.process_external(path, restarted_state, second_delivery)
        self.assertEqual(1, sent)
        self.assertEqual(["A"], second_delivery.calls)
        self.assertEqual(path.stat().st_size, restarted_state["files"][str(path)]["offset"])
        self.assertIn("A", restarted_state["sent"])
        self.assertNotIn("A", restarted_state["delivery_attempts"])

        with mock.patch.object(notifier.time, "time", return_value=1_000):
            self.process_external(path, restarted_state, second_delivery)
        self.assertEqual(["A"], second_delivery.calls)

    def test_external_two_failures_become_exhausted_and_never_send_again(self) -> None:
        path = self.write_external_events("A")
        state = self.new_state()
        delivery = SequenceNotifier(False, False)

        with mock.patch.object(notifier.time, "time", return_value=100):
            self.process_external(path, state, delivery)
        with mock.patch.object(notifier.time, "time", return_value=160):
            self.process_external(path, state, delivery)

        exhausted = state["delivery_attempts"]["A"]
        self.assertEqual(["A", "A"], delivery.calls)
        self.assertEqual("exhausted", exhausted["status"])
        self.assertEqual(2, exhausted["attempts"])
        self.assertEqual("all_channels_failed", exhausted["last_result"])
        self.assertIsNone(exhausted["next_retry_at"])
        self.assertEqual(path.stat().st_size, state["files"][str(path)]["offset"])

        restarted_state = notifier.load_state(self.state_path)
        for current_time in (220, 10_000, 20_000):
            with mock.patch.object(notifier.time, "time", return_value=current_time):
                self.process_external(path, restarted_state, delivery)
        self.assertEqual(["A", "A"], delivery.calls)
        self.assertEqual("exhausted", restarted_state["delivery_attempts"]["A"]["status"])

    def test_failed_a_does_not_advance_to_b(self) -> None:
        path = self.write_external_events("A", "B")
        state = self.new_state()
        delivery = SequenceNotifier(False, False, True)

        with mock.patch.object(notifier.time, "time", return_value=100):
            self.process_external(path, state, delivery)

        self.assertEqual(["A"], delivery.calls)
        self.assertEqual(0, state["files"][str(path)]["offset"])

        with mock.patch.object(notifier.time, "time", return_value=160):
            sent = self.process_external(path, state, delivery)

        self.assertEqual(1, sent)
        self.assertEqual(["A", "A", "B"], delivery.calls)
        self.assertEqual("exhausted", state["delivery_attempts"]["A"]["status"])
        self.assertIn("B", state["sent"])
        self.assertEqual(path.stat().st_size, state["files"][str(path)]["offset"])

    def test_codex_processor_retries_failed_event(self) -> None:
        path = self.root / "rollout-test.jsonl"
        session_meta = {
            "type": "session_meta",
            "payload": {
                "id": "codex-thread",
                "cwd": "/tmp/project",
                "source": "vscode",
                "thread_source": "user",
            },
        }
        completion = {
            "timestamp": "2026-08-02T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "任务已完成。",
            },
        }
        first_line = json.dumps(session_meta) + "\n"
        path.write_text(first_line + json.dumps(completion) + "\n", encoding="utf-8")
        state = self.new_state()
        delivery = SequenceNotifier(False, True)

        with mock.patch.object(notifier.time, "time", return_value=100):
            notifier.process_file(
                path,
                state,
                delivery,
                set(),
                notifier.Logger(None),
                self.checkpoint_for(state),
            )
        self.assertEqual(len(first_line.encode("utf-8")), state["files"][str(path)]["offset"])

        with mock.patch.object(notifier.time, "time", return_value=160):
            sent = notifier.process_file(
                path,
                state,
                delivery,
                set(),
                notifier.Logger(None),
                self.checkpoint_for(state),
            )

        self.assertEqual(1, sent)
        self.assertEqual(2, len(delivery.calls))
        self.assertEqual(delivery.calls[0], delivery.calls[1])
        self.assertEqual(path.stat().st_size, state["files"][str(path)]["offset"])

    def test_zcode_processor_retries_failed_event(self) -> None:
        path = self.root / "zcode-test.jsonl"
        record = {
            "message": "ZCode Protocol background turn completed",
            "timestamp": "2026-08-02T00:00:00Z",
            "sessionId": "zcode-session",
            "durationMs": 1_500,
            "context": {
                "inputId": "input-1",
                "queryId": "query-1",
                "workspacePath": "/tmp/project",
            },
        }
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        state = self.new_state()
        delivery = SequenceNotifier(False, True)

        with mock.patch.object(notifier.time, "time", return_value=100):
            notifier.process_zcode_file(
                path,
                state,
                delivery,
                notifier.Logger(None),
                self.checkpoint_for(state),
            )
        self.assertEqual(0, state["files"][str(path)]["offset"])

        with mock.patch.object(notifier.time, "time", return_value=160):
            sent = notifier.process_zcode_file(
                path,
                state,
                delivery,
                notifier.Logger(None),
                self.checkpoint_for(state),
            )

        self.assertEqual(1, sent)
        self.assertEqual(2, len(delivery.calls))
        self.assertEqual(delivery.calls[0], delivery.calls[1])
        self.assertEqual(path.stat().st_size, state["files"][str(path)]["offset"])

    def test_duplicate_zcode_record_at_new_offset_sends_once(self) -> None:
        path = self.root / "zcode-duplicate.jsonl"
        record = {
            "message": "ZCode Protocol background turn completed",
            "timestamp": "2026-08-02T00:00:00Z",
            "sessionId": "zcode-session",
            "context": {
                "inputId": "input-1",
                "queryId": "query-1",
                "workspacePath": "/tmp/project",
            },
        }
        encoded = json.dumps(record) + "\n"
        path.write_text(encoded + encoded, encoding="utf-8")
        state = self.new_state()
        delivery = SequenceNotifier(True)

        notifier.process_zcode_file(path, state, delivery, notifier.Logger(None), self.checkpoint_for(state))

        self.assertEqual(1, len(delivery.calls))
        self.assertEqual(path.stat().st_size, state["files"][str(path)]["offset"])

    def test_duplicate_grok_record_at_new_offset_sends_once(self) -> None:
        session_dir = self.root / "grok-session"
        session_dir.mkdir()
        (session_dir / "summary.json").write_text(
            json.dumps({"info": {"id": "grok-session", "cwd": "/tmp/project"}}),
            encoding="utf-8",
        )
        (session_dir / "chat_history.jsonl").write_text(
            json.dumps({"type": "assistant", "content": "任务已完成。"}) + "\n",
            encoding="utf-8",
        )
        event = {"type": "turn_ended", "outcome": "completed", "ts": 1_785_282_889_000}
        encoded = json.dumps(event) + "\n"
        path = session_dir / "events.jsonl"
        path.write_text(encoded + encoded, encoding="utf-8")
        state = self.new_state()
        delivery = SequenceNotifier(True)

        notifier.process_external_file(
            path,
            state,
            delivery,
            notifier.Logger(None),
            "Grok Build",
            notifier.trigger_from_grok_record,
            self.checkpoint_for(state),
        )

        self.assertEqual(1, len(delivery.calls))
        self.assertEqual(path.stat().st_size, state["files"][str(path)]["offset"])

    def test_attempting_crash_recovery_never_exceeds_two_rounds(self) -> None:
        path = self.write_external_events("A")
        state = self.new_state()
        state["files"][str(path)] = {"offset": 0, "kind": "Test Tool"}
        state["delivery_attempts"]["A"] = {
            "status": "attempting",
            "attempts": 1,
            "first_attempt_at": 100,
            "last_attempt_at": 100,
            "next_retry_at": None,
            "last_result": "attempting",
            "source": "test_tool",
            "event_type": "test_turn_completed",
            "session_id": "test-session",
            "log_path": str(path),
            "line_offset": 0,
        }
        notifier.save_state(self.state_path, state)
        restarted_state = notifier.load_state(self.state_path)
        delivery = SequenceNotifier(False)

        with mock.patch.object(notifier.time, "time", return_value=159):
            self.process_external(path, restarted_state, delivery)
        self.assertEqual([], delivery.calls)
        self.assertEqual("retry_wait", restarted_state["delivery_attempts"]["A"]["status"])

        with mock.patch.object(notifier.time, "time", return_value=160):
            self.process_external(path, restarted_state, delivery)
        self.assertEqual(["A"], delivery.calls)
        self.assertEqual(2, restarted_state["delivery_attempts"]["A"]["attempts"])
        self.assertEqual("exhausted", restarted_state["delivery_attempts"]["A"]["status"])

        restarted_state["files"][str(path)]["offset"] = 0
        with mock.patch.object(notifier.time, "time", return_value=10_000):
            self.process_external(path, restarted_state, delivery)
        self.assertEqual(["A"], delivery.calls)

        second_state = self.new_state()
        second_state["files"][str(path)] = {"offset": 0, "kind": "Test Tool"}
        second_state["delivery_attempts"]["A"] = {
            "status": "attempting",
            "attempts": 2,
            "last_attempt_at": 100,
            "log_path": str(path),
            "line_offset": 0,
        }
        no_delivery = SequenceNotifier()
        with mock.patch.object(notifier.time, "time", return_value=160):
            self.process_external(path, second_state, no_delivery)
        self.assertEqual([], no_delivery.calls)
        self.assertEqual("exhausted", second_state["delivery_attempts"]["A"]["status"])
        self.assertEqual("outcome_unknown_after_restart", second_state["delivery_attempts"]["A"]["last_result"])

    def test_state_v1_migrates_and_exhausted_record_persists(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "initialized": True,
                    "files": {},
                    "sent": {"old-event": 1},
                }
            ),
            encoding="utf-8",
        )

        state = notifier.load_state(self.state_path)
        self.assertEqual(notifier.STATE_VERSION, state["version"])
        self.assertEqual({}, state["delivery_attempts"])
        self.assertEqual({}, state["delivery_stats"])

        notifier.mark_delivery_exhausted(
            state,
            "failed-event",
            {"attempts": 2, "last_attempt_at": 200},
            200,
            "all_channels_failed",
        )
        notifier.save_state(self.state_path, state)
        reloaded = notifier.load_state(self.state_path)

        self.assertEqual("exhausted", reloaded["delivery_attempts"]["failed-event"]["status"])
        self.assertEqual(2, reloaded["delivery_attempts"]["failed-event"]["attempts"])
        self.assertEqual(1, reloaded["delivery_stats"]["exhausted_total"])


if __name__ == "__main__":
    unittest.main()
