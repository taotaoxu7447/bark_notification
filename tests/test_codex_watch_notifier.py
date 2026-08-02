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

    def test_doctor_marks_invalid_state_without_replacing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            state_path.write_bytes(b"[]\n")
            args = SimpleNamespace(
                state=str(state_path),
                log=str(root / "notifier.log"),
                sessions_root=[str(root / "sessions")],
                include_archived=False,
                disable_zcode=True,
                zcode_log_root=str(root / "zcode"),
                disable_kimi=True,
                kimi_sessions_root=str(root / "kimi"),
                disable_grok=True,
                grok_sessions_root=str(root / "grok"),
                disable_claude=True,
                claude_hook_events_file=str(root / "claude.jsonl"),
            )
            output = io.StringIO()
            environment = {
                "AGENTWATCH_CONFIG_DIR": str(root / "config"),
                "CODEX_WATCH_ENV": str(root / "missing.env"),
                "CODEX_WATCH_MACOS_NOTIFICATION": "0",
            }

            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                notifier.platform, "system", return_value="Linux"
            ), contextlib.redirect_stdout(output):
                result = notifier.doctor(args, notifier.Logger(None))

            self.assertEqual(1, result)
            self.assertIn("[WARN] state data valid", output.getvalue())
            self.assertEqual(b"[]\n", state_path.read_bytes())


class ClaudeHookWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.spool = self.root / "claude-hook-events.jsonl"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def record(
        *,
        hook_event_name: str = "Stop",
        prompt_id: str = "prompt-1",
        message: str = "任务已完成。",
        error: str = "",
        has_background_tasks: bool = False,
        has_session_crons: bool = False,
        transcript_size: int = 123,
        transcript_path: str = "/tmp/claude-session.jsonl",
        received_at: int = 1_785_282_889,
        stop_hook_active: bool = False,
    ) -> dict:
        return {
            "schema": notifier.CLAUDE_HOOK_SCHEMA,
            "hook_event_name": hook_event_name,
            "session_id": "claude-session-123",
            "prompt_id": prompt_id,
            "transcript_path": transcript_path,
            "transcript_size": transcript_size,
            "cwd": "/tmp/claude-project",
            "received_at": received_at,
            "last_assistant_message": message,
            "last_assistant_message_sha256": notifier.hashlib.sha256(message.encode("utf-8")).hexdigest(),
            "error": error,
            "error_details": "429 Too Many Requests" if error else "",
            "stop_hook_active": stop_hook_active,
            "has_background_tasks": has_background_tasks,
            "has_session_crons": has_session_crons,
        }

    def write_records(self, *records: dict) -> None:
        with self.spool.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def watcher_args(self, spool: Path, *, process_existing: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            sessions_root=[str(self.root / "missing-codex-sessions")],
            include_archived=False,
            disable_zcode=True,
            zcode_log_root=str(self.root / "missing-zcode"),
            disable_kimi=True,
            kimi_sessions_root=str(self.root / "missing-kimi"),
            disable_grok=True,
            grok_sessions_root=str(self.root / "missing-grok"),
            disable_claude=False,
            claude_hook_events_file=str(spool),
            process_existing=process_existing,
            dry_run=True,
            once=True,
            poll_interval=0.5,
        )

    def test_stop_creates_generic_end_of_turn_event_with_claude_source(self) -> None:
        record = self.record()

        event = notifier.trigger_from_claude_hook_record(self.spool, 0, record)

        self.assertIsNotNone(event)
        self.assertEqual("claude_turn_completed", event["event_type"])
        self.assertEqual("Claude Code", event["bark_group"])
        self.assertEqual("claude", notifier.ntfy_source(event))
        self.assertIn("Claude Code 已结束本轮", event["notification_title"])
        self.assertNotIn("Claude Code 已完成", event["notification_title"])
        self.assertEqual(24, len(event["stable_id"]))

    def test_attention_and_stop_failure_create_attention_events(self) -> None:
        question = self.record(prompt_id="prompt-question", message="需要你确认是否继续。")
        failure = self.record(
            hook_event_name="StopFailure",
            prompt_id="prompt-failure",
            message="API Error: Rate limit reached",
            error="rate_limit",
        )

        question_event = notifier.trigger_from_claude_hook_record(self.spool, 0, question)
        failure_event = notifier.trigger_from_claude_hook_record(self.spool, 1, failure)

        self.assertEqual("claude_turn_attention", question_event["event_type"])
        self.assertEqual("需要处理", question_event["status"])
        self.assertEqual("claude_turn_attention", failure_event["event_type"])
        self.assertEqual("需要处理", failure_event["status"])
        self.assertIn("rate_limit", failure_event["status_detail"])

    def test_prompt_id_is_primary_stable_dedupe_key(self) -> None:
        first = self.record(message="first", transcript_size=100)
        changed_spool_snapshot = self.record(message="changed", transcript_size=999)
        different_prompt = self.record(prompt_id="prompt-2", message="first", transcript_size=100)

        first_event = notifier.trigger_from_claude_hook_record(self.spool, 0, first)
        changed_event = notifier.trigger_from_claude_hook_record(self.spool, 1, changed_spool_snapshot)
        different_event = notifier.trigger_from_claude_hook_record(self.spool, 2, different_prompt)

        self.assertEqual(first_event["stable_id"], changed_event["stable_id"])
        self.assertNotEqual(first_event["stable_id"], different_event["stable_id"])

    def test_fallback_stable_id_uses_normalized_hook_snapshot(self) -> None:
        first = self.record(prompt_id="", message="first", transcript_size=100)
        same = dict(first)
        changed = self.record(prompt_id="", message="second", transcript_size=100)

        first_event = notifier.trigger_from_claude_hook_record(self.spool, 0, first)
        same_event = notifier.trigger_from_claude_hook_record(self.spool, 1, same)
        changed_event = notifier.trigger_from_claude_hook_record(self.spool, 2, changed)

        self.assertEqual(first_event["stable_id"], same_event["stable_id"])
        self.assertNotEqual(first_event["stable_id"], changed_event["stable_id"])

    def test_inflight_background_or_cron_stop_is_silent(self) -> None:
        background = self.record(has_background_tasks=True)
        cron = self.record(prompt_id="prompt-2", has_session_crons=True)

        self.assertIsNone(notifier.trigger_from_claude_hook_record(self.spool, 0, background))
        self.assertIsNone(notifier.trigger_from_claude_hook_record(self.spool, 1, cron))

    def test_false_stop_settles_without_network_or_retry_then_sends_once(self) -> None:
        transcript = self.root / "settling-session.jsonl"
        transcript.write_text("initial transcript\n", encoding="utf-8")
        received_at = int(notifier.time.time())
        self.write_records(
            self.record(
                prompt_id="settle-once",
                transcript_path=str(transcript),
                transcript_size=transcript.stat().st_size,
                received_at=received_at,
                stop_hook_active=False,
            )
        )
        state = {"files": {str(self.spool): {"offset": 0}}, "sent": {}}
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        with mock.patch.object(
            notifier, "claude_stop_settle_seconds", return_value=10
        ), mock.patch.object(notifier.time, "time", return_value=received_at + 9):
            self.assertEqual(
                0,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )

        self.assertEqual([], sent_events)
        self.assertEqual(0, state["files"][str(self.spool)]["offset"])
        self.assertEqual({}, state.get("delivery_attempts", {}))

        with mock.patch.object(
            notifier, "claude_stop_settle_seconds", return_value=10
        ), mock.patch.object(notifier.time, "time", return_value=received_at + 10):
            self.assertEqual(
                1,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )
            self.assertEqual(
                0,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )

        self.assertEqual(["settle-once"], [event["prompt_id"] for event in sent_events])
        self.assertFalse(sent_events[0]["stop_hook_active"])
        self.assertEqual(
            self.spool.stat().st_size,
            state["files"][str(self.spool)]["offset"],
        )

    def test_transcript_growth_suppresses_false_then_true_stop_sends_once(self) -> None:
        transcript = self.root / "continued-session.jsonl"
        transcript.write_text("first response\n", encoding="utf-8")
        received_at = int(notifier.time.time())
        initial_size = transcript.stat().st_size
        self.write_records(
            self.record(
                prompt_id="parallel-hook-turn",
                message="First attempted stop",
                transcript_path=str(transcript),
                transcript_size=initial_size,
                received_at=received_at,
                stop_hook_active=False,
            )
        )
        state = {"files": {str(self.spool): {"offset": 0}}, "sent": {}}
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        with mock.patch.object(
            notifier, "claude_stop_settle_seconds", return_value=10
        ), mock.patch.object(notifier.time, "time", return_value=received_at + 1):
            self.assertEqual(
                0,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )

        transcript.write_text(
            "first response\ncontinuation after sibling Stop hook blocked\n",
            encoding="utf-8",
        )
        self.write_records(
            self.record(
                prompt_id="parallel-hook-turn",
                message="Final response after continuation",
                transcript_path=str(transcript),
                transcript_size=transcript.stat().st_size,
                received_at=received_at + 2,
                stop_hook_active=True,
            )
        )

        with mock.patch.object(
            notifier, "claude_stop_settle_seconds", return_value=10
        ), mock.patch.object(notifier.time, "time", return_value=received_at + 2):
            self.assertEqual(
                1,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )

        self.assertEqual(1, len(sent_events))
        self.assertEqual("parallel-hook-turn", sent_events[0]["prompt_id"])
        self.assertTrue(sent_events[0]["stop_hook_active"])
        self.assertEqual(
            1,
            state["files"][str(self.spool)][
                "claude_provisional_stops_suppressed"
            ],
        )
        self.assertEqual({}, state.get("delivery_attempts", {}))

    def test_async_transcript_flush_without_true_stop_does_not_drop_notification(self) -> None:
        transcript = self.root / "async-flush-session.jsonl"
        transcript.write_text("transcript before final flush\n", encoding="utf-8")
        received_at = int(notifier.time.time())
        captured_size = transcript.stat().st_size
        self.write_records(
            self.record(
                prompt_id="normal-final-stop",
                transcript_path=str(transcript),
                transcript_size=captured_size,
                received_at=received_at,
                stop_hook_active=False,
            )
        )
        # Claude documents transcript writes as asynchronous, so ordinary
        # final-message persistence can happen after Stop without any blocker.
        transcript.write_text(
            "transcript before final flush\nfinal assistant message flushed later\n",
            encoding="utf-8",
        )
        state = {"files": {str(self.spool): {"offset": 0}}, "sent": {}}
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        with mock.patch.object(
            notifier, "claude_stop_settle_seconds", return_value=10
        ), mock.patch.object(notifier.time, "time", return_value=received_at + 9):
            self.assertEqual(
                0,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )
        self.assertEqual([], sent_events)
        self.assertEqual(0, state["files"][str(self.spool)]["offset"])

        with mock.patch.object(
            notifier, "claude_stop_settle_seconds", return_value=10
        ), mock.patch.object(notifier.time, "time", return_value=received_at + 10):
            self.assertEqual(
                1,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )

        self.assertEqual(["normal-final-stop"], [event["prompt_id"] for event in sent_events])
        self.assertNotIn(
            "claude_provisional_stops_suppressed",
            state["files"][str(self.spool)],
        )

    def test_invalid_true_record_cannot_suppress_valid_provisional_stop(self) -> None:
        transcript = self.root / "invalid-lookahead-session.jsonl"
        transcript.write_text("done\n", encoding="utf-8")
        received_at = int(notifier.time.time())
        provisional = self.record(
            prompt_id="invalid-lookahead",
            transcript_path=str(transcript),
            transcript_size=transcript.stat().st_size,
            received_at=received_at,
            stop_hook_active=False,
        )
        invalid_true = self.record(
            prompt_id="invalid-lookahead",
            transcript_path=str(transcript),
            transcript_size=transcript.stat().st_size,
            received_at=received_at + 1,
            stop_hook_active=True,
        )
        invalid_true["last_assistant_message"] = ""
        self.write_records(provisional, invalid_true)
        state = {"files": {str(self.spool): {"offset": 0}}, "sent": {}}
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        with mock.patch.object(
            notifier, "claude_stop_settle_seconds", return_value=10
        ), mock.patch.object(notifier.time, "time", return_value=received_at + 1):
            self.assertEqual(
                0,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )
        self.assertEqual(0, state["files"][str(self.spool)]["offset"])

        with mock.patch.object(
            notifier, "claude_stop_settle_seconds", return_value=10
        ), mock.patch.object(notifier.time, "time", return_value=received_at + 10):
            self.assertEqual(
                1,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )

        self.assertEqual(["invalid-lookahead"], [event["prompt_id"] for event in sent_events])
        self.assertNotIn(
            "claude_provisional_stops_suppressed",
            state["files"][str(self.spool)],
        )

    def test_true_stop_and_stop_failure_do_not_wait_for_settle_window(self) -> None:
        transcript = self.root / "immediate-session.jsonl"
        transcript.write_text("done\n", encoding="utf-8")
        now = int(notifier.time.time())
        self.write_records(
            self.record(
                prompt_id="continuation-final",
                transcript_path=str(transcript),
                transcript_size=transcript.stat().st_size,
                received_at=now,
                stop_hook_active=True,
            ),
            self.record(
                hook_event_name="StopFailure",
                prompt_id="api-failure",
                message="API Error: overloaded",
                error="overloaded",
                transcript_path=str(transcript),
                transcript_size=transcript.stat().st_size,
                received_at=now,
            ),
        )
        state = {"files": {str(self.spool): {"offset": 0}}, "sent": {}}
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        with mock.patch.object(notifier.time, "time", return_value=now):
            self.assertEqual(
                2,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )

        self.assertEqual(
            ["continuation-final", "api-failure"],
            [event["prompt_id"] for event in sent_events],
        )

    def test_matching_stop_failure_bypasses_an_earlier_provisional_stop(self) -> None:
        transcript = self.root / "failed-continuation-session.jsonl"
        transcript.write_text("first response\n", encoding="utf-8")
        now = int(notifier.time.time())
        common = {
            "prompt_id": "continuation-api-failure",
            "transcript_path": str(transcript),
            "transcript_size": transcript.stat().st_size,
        }
        self.write_records(
            self.record(
                **common,
                message="First attempted stop",
                received_at=now,
                stop_hook_active=False,
            ),
            self.record(
                **common,
                hook_event_name="StopFailure",
                message="API Error: overloaded",
                error="overloaded",
                received_at=now + 1,
            ),
        )
        state = {"files": {str(self.spool): {"offset": 0}}, "sent": {}}
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        with mock.patch.object(notifier.time, "time", return_value=now + 1):
            self.assertEqual(
                1,
                notifier.process_external_file(
                    self.spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                ),
            )

        self.assertEqual(["claude_turn_attention"], [event["event_type"] for event in sent_events])
        self.assertEqual(
            1,
            state["files"][str(self.spool)][
                "claude_provisional_stops_suppressed"
            ],
        )

    def test_stop_settle_window_has_documented_minimum_and_maximum(self) -> None:
        with mock.patch.dict(
            os.environ, {"CLAUDE_WATCH_STOP_SETTLE_SECONDS": ""}, clear=False
        ):
            self.assertEqual(35, notifier.claude_stop_settle_seconds())
        with mock.patch.dict(
            os.environ, {"CLAUDE_WATCH_STOP_SETTLE_SECONDS": "1"}, clear=False
        ):
            self.assertEqual(5, notifier.claude_stop_settle_seconds())
        with mock.patch.dict(
            os.environ, {"CLAUDE_WATCH_STOP_SETTLE_SECONDS": "9999"}, clear=False
        ):
            self.assertEqual(600, notifier.claude_stop_settle_seconds())

    def test_first_baseline_does_not_replay_and_new_hook_is_sent_once(self) -> None:
        old_record = self.record(prompt_id="old")
        new_record = self.record(prompt_id="new")
        self.write_records(old_record)
        state: dict = {"files": {}, "sent": {}}
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        notifier.initialize_claude_spool(
            state,
            self.spool,
            process_existing=False,
            log=notifier.Logger(None),
        )
        with mock.patch.dict(os.environ, {"CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0"}):
            notifier.process_external_file(
                self.spool,
                state,
                RecordingNotifier(),
                notifier.Logger(None),
                "Claude Code",
                notifier.trigger_from_claude_hook_record,
            )
            self.write_records(new_record)
            notifier.process_external_file(
                self.spool,
                state,
                RecordingNotifier(),
                notifier.Logger(None),
                "Claude Code",
                notifier.trigger_from_claude_hook_record,
            )
            notifier.process_external_file(
                self.spool,
                state,
                RecordingNotifier(),
                notifier.Logger(None),
                "Claude Code",
                notifier.trigger_from_claude_hook_record,
            )

        self.assertEqual(str(self.spool), state["claude_initialized"])
        self.assertEqual(["new"], [event["prompt_id"] for event in sent_events])
        self.assertEqual(self.spool.stat().st_size, state["files"][str(self.spool)]["offset"])

    def test_spool_age_defaults_to_24_hours_and_enforces_one_hour_minimum(self) -> None:
        with mock.patch.dict(
            os.environ, {"CLAUDE_WATCH_SPOOL_MAX_AGE_SECONDS": ""}, clear=False
        ):
            self.assertEqual(24 * 60 * 60, notifier.claude_spool_max_age_seconds())
        with mock.patch.dict(
            os.environ, {"CLAUDE_WATCH_SPOOL_MAX_AGE_SECONDS": "1"}, clear=False
        ):
            self.assertEqual(60 * 60, notifier.claude_spool_max_age_seconds())

    def test_existing_spool_start_time_uses_file_time_and_persists(self) -> None:
        self.write_records(self.record(prompt_id="existing"))
        now = int(notifier.time.time())
        old_file_time = now - (2 * 24 * 60 * 60)
        os.utime(self.spool, (old_file_time, old_file_time))
        state = {"files": {}, "sent": {}}

        with mock.patch.object(notifier.time, "time", return_value=now):
            notifier.initialize_claude_spool(
                state,
                self.spool,
                process_existing=False,
                log=notifier.Logger(None),
            )
        recorded = state["files"][str(self.spool)]["claude_spool_started_at"]
        self.assertGreater(recorded, 0)
        self.assertLessEqual(recorded, old_file_time)

        state_path = self.root / "persistent-start-state.json"
        notifier.save_state(state_path, state)
        reloaded = notifier.load_state(state_path)
        self.write_records(self.record(prompt_id="later-append-must-not-reset-ttl"))
        with mock.patch.object(notifier.time, "time", return_value=now + 10_000):
            notifier.initialize_claude_spool(
                reloaded,
                self.spool,
                process_existing=False,
                log=notifier.Logger(None),
            )
        self.assertEqual(
            recorded,
            reloaded["files"][str(self.spool)]["claude_spool_started_at"],
        )

    def test_empty_live_spool_starts_ttl_when_its_first_record_arrives(self) -> None:
        self.spool.write_bytes(b"")
        state = {"files": {}, "sent": {}}
        now = int(notifier.time.time())
        with mock.patch.object(notifier.time, "time", return_value=now):
            notifier.initialize_claude_spool(
                state,
                self.spool,
                process_existing=False,
                log=notifier.Logger(None),
            )
        self.assertEqual(0, state["files"][str(self.spool)]["claude_spool_started_at"])

        self.write_records(self.record(prompt_id="first-live-record"))
        with mock.patch.object(notifier.time, "time", return_value=now + 1):
            notifier.initialize_claude_spool(
                state,
                self.spool,
                process_existing=False,
                log=notifier.Logger(None),
            )
        self.assertGreater(
            state["files"][str(self.spool)]["claude_spool_started_at"], 0
        )

    def test_fully_consumed_spool_rotates_when_24_hour_ttl_expires(self) -> None:
        self.write_records(self.record(prompt_id="privacy-ttl"))
        size = self.spool.stat().st_size
        now = int(notifier.time.time())
        state = {
            "claude_initialized": str(self.spool),
            "files": {
                str(self.spool): {
                    "offset": size,
                    "size": size,
                    "kind": "Claude Code",
                    "claude_spool_started_at": now - (24 * 60 * 60),
                }
            },
            "sent": {},
        }

        with mock.patch.object(notifier.time, "time", return_value=now), mock.patch.object(
            notifier, "claude_spool_max_bytes", return_value=size + 1
        ), mock.patch.object(
            notifier, "claude_spool_max_age_seconds", return_value=24 * 60 * 60
        ):
            rotated = notifier.rotate_consumed_claude_spool(
                self.spool, state, notifier.Logger(None)
            )

        self.assertTrue(rotated)
        self.assertEqual(0, self.spool.stat().st_size)
        self.assertEqual(0, state["files"][str(self.spool)]["claude_spool_started_at"])
        self.assertEqual(1, len(notifier.claude_drain_files(self.spool)))

    def test_ttl_never_rotates_unread_active_or_already_draining_spool(self) -> None:
        self.write_records(self.record(prompt_id="protected"))
        original = self.spool.read_bytes()
        size = len(original)
        now = int(notifier.time.time())
        rec = {
            "offset": size - 1,
            "size": size,
            "kind": "Claude Code",
            "claude_spool_started_at": now - (2 * 24 * 60 * 60),
        }
        state = {"files": {str(self.spool): rec}, "sent": {}}

        with mock.patch.object(notifier.time, "time", return_value=now), mock.patch.object(
            notifier, "claude_spool_max_bytes", return_value=size + 1
        ), mock.patch.object(
            notifier, "claude_spool_max_age_seconds", return_value=24 * 60 * 60
        ):
            self.assertFalse(
                notifier.rotate_consumed_claude_spool(
                    self.spool, state, notifier.Logger(None)
                )
            )
            rec["offset"] = size
            state["delivery_attempts"] = {
                "pending": {
                    "status": "retry_wait",
                    "log_path": str(self.spool),
                    "line_offset": 0,
                }
            }
            self.assertFalse(
                notifier.rotate_consumed_claude_spool(
                    self.spool, state, notifier.Logger(None)
                )
            )
            state["delivery_attempts"] = {}
            drain = self.spool.parent / (
                f"{notifier.claude_drain_prefix(self.spool)}0-{notifier.time.time_ns()}-deadbeef"
            )
            drain.write_bytes(b"")
            state["files"][str(drain)] = {
                "offset": 0,
                "size": 0,
                "kind": "Claude Code",
                "claude_drain": True,
                "claude_spool_path": str(self.spool),
                "file_identity": notifier.file_identity(drain),
            }
            self.assertFalse(
                notifier.rotate_consumed_claude_spool(
                    self.spool, state, notifier.Logger(None)
                )
            )

        self.assertEqual(original, self.spool.read_bytes())

    def test_switching_configured_spool_path_baselines_that_path_before_delivery(self) -> None:
        old_spool = self.root / "old-events.jsonl"
        new_spool = self.root / "new-events.jsonl"
        old_spool.write_text(json.dumps(self.record(prompt_id="old-path")) + "\n", encoding="utf-8")
        new_spool.write_text(json.dumps(self.record(prompt_id="queued-before-switch")) + "\n", encoding="utf-8")
        state_path = self.root / "state.json"
        notifier.save_state(
            state_path,
            {
                "initialized": True,
                "claude_initialized": str(old_spool),
                "files": {
                    str(old_spool): {
                        "offset": old_spool.stat().st_size,
                        "size": old_spool.stat().st_size,
                        "kind": "Claude Code",
                    }
                },
                "sent": {},
            },
        )
        sent_events = []

        class RecordingNotifier:
            channels = ["test"]

            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        with mock.patch.object(notifier, "Notifier", return_value=RecordingNotifier()), mock.patch.dict(
            os.environ, {"CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0"}
        ):
            self.assertEqual(
                0,
                notifier.run_watcher(
                    self.watcher_args(new_spool), notifier.Logger(None), state_path
                ),
            )
            switched = notifier.load_state(state_path)
            self.assertEqual(str(new_spool), switched["claude_initialized"])
            self.assertEqual(new_spool.stat().st_size, switched["files"][str(new_spool)]["offset"])
            self.assertEqual([], sent_events)

            with new_spool.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self.record(prompt_id="after-switch")) + "\n")
            self.assertEqual(
                0,
                notifier.run_watcher(
                    self.watcher_args(new_spool), notifier.Logger(None), state_path
                ),
            )

        self.assertEqual(["after-switch"], [event["prompt_id"] for event in sent_events])

    def test_consumed_spool_rotation_drains_a_writer_opened_before_rename(self) -> None:
        first = self.record(prompt_id="before-rotation")
        self.write_records(first)
        original_size = self.spool.stat().st_size
        state = {
            "claude_initialized": str(self.spool),
            "files": {
                str(self.spool): {
                    "offset": original_size,
                    "size": original_size,
                    "kind": "Claude Code",
                }
            },
            "sent": {},
        }
        sent_events = []

        class RecordingNotifier:
            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        late_writer = self.spool.open("ab", buffering=0)
        try:
            with mock.patch.object(notifier, "claude_spool_max_bytes", return_value=1):
                self.assertTrue(
                    notifier.rotate_consumed_claude_spool(
                        self.spool, state, notifier.Logger(None)
                    )
                )
            drains = notifier.claude_drain_files(self.spool)
            self.assertEqual(1, len(drains))
            self.assertEqual(0, self.spool.stat().st_size)

            late_record = self.record(prompt_id="late-old-inode")
            late_writer.write((json.dumps(late_record) + "\n").encode("utf-8"))
        finally:
            late_writer.close()

        with mock.patch.dict(os.environ, {"CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0"}):
            notifier.process_external_file(
                drains[0],
                state,
                RecordingNotifier(),
                notifier.Logger(None),
                "Claude Code",
                notifier.trigger_from_claude_hook_record,
            )
            notifier.process_external_file(
                drains[0],
                state,
                RecordingNotifier(),
                notifier.Logger(None),
                "Claude Code",
                notifier.trigger_from_claude_hook_record,
            )

        self.assertEqual(["late-old-inode"], [event["prompt_id"] for event in sent_events])
        self.assertEqual(drains[0].stat().st_size, state["files"][str(drains[0])]["offset"])

        # A changed size starts a new safety window; only the next stable check
        # can retire the drain. This bounds consumed retention without deleting
        # bytes that arrived through the old open file descriptor.
        with mock.patch.object(notifier, "claude_drain_grace_seconds", return_value=0):
            self.assertFalse(
                notifier.retire_stable_claude_drain(drains[0], state, notifier.Logger(None))
            )
            self.assertTrue(
                notifier.retire_stable_claude_drain(drains[0], state, notifier.Logger(None))
            )
        self.assertFalse(drains[0].exists())

    def test_watcher_rotates_consumed_spool_and_does_not_resend_retained_history(self) -> None:
        self.write_records(self.record(prompt_id="rotate-through-watcher"))
        state_path = self.root / "rotation-state.json"
        notifier.save_state(
            state_path,
            {
                "initialized": True,
                "claude_initialized": str(self.spool),
                "files": {
                    str(self.spool): {
                        "offset": 0,
                        "size": self.spool.stat().st_size,
                        "kind": "Claude Code",
                    }
                },
                "sent": {},
            },
        )
        sent_events = []

        class RecordingNotifier:
            channels = ["test"]

            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        patches = (
            mock.patch.object(notifier, "Notifier", return_value=RecordingNotifier()),
            mock.patch.object(notifier, "claude_spool_max_bytes", return_value=1),
            mock.patch.dict(os.environ, {"CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0"}),
        )
        with patches[0], patches[1], patches[2]:
            self.assertEqual(
                0,
                notifier.run_watcher(
                    self.watcher_args(self.spool), notifier.Logger(None), state_path
                ),
            )
            self.assertEqual(1, len(notifier.claude_drain_files(self.spool)))
            self.assertEqual(0, self.spool.stat().st_size)
            self.assertEqual(
                0,
                notifier.run_watcher(
                    self.watcher_args(self.spool), notifier.Logger(None), state_path
                ),
            )

        self.assertEqual(
            ["rotate-through-watcher"],
            [event["prompt_id"] for event in sent_events],
        )

    def test_oversized_spool_is_not_rotated_while_any_record_is_unread(self) -> None:
        first_line = (json.dumps(self.record(prompt_id="consumed")) + "\n").encode("utf-8")
        second_line = (json.dumps(self.record(prompt_id="unread")) + "\n").encode("utf-8")
        self.spool.write_bytes(first_line + second_line)
        original = self.spool.read_bytes()
        state = {
            "files": {
                str(self.spool): {
                    "offset": len(first_line),
                    "size": len(original),
                    "kind": "Claude Code",
                }
            },
            "sent": {},
        }

        with mock.patch.object(notifier, "claude_spool_max_bytes", return_value=1):
            rotated = notifier.rotate_consumed_claude_spool(
                self.spool, state, notifier.Logger(None)
            )

        self.assertFalse(rotated)
        self.assertEqual(original, self.spool.read_bytes())
        self.assertEqual([], notifier.claude_drain_files(self.spool))

    def test_test_claude_cli_routes_to_external_test_sender(self) -> None:
        missing_env = self.root / "missing.env"
        with mock.patch.object(notifier, "default_env_path", return_value=missing_env), mock.patch.object(
            notifier.sys, "argv", ["codex_watch_notifier.py", "--dry-run", "--test-claude"]
        ), mock.patch.object(notifier, "send_external_test_notification", return_value=0) as sender:
            result = notifier.main()

        self.assertEqual(0, result)
        args, _log, tool_name, prefix = sender.call_args.args
        self.assertTrue(args.dry_run)
        self.assertEqual("Claude Code", tool_name)
        self.assertEqual("claude", prefix)

    def test_default_spool_tracks_custom_agentwatch_config_directory(self) -> None:
        args = SimpleNamespace(claude_hook_events_file=None)
        environment = {
            "AGENTWATCH_CONFIG_DIR": str(self.root / "custom-config"),
            "CLAUDE_WATCH_EVENTS_FILE": "",
            "CODEX_WATCH_CONFIG_DIR": "",
        }

        with mock.patch.dict(os.environ, environment, clear=False):
            path = notifier.build_claude_hook_events_file(args)

        expected = Path(os.path.abspath(self.root / "custom-config" / "claude-hook-events.jsonl"))
        self.assertEqual(expected, path)

    def test_claude_spool_symlink_is_not_followed(self) -> None:
        real_spool = self.root / "outside.jsonl"
        real_spool.write_text(json.dumps(self.record()) + "\n", encoding="utf-8")
        linked_spool = self.root / "linked.jsonl"
        try:
            linked_spool.symlink_to(real_spool)
        except OSError:
            self.skipTest("symlink creation is unavailable on this platform")

        args = self.watcher_args(linked_spool, process_existing=True)
        state_path = self.root / "symlink-state.json"
        notifier.save_state(state_path, {"initialized": True, "files": {}, "sent": {}})
        sent_events = []

        class RecordingNotifier:
            channels = ["test"]

            def send(self, title: str, body: str, event: dict) -> bool:
                del title, body
                sent_events.append(event)
                return True

        built_path = notifier.build_claude_hook_events_file(args)
        self.assertEqual(Path(os.path.abspath(linked_spool)), built_path)
        self.assertNotEqual(real_spool.resolve(), built_path)
        with mock.patch.object(notifier, "Notifier", return_value=RecordingNotifier()), mock.patch.dict(
            os.environ, {"CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0"}
        ):
            self.assertEqual(0, notifier.run_watcher(args, notifier.Logger(None), state_path))

        self.assertEqual([], sent_events)
        self.assertEqual([], notifier.claude_hook_event_files(built_path))


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


