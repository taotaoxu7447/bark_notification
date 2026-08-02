from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

import agentwatch_core
import claude_hook_config as hook_config


class ClaudeHookConfigTests(unittest.TestCase):
    def make_handler(self, root: Path) -> dict:
        return hook_config.build_claude_hook_handler(
            root / "python",
            root / "runtime" / "agentwatch.py",
            root / "config" / "claude-hook-events.jsonl",
        )

    def test_install_is_idempotent_and_preserves_existing_settings_and_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / "home" / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            existing = {
                "model": "sonnet",
                "unknownFutureSetting": {"keep": True},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "rtk rewrite"}],
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "/usr/local/bin/other-hook"}
                            ]
                        }
                    ],
                },
            }
            settings.write_text(json.dumps(existing), encoding="utf-8")
            backup = root / "config" / "backups" / hook_config.CLAUDE_SETTINGS_BACKUP_FILE
            desired = self.make_handler(root)

            self.assertTrue(
                hook_config.configure_claude_hooks(settings, desired, backup, enabled=True)
            )
            first = settings.read_bytes()
            self.assertFalse(
                hook_config.configure_claude_hooks(settings, desired, backup, enabled=True)
            )
            self.assertEqual(first, settings.read_bytes())

            merged = json.loads(first)
            self.assertEqual("sonnet", merged["model"])
            self.assertEqual({"keep": True}, merged["unknownFutureSetting"])
            self.assertEqual(existing["hooks"]["PreToolUse"], merged["hooks"]["PreToolUse"])
            self.assertEqual(
                "/usr/local/bin/other-hook",
                merged["hooks"]["Stop"][0]["hooks"][0]["command"],
            )
            for event_name in hook_config.CLAUDE_HOOK_EVENTS:
                installed = [
                    handler
                    for group in merged["hooks"][event_name]
                    for handler in group["hooks"]
                    if handler == desired
                ]
                self.assertEqual([desired], installed)
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), existing)
            self.assertEqual(0, stat.S_IMODE(settings.stat().st_mode) & 0o077)
            self.assertEqual(0, stat.S_IMODE(backup.stat().st_mode) & 0o077)

    def test_update_replaces_only_stale_agentwatch_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            desired = self.make_handler(root)
            stale = dict(desired)
            stale["command"] = str(root / "old-python")
            stale["timeout"] = 30
            third_party = {"type": "command", "command": "notify-somewhere-else"}
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [{"hooks": [stale, third_party, stale]}],
                            "StopFailure": [{"hooks": [stale]}],
                        }
                    }
                ),
                encoding="utf-8",
            )

            hook_config.configure_claude_hooks(
                settings,
                desired,
                root / "config" / "backups" / "settings.json",
                enabled=True,
            )
            merged = json.loads(settings.read_text(encoding="utf-8"))
            stop_handlers = [
                handler for group in merged["hooks"]["Stop"] for handler in group["hooks"]
            ]
            failure_handlers = [
                handler
                for group in merged["hooks"]["StopFailure"]
                for handler in group["hooks"]
            ]
            self.assertEqual([third_party, desired], stop_handlers)
            self.assertEqual([desired], failure_handlers)

    def test_managed_id_replaces_handler_after_runtime_path_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / ".claude" / "settings.json"
            backup = root / "config" / "backups" / "settings.json"
            old_handler = hook_config.build_claude_hook_handler(
                root / "python-old",
                root / "runtime-old" / "agentwatch.py",
                root / "config-old" / "events.jsonl",
            )
            new_handler = self.make_handler(root)

            hook_config.configure_claude_hooks(settings, old_handler, backup)
            hook_config.configure_claude_hooks(settings, new_handler, backup)

            payload = json.loads(settings.read_text(encoding="utf-8"))
            for event_name in hook_config.CLAUDE_HOOK_EVENTS:
                handlers = [
                    handler
                    for group in payload["hooks"][event_name]
                    for handler in group["hooks"]
                ]
                self.assertEqual([new_handler], handlers)

    def test_uninstall_removes_only_agentwatch_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / ".claude" / "settings.json"
            backup = root / "config" / "backups" / "settings.json"
            desired = self.make_handler(root)
            hook_config.configure_claude_hooks(settings, desired, backup, enabled=True)
            current = json.loads(settings.read_text(encoding="utf-8"))
            current["hooks"]["Stop"][0]["hooks"].append(
                {"type": "command", "command": "keep-me"}
            )
            current["permissions"] = {"allow": ["Read"]}
            settings.write_text(json.dumps(current), encoding="utf-8")

            self.assertTrue(
                hook_config.configure_claude_hooks(settings, desired, backup, enabled=False)
            )
            removed = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual({"allow": ["Read"]}, removed["permissions"])
            self.assertEqual(
                [{"type": "command", "command": "keep-me"}],
                removed["hooks"]["Stop"][0]["hooks"],
            )
            self.assertNotIn("StopFailure", removed["hooks"])

    def test_invalid_or_symlinked_settings_are_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            desired = self.make_handler(root)
            backup = root / "config" / "backups" / "settings.json"
            invalid = root / ".claude" / "settings.json"
            invalid.parent.mkdir(parents=True)
            invalid.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(agentwatch_core.AgentWatchError):
                hook_config.configure_claude_hooks(invalid, desired, backup)
            self.assertEqual("{not-json", invalid.read_text(encoding="utf-8"))

            outside = root / "outside.json"
            outside.write_text('{"safe":true}', encoding="utf-8")
            invalid.unlink()
            invalid.symlink_to(outside)
            with self.assertRaises(agentwatch_core.AgentWatchError):
                hook_config.configure_claude_hooks(invalid, desired, backup)
            self.assertEqual({"safe": True}, json.loads(outside.read_text(encoding="utf-8")))

    def test_status_reports_user_and_managed_hook_disables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / ".claude" / "settings.json"
            backup = root / "config" / "backups" / "settings.json"
            managed = root / "managed-settings.json"
            desired = self.make_handler(root)
            hook_config.configure_claude_hooks(settings, desired, backup)

            active = hook_config.inspect_claude_hooks(settings, desired)
            self.assertTrue(active["configured"])
            self.assertTrue(active["active"])

            payload = json.loads(settings.read_text(encoding="utf-8"))
            payload["disableAllHooks"] = True
            settings.write_text(json.dumps(payload), encoding="utf-8")
            disabled = hook_config.inspect_claude_hooks(settings, desired)
            self.assertTrue(disabled["configured"])
            self.assertFalse(disabled["active"])
            self.assertTrue(disabled["disable_all_hooks"])

            payload["disableAllHooks"] = False
            settings.write_text(json.dumps(payload), encoding="utf-8")
            managed.write_text('{"allowManagedHooksOnly":true}', encoding="utf-8")
            blocked = hook_config.inspect_claude_hooks(
                settings, desired, managed_settings_path=managed
            )
            self.assertTrue(blocked["configured"])
            self.assertFalse(blocked["active"])
            self.assertTrue(blocked["managed_policy_blocked"])

            managed.write_text(
                '{"strictPluginOnlyCustomization":["hooks","agents"]}',
                encoding="utf-8",
            )
            strict_blocked = hook_config.inspect_claude_hooks(
                settings, desired, managed_settings_path=managed
            )
            self.assertTrue(strict_blocked["managed_policy_blocked"])
            self.assertFalse(strict_blocked["active"])

    def test_managed_drop_ins_are_merged_in_filename_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / ".claude" / "settings.json"
            backup = root / "config" / "backups" / "settings.json"
            managed = root / "managed" / "managed-settings.json"
            drop_ins = managed.parent / "managed-settings.d"
            managed.parent.mkdir(parents=True)
            drop_ins.mkdir()
            desired = self.make_handler(root)
            hook_config.configure_claude_hooks(settings, desired, backup)
            managed.write_text('{"allowManagedHooksOnly":true}', encoding="utf-8")
            (drop_ins / "10-enable-user-hooks.json").write_text(
                '{"allowManagedHooksOnly":false}', encoding="utf-8"
            )
            (drop_ins / "20-strict.json").write_text(
                '{"strictPluginOnlyCustomization":["hooks"]}', encoding="utf-8"
            )
            (drop_ins / ".ignored.json").write_text(
                '{"disableAllHooks":true}', encoding="utf-8"
            )

            status = hook_config.inspect_claude_hooks(
                settings, desired, managed_settings_path=managed
            )

            self.assertTrue(status["configured"])
            self.assertTrue(status["managed_policy_blocked"])
            self.assertFalse(status["active"])
            self.assertEqual(
                [
                    str(managed),
                    str(drop_ins / "10-enable-user-hooks.json"),
                    str(drop_ins / "20-strict.json"),
                ],
                status["managed_policy_sources"],
            )

    def test_policy_helper_requires_runtime_policy_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / ".claude" / "settings.json"
            backup = root / "config" / "backups" / "settings.json"
            managed = root / "managed-settings.json"
            desired = self.make_handler(root)
            hook_config.configure_claude_hooks(settings, desired, backup)
            managed.write_text(
                '{"policyHelper":{"path":"/usr/local/bin/claude-policy"}}',
                encoding="utf-8",
            )

            status = hook_config.inspect_claude_hooks(
                settings, desired, managed_settings_path=managed
            )

            self.assertTrue(status["managed_policy_dynamic"])
            self.assertFalse(status["active"])

    def test_relative_claude_config_dir_is_resolved_under_home(self) -> None:
        home = Path("/tmp/agentwatch-home")
        self.assertEqual(
            home / "custom-claude" / "settings.json",
            hook_config.claude_settings_path(
                home, {"CLAUDE_CONFIG_DIR": "custom-claude"}
            ),
        )


if __name__ == "__main__":
    unittest.main()
