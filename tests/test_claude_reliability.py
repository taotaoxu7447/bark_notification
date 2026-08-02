from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import agentwatch
import claude_hook_config as hook_config
import codex_watch_notifier as notifier


def hook_record(prompt_id: str, *, received_at: int = 1_785_282_889) -> dict:
    message = f"Claude result for {prompt_id}"
    return {
        "schema": notifier.CLAUDE_HOOK_SCHEMA,
        "hook_event_name": "Stop",
        "session_id": "claude-session-reliability",
        "prompt_id": prompt_id,
        "transcript_path": "/tmp/claude-session.jsonl",
        "transcript_size": 123,
        "cwd": "/tmp/claude-project",
        "received_at": received_at,
        "last_assistant_message": message,
        "last_assistant_message_sha256": notifier.hashlib.sha256(
            message.encode("utf-8")
        ).hexdigest(),
        "error": "",
        "error_details": "",
        "has_background_tasks": False,
        "has_session_crons": False,
    }


def watcher_args(root: Path, spool: Path) -> SimpleNamespace:
    return SimpleNamespace(
        sessions_root=[str(root / "missing-codex")],
        include_archived=False,
        disable_zcode=True,
        zcode_log_root=str(root / "missing-zcode"),
        disable_kimi=True,
        kimi_sessions_root=str(root / "missing-kimi"),
        disable_grok=True,
        grok_sessions_root=str(root / "missing-grok"),
        disable_claude=False,
        claude_hook_events_file=str(spool),
        process_existing=False,
        dry_run=True,
        once=True,
        poll_interval=0.5,
    )


class ClaudeHookAppendReliabilityTests(unittest.TestCase):
    def valid_hook_payload(self, root: Path, prompt_id: str, message: str = "done") -> dict:
        transcript = root / "session.jsonl"
        transcript.touch(exist_ok=True)
        return {
            "session_id": "session-concurrent",
            "prompt_id": prompt_id,
            "transcript_path": str(transcript),
            "cwd": str(root),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": message,
            "background_tasks": [],
            "session_crons": [],
        }

    def test_ancestor_symlink_is_silently_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()
            target = outside / "events.jsonl"
            target.write_text("keep intact\n", encoding="utf-8")
            linked_parent = root / "linked"
            try:
                linked_parent.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable on this platform")
            spool = linked_parent / "events.jsonl"

            with mock.patch.object(
                agentwatch.sys,
                "stdin",
                mock.Mock(read=lambda _limit=-1: json.dumps(
                    self.valid_hook_payload(root, "ancestor-link")
                )),
            ):
                result = agentwatch.main(["claude-hook", "--events-file", str(spool)])

            self.assertEqual(0, result)
            self.assertEqual("keep intact\n", target.read_text(encoding="utf-8"))
            self.assertFalse((outside / "events.jsonl.append.lock").exists())

    def test_concurrent_hook_processes_append_complete_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spool = root / "private" / "events.jsonl"
            script = Path(agentwatch.__file__).resolve()
            count = 20

            def invoke(index: int) -> subprocess.CompletedProcess[str]:
                payload = self.valid_hook_payload(
                    root,
                    f"prompt-{index}",
                    message=f"{index}:" + ("并发内容" * 8_000),
                )
                return subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "claude-hook",
                        "--events-file",
                        str(spool),
                    ],
                    input=json.dumps(payload, ensure_ascii=False),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    check=False,
                )

            with ThreadPoolExecutor(max_workers=count) as executor:
                results = list(executor.map(invoke, range(count)))

            self.assertTrue(all(result.returncode == 0 for result in results))
            self.assertTrue(all(result.stdout == "" for result in results))
            self.assertTrue(all(result.stderr == "" for result in results))
            lines = spool.read_text(encoding="utf-8").splitlines()
            self.assertEqual(count, len(lines))
            records = [json.loads(line) for line in lines]
            self.assertEqual(
                {f"prompt-{index}" for index in range(count)},
                {record["prompt_id"] for record in records},
            )
            self.assertEqual(0o600, stat.S_IMODE(spool.stat().st_mode))


