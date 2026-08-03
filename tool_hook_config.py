#!/usr/bin/env python3
"""Safe installation helpers for the Pi and OpenCode AgentWatch integrations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Mapping

from agentwatch_core import AgentWatchError, atomic_write, path_has_link_component


INTEGRATION_REGISTRATION_FILE_NAME = "tool-hook-registration.json"
PI_EXTENSION_FILE_NAME = "agentwatch-notifications.ts"
OPENCODE_PLUGIN_FILE_NAME = "agentwatch-notifications.js"
PI_MANAGED_MARKER = "// AgentWatch managed integration v1: pi"
OPENCODE_MANAGED_MARKER = "// AgentWatch managed integration v1: opencode"
MAX_MANAGED_FILE_BYTES = 512 * 1024


def _expanded_absolute(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(os.path.abspath(expanded))


def pi_extension_path(home: Path, environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("PI_CODING_AGENT_DIR", "").strip()
    root = _expanded_absolute(configured) if configured else home / ".pi" / "agent"
    return root / "extensions" / PI_EXTENSION_FILE_NAME


def opencode_plugin_path(home: Path, environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("OPENCODE_CONFIG_DIR", "").strip()
    if configured:
        root = _expanded_absolute(configured)
    else:
        xdg = values.get("XDG_CONFIG_HOME", "").strip()
        root = (_expanded_absolute(xdg) if xdg else home / ".config") / "opencode"
    return root / "plugins" / OPENCODE_PLUGIN_FILE_NAME


def _js_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def build_pi_extension(python: Path, agentwatch_cli: Path, events_dir: Path) -> str:
    """Return a dependency-free Pi extension using the official settled event."""
    return f"""{PI_MANAGED_MARKER}
import {{ spawn }} from "node:child_process";

const PYTHON = {_js_string(python)};
const AGENTWATCH = {_js_string(agentwatch_cli)};
const EVENTS_DIR = {_js_string(events_dir)};
const MESSAGE_LIMIT = 65536;
let runArmed = false;
let runStartLeaf = null;
const terminatingToolCalls = new Set();
const failedToolCalls = new Set();

function resetRun() {{
  runArmed = false;
  runStartLeaf = null;
  terminatingToolCalls.clear();
  failedToolCalls.clear();
}}

function messageText(message) {{
  const content = message?.content;
  if (typeof content === "string") return content.slice(0, MESSAGE_LIMIT);
  if (!Array.isArray(content)) return "";
  return content
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\\n")
    .slice(0, MESSAGE_LIMIT);
}}

function emit(record) {{
  try {{
    const child = spawn(
      PYTHON,
      [AGENTWATCH, "tool-hook", "--source", "pi", "--events-dir", EVENTS_DIR],
      {{ stdio: ["pipe", "ignore", "ignore"], windowsHide: true }},
    );
    child.on("error", () => {{}});
    child.stdin.on("error", () => {{}});
    child.stdin.end(JSON.stringify(record));
    child.unref();
  }} catch {{}}
}}