class WatcherPoisonAndStateSafetyTests(unittest.TestCase):
    class RecordingNotifier:
        def __init__(self) -> None:
            self.events = []

        def send(self, title: str, body: str, event: dict) -> bool:
            del title, body
            self.events.append(event)
            return True

    def test_codex_and_zcode_poison_lines_do_not_block_later_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rollout = root / "rollout-poison.jsonl"
            codex_records = [
                [],
                {"type": "session_meta", "payload": ["invalid"]},
                {
                    "type": "session_meta",
                    "payload": {"id": "safe-thread", "cwd": str(root)},
                },
                {"type": "event_msg", "payload": ["invalid"]},
                {
                    "timestamp": "2026-08-03T00:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "safe-turn",
                        "last_agent_message": "任务已完成。",
                    },
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in codex_records),
                encoding="utf-8",
            )
            codex_delivery = self.RecordingNotifier()
            codex_state = {"files": {}, "sent": {}}
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0",
                    "CODEX_SESSION_INDEX": str(root / "missing-index.jsonl"),
                },
            ):
                sent = notifier.process_file(
                    rollout,
                    codex_state,
                    codex_delivery,
                    set(),
                    notifier.Logger(None),
                )
            self.assertEqual(1, sent)
            self.assertEqual(["safe-turn"], [event["turn_id"] for event in codex_delivery.events])
            self.assertEqual(1, codex_state["files"][str(rollout)]["invalid_records_skipped"])

            zcode = root / "zcode-poison.jsonl"
            zcode_records = [
                [],
                {
                    "message": "ZCode Protocol background turn completed",
                    "timestamp": "2026-08-03T00:00:00Z",
                    "sessionId": "safe-zcode",
                    "context": ["invalid"],
                },
                {
                    "message": "ZCode Protocol background turn completed",
                    "timestamp": "2026-08-03T00:00:00Z",
                    "sessionId": "safe-zcode",
                    "context": {
                        "inputId": "input-safe",
                        "queryId": "query-safe",
                        "workspacePath": str(root),
                    },
                },
            ]
            zcode.write_text(
                "".join(json.dumps(record) + "\n" for record in zcode_records),
                encoding="utf-8",
            )
            zcode_delivery = self.RecordingNotifier()
            zcode_state = {"files": {}, "sent": {}}
            sent = notifier.process_zcode_file(
                zcode,
                zcode_state,
                zcode_delivery,
                notifier.Logger(None),
            )
            self.assertEqual(1, sent)
            self.assertEqual(["query-safe"], [event["query_id"] for event in zcode_delivery.events])
            self.assertEqual(1, zcode_state["files"][str(zcode)]["invalid_records_skipped"])

    def test_non_object_state_is_rejected_without_rewriting_or_resetting_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            original = b"[]\n"
            state_path.write_bytes(original)

            with self.assertRaises(notifier.StateFileError):
                notifier.load_state(state_path)

            self.assertEqual(original, state_path.read_bytes())

    def test_non_utf8_private_env_is_rejected_instead_of_using_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / "env"
            env_path.write_bytes(b"AGENTWATCH_API_BASE=https://example.test/\xff\n")

            with self.assertRaises(notifier.ConfigFileError):
                notifier.load_env_file(env_path)

    def test_poisoned_true_stop_cannot_suppress_a_valid_provisional_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spool = root / "claude.jsonl"
            now = 1_785_282_889
            message = "任务已完成。"
            base = {
                "schema": notifier.CLAUDE_HOOK_SCHEMA,
                "hook_event_name": "Stop",
                "session_id": "claude-safe-session",
                "prompt_id": "claude-safe-prompt",
                "transcript_path": str(root / "transcript.jsonl"),
                "transcript_size": -1,
                "cwd": str(root),
                "received_at": now,
                "last_assistant_message": message,
                "last_assistant_message_sha256": notifier.hashlib.sha256(
                    message.encode("utf-8")
                ).hexdigest(),
                "error": "",
                "error_details": "",
                "stop_hook_active": False,
                "has_background_tasks": False,
                "has_session_crons": False,
            }
            poisoned_true = dict(base, stop_hook_active=True, received_at=False)
            spool.write_text(
                json.dumps(base, ensure_ascii=False)
                + "\n"
                + json.dumps(poisoned_true, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            state = {"files": {}, "sent": {}}
            delivery = self.RecordingNotifier()
            with mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_WATCH_STOP_SETTLE_SECONDS": "5",
                    "CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0",
                },
            ), mock.patch.object(notifier.time, "time", return_value=now + 6):
                sent = notifier.process_external_file(
                    spool,
                    state,
                    delivery,
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                )

            self.assertEqual(1, sent)
            self.assertEqual(["claude-safe-prompt"], [event["prompt_id"] for event in delivery.events])
            self.assertEqual(
                0,
                state["files"][str(spool)].get(
                    "claude_provisional_stops_suppressed", 0
                ),
            )


if __name__ == "__main__":
    unittest.main()
