from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import agentwatch_core
import tool_hook_config


class ToolHookPathTests(unittest.TestCase):
    def test_default_and_configured_integration_paths_follow_tool_conventions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"

            self.assertEqual(
                home
                / ".pi"
                / "agent"
                / "extensions"
                / tool_hook_config.PI_EXTENSION_FILE_NAME,
                tool_hook_config.pi_extension_path(home, {}),
            )
            self.assertEqual(
                home
                / ".config"
                / "opencode"
                / "plugins"
                / tool_hook_config.OPENCODE_PLUGIN_FILE_NAME,
                tool_hook_config.opencode_plugin_path(home, {}),
            )

            pi_root = root / "custom-pi"
            xdg_root = root / "xdg"
            opencode_root = root / "custom-opencode"
            self.assertEqual(
                pi_root / "extensions" / tool_hook_config.PI_EXTENSION_FILE_NAME,
                tool_hook_config.pi_extension_path(
                    home, {"PI_CODING_AGENT_DIR": str(pi_root)}
                ),
            )
            self.assertEqual(
                xdg_root
                / "opencode"
                / "plugins"
                / tool_hook_config.OPENCODE_PLUGIN_FILE_NAME,
                tool_hook_config.opencode_plugin_path(
                    home, {"XDG_CONFIG_HOME": str(xdg_root)}
                ),
            )
            self.assertEqual(
                opencode_root / "plugins" / tool_hook_config.OPENCODE_PLUGIN_FILE_NAME,
                tool_hook_config.opencode_plugin_path(
                    home,
                    {
                        "XDG_CONFIG_HOME": str(xdg_root),
                        "OPENCODE_CONFIG_DIR": str(opencode_root),
                    },
                ),
            )