export default function agentwatchNotifications(pi) {{
  pi.on("agent_start", async (_event, ctx) => {{
    // Pi's official subagent example launches `--mode json --no-session`.
    // Requiring a persisted non-JSON session keeps child runs silent while
    // the parent session emits exactly one settled event.
    if (ctx.mode === "json" || !ctx.sessionManager.getSessionFile()) {{
      resetRun();
      return;
    }}
    if (!runArmed) {{
      const startLeaf = ctx.sessionManager.getLeafId();
      if (!startLeaf) {{
        resetRun();
        return;
      }}
      runArmed = true;
      runStartLeaf = startLeaf;
    }}
  }});

  pi.on("tool_execution_end", async (event) => {{
    if (!runArmed) return;
    const toolCallId = String(event.toolCallId || "");
    if (!toolCallId) return;
    if (event.result?.terminate === true) terminatingToolCalls.add(toolCallId);
    else terminatingToolCalls.delete(toolCallId);
    if (event.isError === true) failedToolCalls.add(toolCallId);
    else failedToolCalls.delete(toolCallId);
  }});

  pi.on("agent_settled", async (_event, ctx) => {{
    if (!runArmed) return;
    if (ctx.mode === "json" || !ctx.sessionManager.getSessionFile()) {{
      resetRun();
      return;
    }}
    const startLeaf = runStartLeaf;
    const settledTerminatingToolCalls = new Set(terminatingToolCalls);
    const settledFailedToolCalls = new Set(failedToolCalls);
    resetRun();
    if (!startLeaf) return;
    if (!ctx.isIdle() || ctx.hasPendingMessages()) return;

    const branch = ctx.sessionManager.getBranch();
    const startIndex = branch.findIndex((entry) => entry?.id === startLeaf);
    if (startIndex < 0) return;
    const additions = branch.slice(startIndex + 1);
    let terminalIndex = -1;
    for (let index = additions.length - 1; index >= 0; index -= 1) {{
      const entry = additions[index];
      if (entry?.type === "message" && entry.message?.role === "assistant") {{
        terminalIndex = index;
        break;
      }}
    }}
    if (terminalIndex < 0) return;
    const terminal = additions[terminalIndex];

    const reason = String(terminal.message?.stopReason || "");
    const terminalToolCalls = Array.isArray(terminal.message?.content)
      ? terminal.message.content.filter(
        (part) => part?.type === "toolCall" && typeof part.id === "string" && part.id,
      )
      : [];
    if (
      reason === "toolUse" &&
      (
        terminalToolCalls.length === 0 ||
        !terminalToolCalls.every((part) => settledTerminatingToolCalls.has(part.id))
      )
    ) return;

    const terminalToolFailed = terminalToolCalls.some((part) =>
      settledFailedToolCalls.has(part.id)
    );
    const outcome = reason === "stop" || (reason === "toolUse" && !terminalToolFailed)
      ? "completed"
      : reason === "aborted"
        ? "cancelled"
        : "error";
    let message = messageText(terminal.message);
    if (!message && reason === "toolUse") {{
      const terminalToolCallIds = new Set(terminalToolCalls.map((part) => part.id));
      message = additions
        .slice(terminalIndex + 1)
        .filter((entry) =>
          entry?.type === "message" &&
          entry.message?.role === "toolResult" &&
          terminalToolCallIds.has(entry.message?.toolCallId)
        )
        .map((entry) => messageText(entry.message))
        .filter(Boolean)
        .join("\\n")
        .slice(0, MESSAGE_LIMIT);
    }}
    const header = ctx.sessionManager.getHeader();
    emit({{
      schema: "agentwatch_pi_hook_v1",
      event_name: "agent_settled",
      session_id: ctx.sessionManager.getSessionId(),
      event_id: String(terminal.id || ""),
      timestamp: String(terminal.timestamp || new Date().toISOString()),
      cwd: String(ctx.cwd || ctx.sessionManager.getCwd() || ""),
      parent_session: String(header?.parentSession || ""),
      outcome,
      stop_reason: reason,
      message,
    }});
  }});
}}
"""


def build_opencode_plugin(python: Path, agentwatch_cli: Path, events_dir: Path) -> str:
    """Return an import-free OpenCode plugin using the official idle event."""
    return f"""{OPENCODE_MANAGED_MARKER}
import {{ spawnSync }} from "node:child_process";

const PYTHON = {_js_string(python)};
const AGENTWATCH = {_js_string(agentwatch_cli)};
const EVENTS_DIR = {_js_string(events_dir)};
const MESSAGE_LIMIT = 65536;
const DISPOSE_TIMEOUT_MS = 10000;
const armed = new Set();
const pendingBySession = new Map();
const pendingErrors = new Map();

function unwrap(response) {{
  return response && typeof response === "object" && "data" in response
    ? response.data
    : response;
}}

