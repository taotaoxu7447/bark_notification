from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import agentwatch
import agentwatch_core


class ClaudeSpoolOwnershipTests(unittest.TestCase):
    def make_paths(self, root: Path) -> agentwatch.InstallPaths:
        paths = agentwatch.InstallPaths(root / "config", root / "home")
        paths.config.mkdir(mode=0o700)
        return paths

    def set_events_path(self, paths: agentwatch.InstallPaths, events_path: Path) -> None:
        (paths.config / "env").write_text(
            f"CLAUDE_WATCH_EVENTS_FILE={events_path}\n", encoding="utf-8"
        )
        os.chmod(paths.config / "env", 0o600)

    def test_first_custom_spool_must_not_reuse_an_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            private = root / "private"
            private.mkdir(mode=0o700)
            events_path = private / "events.jsonl"
            original = b"important unrelated data\n"
            events_path.write_bytes(original)
            os.chmod(events_path, 0o600)
            self.set_events_path(paths, events_path)

            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "without AgentWatch ownership"
            ):
                agentwatch._preflight_installed_claude_hooks(paths)

            self.assertEqual(original, events_path.read_bytes())
            self.assertFalse(agentwatch._claude_spool_ownership_path(paths).exists())

    def test_nonexistent_private_custom_spool_is_registered_and_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            private = root / "private"
            private.mkdir(mode=0o700)
            events_path = private / "events.jsonl"
            self.set_events_path(paths, events_path)

            agentwatch._preflight_installed_claude_hooks(paths)
            agentwatch._configure_installed_claude_hooks(paths)
            ownership = json.loads(
                agentwatch._claude_spool_ownership_path(paths).read_text(encoding="utf-8")
            )
            self.assertEqual(str(events_path), ownership["events_path"])
            events_path.write_text("reserved AgentWatch queue\n", encoding="utf-8")
            os.chmod(events_path, 0o600)

            # The persisted record, not the file's contents or name, proves the
            # user already dedicated this exact path to AgentWatch.
            agentwatch._preflight_installed_claude_hooks(paths)
            self.assertEqual(events_path, agentwatch._validate_installed_claude_events_path(paths))

    def test_custom_spool_rejects_ancestor_link_directory_and_config_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            private = root / "private"
            private.mkdir(mode=0o700)
            linked = root / "linked"
            try:
                linked.symlink_to(private, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable on this platform")

            candidates = [
                linked / "events.jsonl",
                private,
                paths.config / "state.json",
            ]
            for candidate in candidates:
                with self.subTest(candidate=candidate):
                    self.set_events_path(paths, candidate)
                    with self.assertRaises(agentwatch_core.AgentWatchError):
                        agentwatch._preflight_installed_claude_hooks(paths)

    def test_custom_spool_rejects_regular_file_as_intermediate_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            private = root / "private"
            private.mkdir(mode=0o700)
            blocker = private / "not-a-directory"
            original = b"unrelated file\n"
            blocker.write_bytes(original)
            os.chmod(blocker, 0o600)
            self.set_events_path(paths, blocker / "nested" / "events.jsonl")

            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "ancestor must be a directory"
            ):
                agentwatch._preflight_installed_claude_hooks(paths)

            self.assertEqual(original, blocker.read_bytes())
            self.assertFalse(agentwatch._claude_spool_ownership_path(paths).exists())

    def test_custom_spool_requires_direct_parent_to_already_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            restricted = root / "restricted"
            restricted.mkdir(mode=0o500)
            missing_parent = restricted / "missing"
            events_path = missing_parent / "events.jsonl"
            self.set_events_path(paths, events_path)

            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "parent must already exist"
            ):
                agentwatch._preflight_installed_claude_hooks(paths)

            self.assertFalse(missing_parent.exists())
            self.assertFalse(agentwatch._claude_spool_ownership_path(paths).exists())

    @unittest.skipIf(os.name == "nt", "Unix permission bits required")
    def test_custom_spool_rejects_parent_without_owner_rwx(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            for mode in (0o500, 0o400, 0o200, 0o000):
                with self.subTest(mode=oct(mode)):
                    parent = root / f"parent-{mode:o}"
                    parent.mkdir(mode=0o700)
                    os.chmod(parent, mode)
                    self.set_events_path(paths, parent / "events.jsonl")

                    with self.assertRaisesRegex(
                        agentwatch_core.AgentWatchError, "parent must be private"
                    ):
                        agentwatch._preflight_installed_claude_hooks(paths)

                    self.assertFalse(
                        agentwatch._claude_spool_ownership_path(paths).exists()
                    )

    @unittest.skipIf(os.name == "nt", "Unix permission bits required")
    def test_custom_spool_and_lock_require_owner_read_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            parent = root / "private"
            parent.mkdir(mode=0o700)
            events_path = parent / "events.jsonl"
            self.set_events_path(paths, events_path)

            for mode in (0o400, 0o200, 0o000):
                with self.subTest(target="spool", mode=oct(mode)):
                    events_path.write_bytes(b"")
                    os.chmod(events_path, mode)
                    with self.assertRaisesRegex(
                        agentwatch_core.AgentWatchError,
                        "owner-readable.*owner-writable",
                    ):
                        agentwatch._preflight_installed_claude_hooks(paths)
                    events_path.unlink()

            lock_path = events_path.with_name(events_path.name + ".append.lock")
            for mode in (0o400, 0o200, 0o000):
                with self.subTest(target="lock", mode=oct(mode)):
                    lock_path.write_bytes(b"")
                    os.chmod(lock_path, mode)
                    with self.assertRaisesRegex(
                        agentwatch_core.AgentWatchError,
                        "owner-readable.*owner-writable",
                    ):
                        agentwatch._preflight_installed_claude_hooks(paths)
                    lock_path.unlink()

            self.assertFalse(agentwatch._claude_spool_ownership_path(paths).exists())

    def test_custom_spool_parent_and_file_must_be_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            public = root / "public"
            public.mkdir(mode=0o755)
            missing = public / "events.jsonl"
            self.set_events_path(paths, missing)
            with self.assertRaisesRegex(agentwatch_core.AgentWatchError, "parent must be private"):
                agentwatch._preflight_installed_claude_hooks(paths)

            private = root / "private"
            private.mkdir(mode=0o700)
            existing = private / "events.jsonl"
            existing.touch(mode=0o644)
            self.set_events_path(paths, existing)
            with self.assertRaisesRegex(agentwatch_core.AgentWatchError, "must be private"):
                agentwatch._preflight_installed_claude_hooks(paths)

    def test_status_marks_unsafe_custom_path_inactive_without_touching_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            private = root / "private"
            private.mkdir(mode=0o700)
            events_path = private / "foreign.jsonl"
            original = b"foreign\n"
            events_path.write_bytes(original)
            os.chmod(events_path, 0o600)
            self.set_events_path(paths, events_path)

            with mock.patch.object(
                agentwatch,
                "_claude_cli_status",
                return_value={
                    "cli_detected": True,
                    "cli_path": "/usr/bin/claude",
                    "cli_version": "2.1.220",
                    "minimum_cli_version": "2.1.196",
                    "cli_compatible": True,
                },
            ):
                status = agentwatch._installed_claude_hook_status(paths)

            self.assertFalse(status["events_path_safe"])
            self.assertFalse(status["active"])
            self.assertIn("ownership", status["events_path_error"])
            self.assertEqual(original, events_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