class GeneratedIntegrationTests(unittest.TestCase):
    def test_pi_extension_uses_official_settled_event_and_local_ingest_only(self) -> None:
        python = Path('/tmp/python "quoted"')
        cli = Path("/tmp/agentwatch.py")
        events = Path("/tmp/tool events")

        rendered = tool_hook_config.build_pi_extension(python, cli, events)

        self.assertTrue(rendered.startswith(tool_hook_config.PI_MANAGED_MARKER + "\n"))
        self.assertIn('pi.on("agent_start"', rendered)
        self.assertIn('pi.on("tool_execution_end"', rendered)
        self.assertIn('pi.on("agent_settled"', rendered)
        self.assertIn('ctx.mode === "json"', rendered)
        self.assertIn("getSessionFile()", rendered)
        self.assertIn("if (!startLeaf)", rendered)
        self.assertIn("if (startIndex < 0) return;", rendered)
        self.assertIn('reason === "toolUse"', rendered)
        self.assertIn("settledTerminatingToolCalls.has(part.id)", rendered)
        self.assertNotIn('entry.message?.stopReason !== "toolUse"', rendered)
        self.assertIn('"tool-hook", "--source", "pi"', rendered)
        self.assertIn(
            f"const PYTHON = {json.dumps(str(python), ensure_ascii=False)};",
            rendered,
        )
        self.assertNotIn("fetch(", rendered)
        self.assertNotIn("WebSocket", rendered)

    def test_pi_extension_fails_closed_and_accepts_only_a_fully_terminating_tool_batch(
        self,
    ) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")

        rendered = tool_hook_config.build_pi_extension(
            Path("/runtime/python"),
            Path("/runtime/agentwatch.py"),
            Path("/private/events"),
        )
        rendered = rendered.replace(
            'import { spawn } from "node:child_process";',
            """
const emitted = [];
function spawn() {
  return {
    on() {},
    stdin: {
      on() {},
      end(raw) { emitted.push(JSON.parse(raw)); },
    },
    unref() {},
  };
}
""".strip(),
            1,
        )
        harness = r"""

const handlers = new Map();
agentwatchNotifications({
  on(name, handler) { handlers.set(name, handler); },
});

let startLeaf = null;
let branch = [];
const ctx = {
  mode: "interactive",
  cwd: "/tmp/project",
  isIdle() { return true; },
  hasPendingMessages() { return false; },
  sessionManager: {
    getSessionFile() { return "/tmp/session.jsonl"; },
    getLeafId() { return startLeaf; },
    getBranch() { return branch; },
    getHeader() { return {}; },
    getSessionId() { return "pi-session"; },
    getCwd() { return "/tmp/project"; },
  },
};
const user = (id) => ({
  type: "message",
  id,
  timestamp: "2026-08-04T00:00:00Z",
  message: { role: "user", content: "task" },
});
const assistant = (id, stopReason, content) => ({
  type: "message",
  id,
  timestamp: "2026-08-04T00:00:01Z",
  message: { role: "assistant", stopReason, content },
});
const toolResult = (id, toolCallId, text) => ({
  type: "message",
  id,
  timestamp: "2026-08-04T00:00:02Z",
  message: {
    role: "toolResult",
    toolCallId,
    content: [{ type: "text", text }],
  },
});

// No baseline leaf must never fall back to scanning the full historical branch.
startLeaf = null;
branch = [assistant("historical", "stop", [{ type: "text", text: "old" }])];
await handlers.get("agent_start")({}, ctx);
await handlers.get("agent_settled")({}, ctx);

// A baseline that disappeared from the active branch also fails closed.
startLeaf = "missing-baseline";
branch = [assistant("another-historical", "stop", [{ type: "text", text: "old" }])];
await handlers.get("agent_start")({}, ctx);
await handlers.get("agent_settled")({}, ctx);

// Pi only terminates a tool batch when every finalized result has terminate:true.
startLeaf = "mixed-start";
branch = [
  user(startLeaf),
  assistant("mixed-terminal", "toolUse", [
    { type: "toolCall", id: "mixed-a", name: "a", arguments: {} },
    { type: "toolCall", id: "mixed-b", name: "b", arguments: {} },
  ]),
  toolResult("mixed-result-a", "mixed-a", "a"),
  toolResult("mixed-result-b", "mixed-b", "b"),
];
await handlers.get("agent_start")({}, ctx);
await handlers.get("tool_execution_end")({
  toolCallId: "mixed-a", result: { terminate: true }, isError: false,
}, ctx);
await handlers.get("tool_execution_end")({
  toolCallId: "mixed-b", result: { terminate: false }, isError: false,
}, ctx);
await handlers.get("agent_settled")({}, ctx);

// A fully terminating batch is a valid final turn. Select the latest assistant,
// not an older ordinary assistant, and use its tool result as a text fallback.
startLeaf = "valid-start";
branch = [
  user(startLeaf),
  assistant("older-assistant", "stop", [{ type: "text", text: "older" }]),
  assistant("latest-assistant", "toolUse", [
    { type: "toolCall", id: "final-call", name: "structured_output", arguments: {} },
  ]),
  toolResult("final-result", "final-call", "structured result"),
];
await handlers.get("agent_start")({}, ctx);
await handlers.get("tool_execution_end")({
  toolCallId: "final-call", result: { terminate: true }, isError: false,
}, ctx);
await handlers.get("agent_settled")({}, ctx);

process.stdout.write(JSON.stringify(emitted));
"""

        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "pi-extension-test.mjs"
            script.write_text(rendered + harness, encoding="utf-8")
            completed = subprocess.run(
                [node, str(script)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            [
                {
                    "schema": "agentwatch_pi_hook_v1",
                    "event_name": "agent_settled",
                    "session_id": "pi-session",
                    "event_id": "latest-assistant",
                    "timestamp": "2026-08-04T00:00:01Z",
                    "cwd": "/tmp/project",
                    "parent_session": "",
                    "outcome": "completed",
                    "stop_reason": "toolUse",
                    "message": "structured result",
                }
            ],
            json.loads(completed.stdout),
        )

    def test_opencode_plugin_uses_official_events_and_filters_child_sessions(self) -> None:
        rendered = tool_hook_config.build_opencode_plugin(
            Path("/tmp/python"),
            Path("/tmp/agentwatch.py"),
            Path("/tmp/tool-events"),
        )

        self.assertTrue(
            rendered.startswith(tool_hook_config.OPENCODE_MANAGED_MARKER + "\n")
        )
        for event_name in (
            "message.updated",
            "session.status",
            "session.error",
            "session.idle",
        ):
            self.assertIn(event_name, rendered)
        self.assertIn("session.parentID", rendered)
        self.assertIn("messages.findLastIndex", rendered)
        self.assertIn("messages.slice(lastUserIndex + 1)", rendered)
        self.assertIn('`error:${lastUser?.info?.id || "unknown"}:${errorName}`', rendered)
        self.assertIn("spawnSync(", rendered)
        self.assertIn("dispose: async", rendered)
        self.assertIn("while (pendingBySession.size > 0)", rendered)
        self.assertIn("for (const sessionID of Array.from(armed))", rendered)
        self.assertIn('"--require-persist"', rendered)
        self.assertIn('"tool-hook",', rendered)
        self.assertIn('"opencode",', rendered)
        self.assertNotIn("fetch(", rendered)
        self.assertNotIn("WebSocket", rendered)

    def test_opencode_dispose_drains_a_fire_and_forget_headless_idle_read(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")

        rendered = tool_hook_config.build_opencode_plugin(
            Path("/runtime/python"),
            Path("/runtime/agentwatch.py"),
            Path("/private/events"),
        )
        rendered = rendered.replace(
            'import { spawnSync } from "node:child_process";',
            """
const emitted = [];
function spawnSync(_python, _args, options) {
  emitted.push(JSON.parse(options.input));
  return { status: 0, signal: null, error: null };
}
""".strip(),
            1,
        )
        harness = r"""

let resolveSession;
let resolveMessages;
const sessionRead = new Promise((resolve) => { resolveSession = resolve; });
const messageRead = new Promise((resolve) => { resolveMessages = resolve; });
const hooks = await AgentWatchNotifications({
  directory: "/tmp/project",
  client: {
    session: {
      get() { return sessionRead; },
      messages() { return messageRead; },
    },
  },
});

await hooks.event({
  event: { type: "session.status", properties: { sessionID: "root-session", status: { type: "busy" } } },
});
// Match an immediate headless teardown before the fire-and-forget idle callback
// has started. dispose() must compensate every still-armed root session.
let disposed = false;
const shutdown = hooks.dispose().then(() => { disposed = true; });
await new Promise((resolve) => setTimeout(resolve, 0));
if (disposed) throw new Error("dispose returned before the pending session read");

resolveSession({ data: { id: "root-session", directory: "/tmp/project", parentID: "" } });
await new Promise((resolve) => setTimeout(resolve, 0));
if (disposed) throw new Error("dispose returned before the pending message read");
resolveMessages({ data: [
  { info: { id: "old-user", role: "user" }, parts: [] },
  {
    info: { id: "old-assistant", role: "assistant", finish: "stop", time: { completed: 1 } },
    parts: [{ type: "text", text: "old" }],
  },
  { info: { id: "new-user", role: "user" }, parts: [] },
  {
    info: {
      id: "new-assistant",
      role: "assistant",
      finish: "stop",
      time: { completed: 1785800000123 },
      path: { cwd: "/tmp/project" },
    },
    parts: [{ type: "text", text: "current turn complete" }],
  },
] });
await shutdown;
process.stdout.write(JSON.stringify({ disposed, emitted }));
"""

        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "opencode-plugin-headless-test.mjs"
            script.write_text(rendered + harness, encoding="utf-8")
            completed = subprocess.run(
                [node, str(script)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["disposed"])
        self.assertEqual(1, len(result["emitted"]))
        event = result["emitted"][0]
        self.assertEqual("root-session", event["session_id"])
        self.assertEqual("new-assistant", event["event_id"])
        self.assertEqual("current turn complete", event["message"])


class ManagedIntegrationSafetyTests(unittest.TestCase):
    def test_configure_is_private_idempotent_inspectable_and_removable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plugins" / tool_hook_config.OPENCODE_PLUGIN_FILE_NAME
            desired = tool_hook_config.build_opencode_plugin(
                Path("/runtime/python"),
                Path("/runtime/agentwatch.py"),
                Path("/private/events"),
            )

            self.assertTrue(
                tool_hook_config.configure_managed_integration(
                    path,
                    tool_hook_config.OPENCODE_MANAGED_MARKER,
                    desired,
                    enabled=True,
                )
            )
            self.assertEqual(desired, path.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual(0, stat.S_IMODE(path.stat().st_mode) & 0o077)
            self.assertFalse(
                tool_hook_config.configure_managed_integration(
                    path,
                    tool_hook_config.OPENCODE_MANAGED_MARKER,
                    desired,
                    enabled=True,
                )
            )
            self.assertEqual(
                {
                    "path": str(path),
                    "configured": True,
                    "current": True,
                    "path_safe": True,
                },
                tool_hook_config.inspect_managed_integration(
                    path,
                    tool_hook_config.OPENCODE_MANAGED_MARKER,
                    desired,
                ),
            )
            self.assertTrue(
                tool_hook_config.configure_managed_integration(
                    path,
                    tool_hook_config.OPENCODE_MANAGED_MARKER,
                    desired,
                    enabled=False,
                )
            )
            self.assertFalse(path.exists())

    def test_non_agentwatch_file_is_never_replaced_or_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / tool_hook_config.PI_EXTENSION_FILE_NAME
            original = "// user-owned extension\n"
            path.write_text(original, encoding="utf-8")
            desired = tool_hook_config.build_pi_extension(
                Path("/runtime/python"),
                Path("/runtime/agentwatch.py"),
                Path("/private/events"),
            )

            for enabled in (True, False):
                with self.subTest(enabled=enabled), self.assertRaises(
                    agentwatch_core.AgentWatchError
                ):
                    tool_hook_config.configure_managed_integration(
                        path,
                        tool_hook_config.PI_MANAGED_MARKER,
                        desired,
                        enabled=enabled,
                    )
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_linked_integration_path_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "important.ts"
            target.write_text("keep intact\n", encoding="utf-8")
            path = root / tool_hook_config.PI_EXTENSION_FILE_NAME
            path.symlink_to(target)
            desired = tool_hook_config.build_pi_extension(
                Path("/runtime/python"),
                Path("/runtime/agentwatch.py"),
                Path("/private/events"),
            )

            with self.assertRaises(agentwatch_core.AgentWatchError):
                tool_hook_config.configure_managed_integration(
                    path,
                    tool_hook_config.PI_MANAGED_MARKER,
                    desired,
                    enabled=True,
                )

            self.assertEqual("keep intact\n", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
