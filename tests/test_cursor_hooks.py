from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

import agentwatch
import agentwatch_core
import codex_watch_notifier as watcher
import tool_hook_config as config


class CursorConfigTests(unittest.TestCase):
    def test_merge_update_and_uninstall_preserve_other_hooks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'hooks.json'
            backup = root / 'backup.json'
            original = {'version': 1, 'other': True, 'hooks': {
                'stop': [{'command': 'my-own-hook', 'loop_limit': 2}],
                'afterFileEdit': [], 'subagentStop': [{'command': 'own-subagent-hook'}]}}
            path.write_text(json.dumps(original))
            handler = config.build_cursor_hook_handler(Path('/path with spaces/python'), Path('/a/agentwatch.py'), root / 'events')
            self.assertTrue(config.configure_cursor_hooks(path, handler, enabled=True, backup=backup))
            self.assertFalse(config.configure_cursor_hooks(path, handler, enabled=True, backup=backup))
            self.assertEqual(original, json.loads(backup.read_text()))
            self.assertTrue(config.inspect_cursor_hooks(path, handler, enabled=True)['active'])
            replacement = config.build_cursor_hook_handler(Path('/new/python'), Path('/a/agentwatch.py'), root / 'events')
            config.configure_cursor_hooks(path, replacement, enabled=True, backup=backup)
            self.assertEqual([original['hooks']['stop'][0], replacement], json.loads(path.read_text())['hooks']['stop'])
            config.configure_cursor_hooks(path, replacement, enabled=False, backup=backup)
            self.assertEqual(original, json.loads(path.read_text()))

    def test_quoted_paths_and_similar_foreign_commands(self):
        handler = config.build_cursor_hook_handler(Path('/a b/python'), Path('/x y/agentwatch.py'), Path('/e f/events'))
        self.assertEqual('/x y/agentwatch.py', shlex.split(handler['command'])[1])
        self.assertTrue(config._owned_cursor_handler(handler))
        self.assertFalse(config._owned_cursor_handler({'command': 'echo ' + config.CURSOR_MANAGED_ID}))

    def test_malformed_or_linked_config_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'hooks.json'
            for value in ['{', '[]', '{"version":2}', '{"hooks":{"stop":{}}}']:
                path.write_text(value)
                with self.assertRaises(agentwatch_core.AgentWatchError):
                    config.configure_cursor_hooks(path, {}, enabled=True, backup=root / 'backup')
                self.assertEqual(value, path.read_text())
            linked = root / 'linked.json'
            linked.symlink_to(path)
            with self.assertRaises(agentwatch_core.AgentWatchError):
                config.preflight_cursor_hooks(linked)

    def test_disabled_missing_config_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertFalse(config.configure_cursor_hooks(root / 'hooks.json', {}, enabled=False, backup=root / 'backup'))
            self.assertEqual([], list(root.iterdir()))


def payload(**overrides):
    return {'conversation_id': 'conversation-1', 'generation_id': 'generation-1',
            'hook_event_name': 'stop', 'status': 'completed', 'loop_count': 0,
            'workspace_roots': ['/work/readable-project'], 'cursor_version': '2.6.20',
            'user_email': 'PRIVATE_EMAIL', 'transcript_path': '/PRIVATE_TRANSCRIPT', **overrides}


class CursorIngestTests(unittest.TestCase):
    def ingest(self, root, value):
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(value).encode()))
        with mock.patch.object(agentwatch.sys, 'stdin', stdin):
            return agentwatch.ingest_cursor_hook_event(root)

    def test_completed_error_and_aborted_share_delivery_pipeline(self):
        for status, outcome in [('completed', 'completed'), ('error', 'error'), ('aborted', 'cancelled')]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                path = self.ingest(Path(folder) / 'events', payload(status=status))
                raw = path.read_text()
                self.assertNotIn('PRIVATE_EMAIL', raw)
                self.assertNotIn('PRIVATE_TRANSCRIPT', raw)
                record = watcher.read_owned_tool_hook_event(path)
                self.assertEqual(outcome, record['outcome'])
                event = watcher.trigger_from_tool_hook_record(path, 0, record)
                self.assertEqual('cursor', watcher.ntfy_source(event))
                self.assertEqual('Cursor', event['bark_group'])
                self.assertIn('readable-project', event['notification_title'])

    def test_duplicate_stop_is_sent_once_and_new_generation_is_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'events'
            paths = [self.ingest(root, payload()) for _ in range(2)]
            paths.append(self.ingest(root, payload(generation_id='generation-2')))
            sender = mock.Mock()
            sender.send.return_value = True
            state = {}
            for path in paths:
                watcher.process_tool_hook_event_file(path, state, sender, mock.Mock())
            self.assertEqual(2, sender.send.call_count)
            self.assertEqual([], list(root.glob('*.json')))

    def test_invalid_and_subagent_events_do_not_write_queue(self):
        invalid = [payload(hook_event_name='subagentStop'), payload(generation_id=''),
                   payload(status='working'), payload(status=[]), payload(loop_count=True), payload(loop_count=-1),
                   payload(workspace_roots=[]), payload(workspace_roots=['relative']),
                   payload(subagent_id='child'), [], {}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'events'
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(agentwatch_core.AgentWatchError):
                    self.ingest(root, value)
            self.assertFalse(root.exists())

    def test_native_cli_never_requests_followup_or_blocks_on_error(self):
        output = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(b'invalid json'))
        with mock.patch.object(agentwatch.sys, 'stdin', stdin), mock.patch('sys.stdout', output):
            result = agentwatch.main(['cursor-hook'])
        self.assertEqual(0, result)
        self.assertEqual({}, json.loads(output.getvalue()))

    def test_cursor_imported_claude_hook_is_ignored(self):
        value = {'hook_event_name': 'Stop', 'session_id': 's', 'prompt_id': 'p',
                 'transcript_path': '/a', 'cwd': '/work/project', 'last_assistant_message': 'done'}
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {'CURSOR_VERSION': '2.6.20'}):
            target = Path(folder) / 'claude.jsonl'
            self.assertFalse(agentwatch.ingest_claude_hook_event(io.StringIO(json.dumps(value)), events_path=target))
            self.assertFalse(target.exists())
