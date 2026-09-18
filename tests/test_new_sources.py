from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import agentwatch
import codex_watch_notifier as watcher
import tool_hook_config
from tests import test_tool_hooks


class ZCodeMetadataTests(unittest.TestCase):
    def test_title_lookup_is_read_only_and_does_not_read_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'db.sqlite'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE session (id TEXT, title TEXT, directory TEXT)')
                conn.execute('INSERT INTO session VALUES (?, ?, ?)', ('sess-1', '修复导航问题', '/work/project'))
            before = db.read_bytes()
            record = {'message': 'Turn completed', 'event': 'turn.completed', 'status': 'completed',
                      'sessionId': 'sess-1', 'context': {'queryId': 'query-1'}}
            with mock.patch.dict(os.environ, {'ZCODE_WATCH_DB_PATH': str(db)}):
                event = watcher.trigger_from_zcode_record(Path(folder) / 'missing', 0, record)
                self.assertEqual({}, watcher.zcode_session_metadata("x' OR 1=1 --"))
            self.assertIn('修复导航问题', event['notification_title'])
            self.assertEqual('/work/project', event['cwd'])
            self.assertEqual(before, db.read_bytes())

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'missing.db'
            with mock.patch.dict(os.environ, {'ZCODE_WATCH_DB_PATH': str(db)}):
                self.assertEqual({}, watcher.zcode_session_metadata('a'))
            self.assertFalse(db.exists())


def projection(turn=1):
    return {'version': 7, 'record': {
        'identity': {'formatVersion': 3, 'cwd': '/work/project', 'createdAt': 1000, 'isSeeded': False},
        'rows': {
            'title': {'ver': 1, 'val': '测试会话'},
            'subagent': {'ver': 2, 'val': {}},
            'turnBoundary': {'ver': 2, 'seq': turn * 10, 'val': {
                'lastTurn': turn, 'openTurnStartSeq': None,
                'lastStepBoundary': {'kind': 'end', 'seq': turn * 10 - 1}}},
        },
    }}


class DeepSeekTests(unittest.TestCase):
    def test_baseline_dedupe_and_new_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'session-example.json'
            path.write_text(json.dumps(projection()))
            state = {}
            sender = mock.Mock()
            sender.send.return_value = True
            log = mock.Mock()
            watcher.baseline_deepseek_projections(state, root, log)
            self.assertEqual(0, watcher.process_deepseek_projection(path, state, sender, log))
            path.write_text(json.dumps(projection(2)))
            self.assertEqual(1, watcher.process_deepseek_projection(path, state, sender, log))
            # A title/cache-only rewrite is not another completion.
            changed = projection(2)
            changed['record']['rows']['turnBoundary']['seq'] += 20
            path.write_text(json.dumps(changed))
            self.assertEqual(0, watcher.process_deepseek_projection(path, state, sender, log))
            self.assertEqual(1, sender.send.call_count)
            self.assertNotIn('测试会话', json.dumps(state, ensure_ascii=False))

    def test_rejects_open_child_seeded_unknown_and_corrupt_snapshots(self):
        cases = []
        opened = projection()
        opened['record']['rows']['turnBoundary']['val']['openTurnStartSeq'] = 1
        cases.append(opened)
        child = projection()
        child['record']['rows']['subagent']['val']['identity'] = {'mode': 'one-shot', 'seq': 1}
        cases.append(child)
        seeded = projection()
        seeded['record']['identity']['isSeeded'] = True
        cases.append(seeded)
        version = projection()
        version['version'] = 999
        cases.append(version)
        goal = projection()
        goal['record']['rows']['goal'] = {'val': {'current': {'phase': 'active'}}}
        cases.append(goal)
        cases.extend([{}, [], {'version': 7, 'record': []}])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session-test.json'
            for value in cases:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    self.assertIsNone(watcher.trigger_from_deepseek_projection(path))

    def test_failed_delivery_is_bounded_and_no_message_is_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session-test.json'
            path.write_text(json.dumps(projection()))
            state = {}
            sender = mock.Mock()
            sender.send.return_value = False
            log = mock.Mock()
            with mock.patch.dict(os.environ, {'NOTIFY_DELIVERY_RETRY_DELAY_SECONDS': '60'}):
                with mock.patch.object(watcher.time, 'time', return_value=1000):
                    watcher.process_deepseek_projection(path, state, sender, log)
                    watcher.process_deepseek_projection(path, state, sender, log)
                with mock.patch.object(watcher.time, 'time', return_value=1061):
                    watcher.process_deepseek_projection(path, state, sender, log)
                    watcher.process_deepseek_projection(path, state, sender, log)
            self.assertEqual(2, sender.send.call_count)


class OmpTests(unittest.TestCase):
    def test_work_host_omp_version_is_supported(self):
        with mock.patch.object(agentwatch.shutil, 'which', return_value='/usr/local/bin/omp'), mock.patch.object(
            agentwatch, '_run', return_value=mock.Mock(returncode=0, stdout='omp/18.0.4', stderr='')
        ):
            result = agentwatch._semver_cli_status('omp', agentwatch.MIN_OMP_EXTENSION_VERSION)
        self.assertTrue(result['cli_compatible'])

    def test_ingestor_routes_omp_and_preserves_title(self):
        payload = test_tool_hooks.valid_hook_payload('omp')
        payload['session_title'] = '修复网络重连'
        with tempfile.TemporaryDirectory() as folder:
            stdin = test_tool_hooks.ToolHookIngestTests._stdin(json.dumps(payload).encode())
            with mock.patch.object(agentwatch.sys, 'stdin', stdin):
                path = agentwatch.ingest_tool_hook_event('omp', Path(folder) / 'events')
            record = watcher.read_owned_tool_hook_event(path)
            event = watcher.trigger_from_tool_hook_record(path, 0, record)
            self.assertEqual('omp', watcher.ntfy_source(event))
            self.assertIn('修复网络重连', event['notification_title'])

    @unittest.skipUnless(shutil.which('node'), 'Node is required for extension lifecycle test')
    def test_stop_continuation_and_child_events_do_not_emit(self):
        source = tool_hook_config.build_omp_extension(Path('/python'), Path('/cli'), Path('/events'))
        source = source.replace('import { spawnSync } from "node:child_process";',
                                'const outputs = []; const spawnSync = (...args) => outputs.push(args);')
        script = source + '''
const handlers = {};
agentwatchOmp({on: (name, fn) => handlers[name] = fn, getSessionName: () => "Readable title"});
const ctx = {cwd: "/work/project", hasPendingMessages: () => false, sessionManager: {
 getSessionFile: () => "/session.jsonl", getSessionId: () => "session-1", getBranch: () => [
 {id: "assistant-1", type: "message", timestamp: "2026-09-18T01:00:00Z", message: {role: "assistant", stopReason: "stop"}}]}};
handlers.agent_start();
handlers.agent_end({}, ctx);
if (outputs.length !== 0) throw Error("child event emitted");
handlers.session_stop({last_assistant_message: "Done"}, ctx);
handlers.agent_end({willContinue: true}, ctx);
if (outputs.length !== 0) throw Error("continuation emitted");
handlers.session_stop({last_assistant_message: "Done"}, ctx);
handlers.agent_end({}, ctx);
handlers.agent_end({}, ctx);
if (outputs.length !== 1) throw Error("missing or duplicate final event");
const payload = JSON.parse(outputs[0][2].input);
if (payload.session_title !== "Readable title" || payload.event_id !== "assistant-1") throw Error("bad metadata");
'''
        result = subprocess.run(['node', '--input-type=module'], input=script, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