function emit(record) {{
  try {{
    const result = spawnSync(
      PYTHON,
      [
        AGENTWATCH,
        "tool-hook",
        "--source",
        "opencode",
        "--events-dir",
        EVENTS_DIR,
        "--require-persist",
      ],
      {{
        input: JSON.stringify(record),
        stdio: ["pipe", "ignore", "ignore"],
        windowsHide: true,
        timeout: 5000,
      }},
    );
    return !result.error && result.signal == null && result.status === 0;
  }} catch {{
    return false;
  }}
}}

function isIdleStatus(status) {{
  return status === "idle" || status?.type === "idle";
}}

export const AgentWatchNotifications = async ({{ client, directory }}) => {{
  async function settle(sessionID) {{
    if (!armed.has(sessionID)) return;
    try {{
      const query = {{ directory }};
      const session = unwrap(await client.session.get({{
        path: {{ id: sessionID }},
        query,
      }}));
      if (!session || session.parentID) {{
        armed.delete(sessionID);
        pendingErrors.delete(sessionID);
        return;
      }}

      const messages = unwrap(await client.session.messages({{
        path: {{ id: sessionID }},
        query,
      }}));
      if (!Array.isArray(messages)) return;
      const lastUserIndex = messages.findLastIndex((item) => item?.info?.role === "user");
      const lastUser = lastUserIndex >= 0 ? messages[lastUserIndex] : null;
      const currentTurn = lastUserIndex >= 0 ? messages.slice(lastUserIndex + 1) : messages;
      const terminal = currentTurn
        .slice()
        .reverse()
        .find((item) =>
          item?.info?.role === "assistant" &&
          !item.info.summary &&
          item.info.time?.completed &&
          item.info.finish !== "tool-calls"
        );
      const fallbackError = pendingErrors.get(sessionID);
      if (!terminal && !fallbackError) return;

      const info = terminal?.info || {{}};
      const errorName = String(info.error?.name || fallbackError?.name || "");
      const outcome = errorName === "MessageAbortedError"
        ? "cancelled"
        : errorName
          ? "error"
          : "completed";
      const eventID = String(info.id || `error:${{lastUser?.info?.id || "unknown"}}:${{errorName}}`);
      const body = Array.isArray(terminal?.parts)
        ? terminal.parts
          .filter((part) => part?.type === "text" && !part.ignored && typeof part.text === "string")
          .map((part) => part.text)
          .join("\\n")
          .slice(0, MESSAGE_LIMIT)
        : "";
      const persisted = emit({{
        schema: "agentwatch_opencode_hook_v1",
        event_name: "session.idle",
        session_id: sessionID,
        event_id: eventID,
        timestamp: Number(info.time?.completed || Date.now()),
        cwd: String(session.directory || info.path?.cwd || directory || ""),
        parent_session: String(session.parentID || ""),
        outcome,
        stop_reason: String(info.finish || errorName || ""),
        message: body,
      }});
      if (!persisted) return;
      armed.delete(sessionID);
      pendingErrors.delete(sessionID);
    }} catch {{
      // Fail closed. A repeated official idle event may retry this read, but
      // the plugin never guesses from logs or sends a network notification.
    }}
  }}

  function scheduleSettle(sessionID) {{
    if (!sessionID || !armed.has(sessionID)) return null;
    const current = pendingBySession.get(sessionID);
    if (current) return current;
    const task = settle(sessionID);
    pendingBySession.set(sessionID, task);
    void task.then(
      () => {{
        if (pendingBySession.get(sessionID) === task) pendingBySession.delete(sessionID);
      }},
      () => {{
        if (pendingBySession.get(sessionID) === task) pendingBySession.delete(sessionID);
      }},
    );
    return task;
  }}

  async function drainPending() {{
    const deadline = Date.now() + DISPOSE_TIMEOUT_MS;
    while (pendingBySession.size > 0) {{
      const remaining = deadline - Date.now();
      if (remaining <= 0) return;
      const tasks = Array.from(pendingBySession.values());
      const completed = await new Promise((resolve) => {{
        let finished = false;
        const timer = setTimeout(() => {{
          if (finished) return;
          finished = true;
          resolve(false);
        }}, remaining);
        void Promise.allSettled(tasks).then(() => {{
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          resolve(true);
        }});
      }});
      if (!completed) return;
    }}
  }}

  return {{
    event: async ({{ event }}) => {{
      if (event.type === "message.updated" && event.properties?.info?.role === "user") {{
        armed.add(event.properties.info.sessionID);
        return;
      }}
      if (event.type === "session.status") {{
        const sessionID = event.properties?.sessionID;
        if (!sessionID) return;
        if (isIdleStatus(event.properties?.status)) scheduleSettle(sessionID);
        else armed.add(sessionID);
        return;
      }}
      if (event.type === "session.error") {{
        const sessionID = event.properties?.sessionID;
        if (sessionID) {{
          armed.add(sessionID);
          pendingErrors.set(sessionID, {{ name: String(event.properties?.error?.name || "UnknownError") }});
        }}
        return;
      }}
      if (event.type === "session.idle") {{
        const sessionID = event.properties?.sessionID;
        if (sessionID) scheduleSettle(sessionID);
      }}
    }},
    dispose: async () => {{
      // OpenCode dispatches event hooks fire-and-forget. Its awaited dispose
      // lifecycle (1.15.11+) is the reliable headless shutdown barrier.
      for (const sessionID of Array.from(armed)) scheduleSettle(sessionID);
      await drainPending();
    }},
  }};
}};
"""


def _managed_text(path: Path, marker: str) -> str | None:
    if not os.path.lexists(path):
        return None
    if path_has_link_component(path):
        raise AgentWatchError(f"refusing linked managed integration path: {path}")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AgentWatchError(f"cannot inspect managed integration: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise AgentWatchError(f"managed integration must be a regular file: {path}")
    if metadata.st_size > MAX_MANAGED_FILE_BYTES:
        raise AgentWatchError(f"managed integration is unexpectedly large: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise AgentWatchError(f"managed integration is not owned by the current user: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AgentWatchError(f"managed integration is not valid UTF-8: {path}") from exc
    if not text.startswith(marker + "\n"):
        raise AgentWatchError(f"refusing to replace a non-AgentWatch integration: {path}")
    return text


def managed_integration_sha256(path: Path, marker: str) -> str | None:
    current = _managed_text(path, marker)
    if current is None:
        return None
    return hashlib.sha256(current.encode("utf-8")).hexdigest()


def _require_expected_digest(
    current: str | None,
    path: Path,
    accepted_sha256: set[str] | frozenset[str] | None,
) -> None:
    if accepted_sha256 is None:
        return
    digest = "" if current is None else hashlib.sha256(current.encode("utf-8")).hexdigest()
    if digest not in accepted_sha256:
        raise AgentWatchError(
            f"managed integration changed after AgentWatch registered it: {path}"
        )


def preflight_managed_integration(
    path: Path,
    marker: str,
    *,
    accepted_sha256: set[str] | frozenset[str] | None = None,
) -> None:
    if path_has_link_component(path):
        raise AgentWatchError(f"refusing linked integration path: {path}")
    current = _managed_text(path, marker)
    _require_expected_digest(current, path, accepted_sha256)


def configure_managed_integration(
    path: Path,
    marker: str,
    desired: str,
    *,
    enabled: bool,
    accepted_sha256: set[str] | frozenset[str] | None = None,
) -> bool:
    current = _managed_text(path, marker)
    _require_expected_digest(current, path, accepted_sha256)
    if not enabled:
        if current is None:
            return False
        path.unlink()
        return True
    if current == desired:
        return False
    if path_has_link_component(path.parent):
        raise AgentWatchError(f"refusing linked integration directory: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path_has_link_component(path):
        raise AgentWatchError(f"refusing linked integration path: {path}")
    atomic_write(path, desired.encode("utf-8"), mode=0o600)
    return True


def inspect_managed_integration(path: Path, marker: str, desired: str) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path),
        "configured": False,
        "current": False,
        "path_safe": True,
    }
    try:
        current = _managed_text(path, marker)
    except AgentWatchError as exc:
        result["path_safe"] = False
        result["error"] = str(exc)
        return result
    result["configured"] = current is not None
    result["current"] = current == desired
    return result