class ClaudeWatcherReliabilityTests(unittest.TestCase):
    def test_ancestor_symlink_spool_is_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()
            target = outside / "events.jsonl"
            target.write_text(json.dumps(hook_record("must-not-send")) + "\n", encoding="utf-8")
            linked_parent = root / "linked"
            try:
                linked_parent.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable on this platform")
            spool = linked_parent / "events.jsonl"
            state_path = root / "state.json"
            notifier.save_state(
                state_path,
                {
                    "initialized": True,
                    "claude_initialized": str(spool),
                    "files": {str(spool): {"offset": 0, "kind": "Claude Code"}},
                    "sent": {},
                },
            )
            sent: list[dict] = []

            class RecordingNotifier:
                channels = ["test"]

                def send(self, title: str, body: str, event: dict) -> bool:
                    del title, body
                    sent.append(event)
                    return True

            with mock.patch.object(notifier, "Notifier", return_value=RecordingNotifier()):
                result = notifier.run_watcher(
                    watcher_args(root, spool), notifier.Logger(None), state_path
                )

            self.assertEqual(0, result)
            self.assertEqual([], sent)
            self.assertEqual([], notifier.claude_hook_event_files(spool))

    def test_poison_records_and_huge_timestamp_do_not_block_later_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spool = root / "events.jsonl"
            records: list[object] = [
                ["non-object"],
                hook_record("huge-time", received_at=10**400),
                hook_record("trigger-raises"),
                hook_record("good"),
            ]
            with spool.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            state = {"files": {str(spool): {"offset": 0}}, "sent": {}}
            sent: list[dict] = []

            class RecordingNotifier:
                def send(self, title: str, body: str, event: dict) -> bool:
                    del title, body
                    sent.append(event)
                    return True

            def trigger(path: Path, offset: int, record: dict) -> dict | None:
                if record.get("prompt_id") == "trigger-raises":
                    raise OverflowError("poisoned timestamp parser")
                return notifier.trigger_from_claude_hook_record(path, offset, record)

            with mock.patch.dict(os.environ, {"CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0"}):
                count = notifier.process_external_file(
                    spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    trigger,
                )

            self.assertEqual(2, count)
            self.assertEqual(["huge-time", "good"], [event["prompt_id"] for event in sent])
            self.assertEqual(spool.stat().st_size, state["files"][str(spool)]["offset"])
            self.assertEqual(1, state["files"][str(spool)]["invalid_records_skipped"])

    def test_malformed_drain_prefix_is_ignored_and_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spool = root / "events.jsonl"
            spool.write_text(json.dumps(hook_record("consumed")) + "\n", encoding="utf-8")
            malformed = root / f"{notifier.claude_drain_prefix(spool)}not-a-valid-drain"
            malformed.write_text("foreign data\n", encoding="utf-8")
            size = spool.stat().st_size
            state = {
                "files": {
                    str(spool): {
                        "offset": size,
                        "size": size,
                        "kind": "Claude Code",
                        "file_identity": notifier.file_identity(spool),
                    }
                },
                "sent": {},
            }

            with mock.patch.object(notifier, "claude_spool_max_bytes", return_value=1):
                self.assertTrue(
                    notifier.rotate_consumed_claude_spool(
                        spool, state, notifier.Logger(None)
                    )
                )

            self.assertTrue(malformed.exists())
            self.assertNotIn(malformed, notifier.claude_drain_files(spool))

    def test_replaced_drain_is_not_read_deleted_or_allowed_to_block_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spool = root / "events.jsonl"
            spool.write_text(json.dumps(hook_record("first")) + "\n", encoding="utf-8")
            size = spool.stat().st_size
            state = {
                "claude_initialized": str(spool),
                "files": {
                    str(spool): {
                        "offset": size,
                        "size": size,
                        "kind": "Claude Code",
                        "file_identity": notifier.file_identity(spool),
                    }
                },
                "sent": {},
            }
            with mock.patch.object(notifier, "claude_spool_max_bytes", return_value=1):
                self.assertTrue(
                    notifier.rotate_consumed_claude_spool(spool, state, notifier.Logger(None))
                )
            foreign_drain = notifier.claude_drain_files(spool)[0]
            replacement = root / "replacement.tmp"
            replacement.write_text(
                json.dumps(hook_record("foreign-must-not-send")) + "\n",
                encoding="utf-8",
            )
            os.replace(replacement, foreign_drain)

            notifier.initialize_claude_spool(
                state, spool, process_existing=False, log=notifier.Logger(None)
            )
            self.assertTrue(state["files"][str(foreign_drain)]["foreign_replacement"])
            self.assertEqual([], notifier.owned_claude_drain_files(spool, state))
            self.assertFalse(
                notifier.retire_stable_claude_drain(
                    foreign_drain, state, notifier.Logger(None)
                )
            )
            self.assertTrue(foreign_drain.exists())

            spool.write_text(json.dumps(hook_record("second")) + "\n", encoding="utf-8")
            live = state["files"][str(spool)]
            live.update(
                {
                    "offset": spool.stat().st_size,
                    "size": spool.stat().st_size,
                    "file_identity": notifier.file_identity(spool),
                }
            )
            with mock.patch.object(notifier, "claude_spool_max_bytes", return_value=1):
                self.assertTrue(
                    notifier.rotate_consumed_claude_spool(spool, state, notifier.Logger(None))
                )
            self.assertTrue(foreign_drain.exists())
            self.assertEqual(1, len(notifier.owned_claude_drain_files(spool, state)))

    def test_live_inode_replacement_baselines_new_file_and_abandons_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spool = root / "events.jsonl"
            spool.write_text(json.dumps(hook_record("old")) + "\n", encoding="utf-8")
            old_identity = notifier.file_identity(spool)
            state = {
                "claude_initialized": str(spool),
                "files": {
                    str(spool): {
                        "offset": 0,
                        "size": spool.stat().st_size,
                        "kind": "Claude Code",
                        "file_identity": old_identity,
                    }
                },
                "delivery_attempts": {
                    "old-event": {
                        "status": "retry_wait",
                        "log_path": str(spool),
                        "next_retry_at": 9999999999,
                    }
                },
                "sent": {},
            }
            replacement = root / "new-events.tmp"
            replacement.write_text(
                json.dumps(hook_record("queued-replacement")) + "\n", encoding="utf-8"
            )
            os.replace(replacement, spool)
            self.assertNotEqual(old_identity, notifier.file_identity(spool))

            notifier.initialize_claude_spool(
                state, spool, process_existing=False, log=notifier.Logger(None)
            )
            self.assertEqual(
                spool.stat().st_size, state["files"][str(spool)]["offset"]
            )
            attempt = state["delivery_attempts"]["old-event"]
            self.assertEqual("exhausted", attempt["status"])
            self.assertEqual("abandoned_source_change", attempt["last_result"])
            sent: list[dict] = []

            class RecordingNotifier:
                def send(self, title: str, body: str, event: dict) -> bool:
                    del title, body
                    sent.append(event)
                    return True

            with mock.patch.dict(os.environ, {"CODEX_WATCH_MAX_EVENT_AGE_SECONDS": "0"}):
                notifier.process_external_file(
                    spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                )
                with spool.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(hook_record("after-replacement")) + "\n")
                notifier.process_external_file(
                    spool,
                    state,
                    RecordingNotifier(),
                    notifier.Logger(None),
                    "Claude Code",
                    notifier.trigger_from_claude_hook_record,
                )

            self.assertEqual(["after-replacement"], [event["prompt_id"] for event in sent])

    def test_path_switch_abandons_retries_for_old_live_and_drain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_spool = root / "old-events.jsonl"
            new_spool = root / "new-events.jsonl"
            old_spool.write_text("", encoding="utf-8")
            new_spool.write_text("", encoding="utf-8")
            old_drain = root / (
                f"{notifier.claude_drain_prefix(old_spool)}0-123456789-deadbeef"
            )
            old_drain.write_text("", encoding="utf-8")
            state = {
                "claude_initialized": str(old_spool),
                "files": {
                    str(old_spool): {"offset": 0, "kind": "Claude Code"},
                    str(old_drain): {
                        "offset": 0,
                        "kind": "Claude Code",
                        "claude_drain": True,
                        "claude_spool_path": str(old_spool),
                    },
                },
                "delivery_attempts": {
                    "live": {"status": "attempting", "log_path": str(old_spool)},
                    "drain": {"status": "retry_wait", "log_path": str(old_drain)},
                },
                "sent": {},
            }

            notifier.initialize_claude_spool(
                state, new_spool, process_existing=False, log=notifier.Logger(None)
            )

            self.assertEqual("exhausted", state["delivery_attempts"]["live"]["status"])
            self.assertEqual("exhausted", state["delivery_attempts"]["drain"]["status"])
            self.assertEqual(
                2, state["delivery_stats"]["abandoned_source_changes"]
            )


class ClaudeManagedSettingsReliabilityTests(unittest.TestCase):
    def test_unreadable_managed_dropin_directory_is_reported_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / ".claude" / "settings.json"
            backup = root / "backup" / "settings.json"
            managed = root / "managed" / "managed-settings.json"
            drop_ins = managed.parent / "managed-settings.d"
            drop_ins.mkdir(parents=True)
            desired = hook_config.build_claude_hook_handler(
                root / "python", root / "agentwatch.py", root / "events.jsonl"
            )
            hook_config.configure_claude_hooks(settings, desired, backup)

            original_iterdir = Path.iterdir

            def guarded_iterdir(path: Path):
                if path == drop_ins:
                    raise PermissionError("managed settings are unreadable")
                return original_iterdir(path)

            with mock.patch.object(Path, "iterdir", guarded_iterdir):
                status = hook_config.inspect_claude_hooks(
                    settings, desired, managed_settings_path=managed
                )

            self.assertTrue(status["configured"])
            self.assertFalse(status["active"])
            self.assertTrue(status["managed_policy_error"])


if __name__ == "__main__":
    unittest.main()
