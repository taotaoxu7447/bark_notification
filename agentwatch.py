#!/usr/bin/env python3
"""Cross-platform AgentWatch computer command line interface."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable

from agentwatch_core import (
    AgentWatchApi,
    AgentWatchError,
    ApiError,
    ComputerTokenStore,
    atomic_write,
    api_base,
    config_dir,
    infer_delivery_mode,
    load_delivery_mode,
    load_machine,
    load_or_create_machine,
    path_has_link_component,
    resolve_delivery,
    save_delivery_mode,
    save_machine_account,
)
from claude_hook_config import (
    CLAUDE_HOOK_MANAGED_ID,
    CLAUDE_SETTINGS_BACKUP_FILE,
    build_claude_hook_handler,
    claude_managed_settings_path,
    claude_settings_path,
    configure_claude_hooks,
    inspect_claude_hooks,
    preflight_claude_hooks,
)


VERSION = "0.3.0"
MACOS_LABEL = "com.xutao.codex-watch-notifier"
LINUX_UNIT = "codex-watch-notifier.service"
WINDOWS_TASK = "CodexWatchNotifier"
RUNTIME_FILES = (
    "agentwatch.py",
    "agentwatch_core.py",
    "codex_watch_notifier.py",
    "claude_hook_config.py",
    "env.example",
)
RUNNING_SERVICE_STATES = {"running", "active"}
BARK_UPDATE_INSTRUCTION = (
    "请私下将 BARK_URL 或 BARK_KEY 写入持久 env，然后运行 agentwatch update 以启用 Bark"
)
CLAUDE_HOOK_EVENTS_FILE_NAME = "claude-hook-events.jsonl"
CLAUDE_HOOK_REGISTRATION_FILE_NAME = "claude-hook-registration.json"
CLAUDE_SPOOL_OWNERSHIP_FILE_NAME = "claude-spool-ownership.json"
CLAUDE_HOOK_SCHEMA = "agentwatch_claude_hook_v1"
CLAUDE_HOOK_INPUT_LIMIT_BYTES = 1024 * 1024
CLAUDE_HOOK_MESSAGE_LIMIT_CHARS = 64 * 1024
CLAUDE_APPEND_LOCK_TIMEOUT_SECONDS = 4.0
SERVICE_STATE_TIMEOUT_SECONDS = 8.0
SERVICE_STATE_POLL_SECONDS = 0.2
# AgentWatch relies on the complete modern Stop payload contract. Exec-form
# hook args arrived earlier, but background_tasks/session_crons require 2.1.145
# and the prompt_id used as the primary per-turn de-duplication key requires
# 2.1.196.
MIN_CLAUDE_HOOK_VERSION = (2, 1, 196)
CLAUDE_STOP_FAILURE_ERRORS = frozenset(
    {
        "rate_limit",
        "overloaded",
        "authentication_failed",
        "oauth_org_not_allowed",
        "billing_error",
        "invalid_request",
        "model_not_found",
        "server_error",
        "max_output_tokens",
        "unknown",
    }
)


class DeliveryModeRequired(AgentWatchError):
    """Raised when a headless install has no safe receiver choice to infer."""


class RejectPasswordAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        del namespace, values, option_string
        parser.error("passwords are accepted only through the hidden interactive prompt")


def _run(
    command: list[str],
    *,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        timeout=timeout,
    )


def reject_symlink_path(path: Path, boundary: Path) -> None:
    """Reject links in installer-controlled path components.

    The boundary itself may be a platform alias such as macOS `/tmp`; only
    descendants that this installer owns are checked.
    """
    target = Path(os.path.abspath(path))
    root = Path(os.path.abspath(boundary))
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise AgentWatchError(f"unsafe installation path outside {root}: {target}") from None
    if path_has_link_component(target):
        raise AgentWatchError(f"refusing symlink or junction in installation path: {target}")


def systemd_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


class InstallPaths:
    def __init__(self, root: Path | None = None, home: Path | None = None) -> None:
        self.config = root or config_dir()
        self.home = home or Path.home()
        self.runtime = self.config / "bin"
        self.launcher_dir = self.home / ".local" / "bin"
        self.launcher = self.launcher_dir / ("agentwatch.cmd" if platform.system() == "Windows" else "agentwatch")
        self.macos_plist = self.home / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
        self.linux_unit = self.home / ".config" / "systemd" / "user" / LINUX_UNIT


def claude_hook_events_path() -> Path:
    configured = os.getenv("CLAUDE_WATCH_EVENTS_FILE", "").strip()
    if configured:
        expanded = Path(os.path.expandvars(os.path.expanduser(configured)))
        return Path(os.path.abspath(expanded))
    return config_dir() / CLAUDE_HOOK_EVENTS_FILE_NAME


def _claude_hook_string(
    payload: dict[str, Any],
    name: str,
    *,
    required: bool = False,
    limit: int,
) -> str | None:
    value = payload.get(name)
    if value is None:
        return None if not required else ""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if required and not normalized:
        return ""
    if len(normalized) > limit:
        return None
    return normalized


@contextmanager
def _claude_spool_append_lock(path: Path) -> Any:
    lock_path = path.with_name(path.name + ".append.lock")
    if path_has_link_component(lock_path):
        raise AgentWatchError("Claude hook spool lock path must not contain a symlink or junction")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    acquired = False
    try:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise AgentWatchError("Claude hook spool lock is not a regular file")
        try:
            os.fchmod(handle.fileno(), 0o600)
        except (AttributeError, OSError):
            pass
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            handle.seek(0)
            lock_once = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            lock_once = lambda: fcntl.flock(
                handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        deadline = time.monotonic() + CLAUDE_APPEND_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                lock_once()
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AgentWatchError("Claude hook spool append lock is busy") from None
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _append_private_jsonl(path: Path, record: dict[str, Any]) -> None:
    if path_has_link_component(path):
        raise AgentWatchError(
            "Claude hook event spool path must not contain a symlink or junction"
        )
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    with _claude_spool_append_lock(path):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise AgentWatchError("Claude hook event spool is not a regular file")
            try:
                os.fchmod(descriptor, 0o600)
            except (AttributeError, OSError):
                pass
            encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if os.write(descriptor, encoded) != len(encoded):
                raise AgentWatchError("could not append the complete Claude hook event")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def ingest_claude_hook_event(
    stream: Any = None,
    *,
    events_path: Path | None = None,
) -> bool:
    """Validate one official Claude Stop/StopFailure payload and append a safe subset.

    This command is deliberately non-blocking for Claude Code: callers should
    ignore a False result and still exit zero. The transcript is never parsed;
    only its current byte size is recorded for stable event deduplication.
    """
    source = stream if stream is not None else sys.stdin
    reader = getattr(source, "buffer", source)
    raw = reader.read(CLAUDE_HOOK_INPUT_LIMIT_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes) or not raw or len(raw) > CLAUDE_HOOK_INPUT_LIMIT_BYTES:
        return False
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False

    hook_event_name = _claude_hook_string(payload, "hook_event_name", required=True, limit=32)
    if hook_event_name not in {"Stop", "StopFailure"}:
        return False
    session_id = _claude_hook_string(payload, "session_id", required=True, limit=256)
    prompt_id = _claude_hook_string(payload, "prompt_id", limit=256)
    if prompt_id is None and payload.get("prompt_id") is not None:
        return False
    transcript_path = _claude_hook_string(payload, "transcript_path", required=True, limit=4096)
    cwd = _claude_hook_string(payload, "cwd", required=True, limit=4096)
    if not session_id or not transcript_path or not cwd:
        return False

    message_value = payload.get("last_assistant_message", "")
    if not isinstance(message_value, str):
        return False
    message = message_value.strip()
    if hook_event_name == "Stop" and not message:
        return False
    message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    stored_message = message[:CLAUDE_HOOK_MESSAGE_LIMIT_CHARS]

    error = ""
    error_details = ""
    if hook_event_name == "StopFailure":
        parsed_error = _claude_hook_string(payload, "error", required=True, limit=64)
        if parsed_error not in CLAUDE_STOP_FAILURE_ERRORS:
            return False
        error = parsed_error
        parsed_details = _claude_hook_string(payload, "error_details", limit=4096)
        if parsed_details is None and payload.get("error_details") is not None:
            return False
        error_details = parsed_details or ""

    background_tasks = payload.get("background_tasks", [])
    session_crons = payload.get("session_crons", [])
    stop_hook_active = False
    if hook_event_name == "Stop":
        stop_hook_active = payload.get("stop_hook_active", False)
        if not isinstance(stop_hook_active, bool):
            return False
        if not isinstance(background_tasks, list) or not isinstance(session_crons, list):
            return False
    else:
        background_tasks = []
        session_crons = []

    expanded_transcript = Path(os.path.expandvars(os.path.expanduser(transcript_path)))
    try:
        transcript_size = expanded_transcript.stat().st_size
    except OSError:
        transcript_size = -1

    record = {
        "schema": CLAUDE_HOOK_SCHEMA,
        "hook_event_name": hook_event_name,
        "session_id": session_id,
        "prompt_id": prompt_id or "",
        "transcript_path": transcript_path,
        "transcript_size": transcript_size,
        "cwd": cwd,
        "received_at": int(time.time()),
        "last_assistant_message": stored_message,
        "last_assistant_message_sha256": message_hash,
        "error": error,
        "error_details": error_details,
        "stop_hook_active": stop_hook_active,
        "has_background_tasks": bool(background_tasks),
        "has_session_crons": bool(session_crons),
    }
    _append_private_jsonl(events_path or claude_hook_events_path(), record)
    return True


class ServiceManager:
    def __init__(self, paths: InstallPaths, system_name: str | None = None) -> None:
        self.paths = paths
        self.system_name = system_name or platform.system()

    def install(
        self,
        should_start: bool | None = None,
        *,
        authenticated: bool | None = None,
    ) -> None:
        # `authenticated=` remains accepted for callers of the v0.2.0 Python
        # surface, but the boolean now means "at least one selected receiver is
        # operational" rather than specifically "has an AgentWatch token".
        if should_start is None:
            should_start = bool(authenticated)
        if self.system_name == "Darwin":
            self._install_macos(should_start)
        elif self.system_name == "Linux":
            self._install_linux(should_start)
        elif self.system_name == "Windows":
            self._install_windows(should_start)
        else:
            raise AgentWatchError(f"unsupported operating system: {self.system_name}")

    def start(self) -> None:
        if self.system_name == "Darwin":
            target = f"gui/{os.getuid()}/{MACOS_LABEL}"
            domain = f"gui/{os.getuid()}"
            self._attempt(["launchctl", "bootout", target])
            self._attempt(["launchctl", "enable", target])
            result = _run(["launchctl", "bootstrap", domain, str(self.paths.macos_plist)])
            if result.returncode != 0 and "already loaded" not in result.stderr.lower():
                raise AgentWatchError("could not start the AgentWatch LaunchAgent")
            result = _run(["launchctl", "kickstart", "-k", target])
            if result.returncode != 0:
                raise AgentWatchError("could not start the AgentWatch LaunchAgent")
            self._wait_for_state(
                lambda: self._macos_service_state() == "running"
                and not self._macos_service_disabled(),
                "AgentWatch LaunchAgent is not confirmed enabled and running",
            )
        elif self.system_name == "Linux":
            self._attempt(["systemctl", "--user", "enable", "--now", LINUX_UNIT])
            self._wait_for_state(
                lambda: self._linux_service_snapshot()
                == ("loaded", "active", "enabled"),
                "AgentWatch systemd user service is not confirmed enabled and running",
            )
        elif self.system_name == "Windows":
            self._attempt(["schtasks.exe", "/Change", "/TN", WINDOWS_TASK, "/Enable"])
            self._attempt(["schtasks.exe", "/Run", "/TN", WINDOWS_TASK])
            self._wait_for_state(
                lambda: self._windows_task_snapshot() == (True, "running", True),
                "AgentWatch scheduled task is not confirmed enabled and running",
            )
        else:
            raise AgentWatchError(f"unsupported operating system: {self.system_name}")

    def stop(self) -> None:
        if self.system_name == "Darwin":
            target = f"gui/{os.getuid()}/{MACOS_LABEL}"
            self._attempt(["launchctl", "bootout", target])
            self._attempt(["launchctl", "disable", target])
            self._wait_for_state(
                lambda: self._macos_service_state() == "stopped"
                and self._macos_service_disabled(),
                "AgentWatch LaunchAgent is not confirmed stopped and disabled",
            )
        elif self.system_name == "Linux":
            self._attempt(["systemctl", "--user", "stop", LINUX_UNIT])
            self._attempt(["systemctl", "--user", "disable", LINUX_UNIT])
            self._wait_for_state(
                lambda: self._linux_snapshot_is_stopped(
                    self._linux_service_snapshot()
                ),
                "AgentWatch systemd user service is not confirmed stopped and disabled",
            )
        elif self.system_name == "Windows":
            self._attempt(["schtasks.exe", "/End", "/TN", WINDOWS_TASK])
            self._attempt(["schtasks.exe", "/Change", "/TN", WINDOWS_TASK, "/Disable"])
            self._wait_for_state(
                lambda: self._windows_snapshot_is_stopped(
                    self._windows_task_snapshot()
                ),
                "AgentWatch scheduled task is not confirmed stopped and disabled",
            )
        else:
            raise AgentWatchError(f"unsupported operating system: {self.system_name}")

    @staticmethod
    def _attempt(command: list[str]) -> subprocess.CompletedProcess[str] | None:
        """Run a cleanup command while deferring success to a state check.

        Stop/delete tools commonly return a failure when an object was already
        absent.  Conversely, a successful exit code is not proof that a daemon
        or task disappeared.  Uninstall therefore treats command results only
        as requests and makes the platform state query authoritative.
        """
        try:
            return _run(command)
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _wait_for_state(check: Callable[[], bool], message: str) -> None:
        """Poll asynchronous service managers for a bounded eight seconds."""
        deadline = time.monotonic() + SERVICE_STATE_TIMEOUT_SECONDS
        last_error: AgentWatchError | None = None
        while True:
            try:
                if check():
                    return
                last_error = None
            except AgentWatchError as exc:
                # A transient query failure is not success.  Retry within the
                # same fixed bound, then surface uncertainty as a hard error.
                last_error = exc
            if time.monotonic() >= deadline:
                if last_error is not None:
                    raise AgentWatchError(message) from last_error
                raise AgentWatchError(message)
            time.sleep(SERVICE_STATE_POLL_SECONDS)

    def _macos_service_state(self) -> str:
        target = f"gui/{os.getuid()}/{MACOS_LABEL}"
        try:
            result = _run(["launchctl", "print", target])
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentWatchError(
                "could not verify whether the AgentWatch LaunchAgent is still loaded"
            ) from exc
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("state ="):
                    state = stripped.split("=", 1)[1].strip().lower()
                    return state or "loaded"
            return "loaded"
        diagnostic = f"{result.stdout}\n{result.stderr}".lower()
        absent_markers = (
            "could not find service",
            "service could not be found",
            "no such process",
        )
        if any(marker in diagnostic for marker in absent_markers):
            return "stopped"
        raise AgentWatchError(
            "could not verify whether the AgentWatch LaunchAgent is still loaded"
        )

    def _macos_service_loaded(self) -> bool:
        return self._macos_service_state() != "stopped"

    def _macos_service_disabled(self) -> bool:
        domain = f"gui/{os.getuid()}"
        try:
            result = _run(["launchctl", "print-disabled", domain])
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentWatchError(
                "could not verify whether the AgentWatch LaunchAgent is disabled"
            ) from exc
        if result.returncode != 0 or "disabled services" not in result.stdout.lower():
            raise AgentWatchError(
                "could not verify whether the AgentWatch LaunchAgent is disabled"
            )
        pattern = re.compile(
            rf'["\']?{re.escape(MACOS_LABEL)}["\']?\s*=>\s*(true|false)',
            re.IGNORECASE,
        )
        match = pattern.search(result.stdout)
        return bool(match and match.group(1).lower() == "true")

    def _linux_service_snapshot(self) -> tuple[str, str, str]:
        command = [
            "systemctl",
            "--user",
            "show",
            LINUX_UNIT,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=UnitFileState",
        ]
        try:
            result = _run(command)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentWatchError(
                "could not verify the AgentWatch systemd user service state"
            ) from exc
        if result.returncode != 0:
            raise AgentWatchError(
                "could not verify the AgentWatch systemd user service state"
            )
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip().lower()
        load_state = values.get("LoadState", "")
        active_state = values.get("ActiveState", "")
        unit_file_state = values.get("UnitFileState", "")
        if not load_state or not active_state:
            raise AgentWatchError(
                "could not verify the AgentWatch systemd user service state"
            )
        return load_state, active_state, unit_file_state

    @staticmethod
    def _linux_snapshot_is_stopped(snapshot: tuple[str, str, str]) -> bool:
        load_state, active_state, unit_file_state = snapshot
        if active_state not in {"inactive", "failed"}:
            return False
        if load_state == "not-found":
            return unit_file_state in {"", "disabled", "masked", "not-found"}
        return unit_file_state in {"disabled", "masked", "not-found"}

    @staticmethod
    def _linux_snapshot_is_removed(snapshot: tuple[str, str, str]) -> bool:
        load_state, active_state, _unit_file_state = snapshot
        return load_state == "not-found" and active_state in {"inactive", "failed"}

    def _windows_task_snapshot(self) -> tuple[bool, str, bool]:
        # Enumerating and filtering avoids conflating a genuinely absent task
        # with a localized Get-ScheduledTask "not found" error.  The sentinel
        # output stays machine-readable across Windows display languages.
        escaped_name = WINDOWS_TASK.replace("'", "''")
        script = (
            "$ErrorActionPreference='Stop';"
            "$tasks=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {"
            f"$_.TaskName -ceq '{escaped_name}' -and $_.TaskPath -eq '\\'"
            "});"
            "if($tasks.Count -eq 0){Write-Output 'agentwatch:absent';exit 0};"
            "if($tasks.Count -ne 1){Write-Output 'agentwatch:ambiguous';exit 3};"
            "$enabled=$tasks[0].Settings.Enabled;"
            "Write-Output ('agentwatch:present:'+$tasks[0].State.ToString().ToLowerInvariant()+':'"
            "+$enabled.ToString().ToLowerInvariant())"
        )
        try:
            result = _run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ]
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentWatchError(
                "could not verify whether the AgentWatch scheduled task still exists"
            ) from exc
        if result.returncode != 0:
            raise AgentWatchError(
                "could not verify whether the AgentWatch scheduled task still exists"
            )
        lines = [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]
        if lines == ["agentwatch:absent"]:
            return False, "absent", False
        if len(lines) == 1 and lines[0].startswith("agentwatch:present:"):
            parts = lines[0].split(":")
            if len(parts) != 4 or parts[3] not in {"true", "false"}:
                raise AgentWatchError(
                    "could not verify whether the AgentWatch scheduled task is enabled"
                )
            state = parts[2]
            if state not in {"ready", "disabled", "running", "queued"}:
                raise AgentWatchError(
                    "could not verify whether the AgentWatch scheduled task is stopped"
                )
            return True, state, parts[3] == "true"
        raise AgentWatchError(
            "could not verify whether the AgentWatch scheduled task still exists"
        )

    @staticmethod
    def _windows_snapshot_is_stopped(snapshot: tuple[bool, str, bool]) -> bool:
        exists, state, enabled = snapshot
        return not exists or (state in {"ready", "disabled"} and not enabled)

    def uninstall(self) -> None:
        if self.system_name == "Darwin":
            reject_symlink_path(self.paths.macos_plist, self.paths.home)
        elif self.system_name == "Linux":
            reject_symlink_path(self.paths.linux_unit, self.paths.home)
        if self.system_name == "Darwin":
            self.stop()
            try:
                self.paths.macos_plist.unlink()
            except FileNotFoundError:
                pass
            self._wait_for_state(
                lambda: not self._macos_service_loaded(),
                "AgentWatch LaunchAgent removal could not be verified; runtime files were preserved",
            )
        elif self.system_name == "Linux":
            self.stop()
            try:
                self.paths.linux_unit.unlink()
            except FileNotFoundError:
                pass
            self._attempt(["systemctl", "--user", "daemon-reload"])
            self._wait_for_state(
                lambda: self._linux_snapshot_is_removed(
                    self._linux_service_snapshot()
                ),
                "AgentWatch systemd user service removal could not be verified; "
                "runtime files were preserved",
            )
        elif self.system_name == "Windows":
            self.stop()
            exists, state, enabled = self._windows_task_snapshot()
            if exists and not self._windows_snapshot_is_stopped((exists, state, enabled)):
                raise AgentWatchError(
                    "AgentWatch scheduled task is not confirmed stopped and disabled; "
                    "runtime files were preserved"
                )
            if exists:
                self._attempt(["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK, "/F"])
            self._wait_for_state(
                lambda: not self._windows_task_snapshot()[0],
                "AgentWatch scheduled task still exists; runtime files were preserved",
            )
        else:
            raise AgentWatchError(f"unsupported operating system: {self.system_name}")

    def state(self) -> str:
        if self.system_name == "Darwin":
            result = _run(["launchctl", "print", f"gui/{os.getuid()}/{MACOS_LABEL}"])
            if result.returncode != 0:
                return "stopped"
            for line in result.stdout.splitlines():
                if line.strip().startswith("state ="):
                    return line.split("=", 1)[1].strip()
            return "loaded"
        if self.system_name == "Linux":
            result = _run(["systemctl", "--user", "is-active", LINUX_UNIT])
            return result.stdout.strip() or "stopped"
        if self.system_name == "Windows":
            result = _run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-ScheduledTask -TaskName '{WINDOWS_TASK}' -ErrorAction Stop).State.ToString()",
                ]
            )
            if result.returncode != 0:
                return "stopped"
            return result.stdout.strip().lower() or "installed"
        return "unsupported"

    def installed(self) -> bool:
        if self.system_name == "Darwin":
            return self.paths.macos_plist.exists()
        if self.system_name == "Linux":
            return self.paths.linux_unit.exists()
        if self.system_name == "Windows":
            return _run(["schtasks.exe", "/Query", "/TN", WINDOWS_TASK]).returncode == 0
        return False

    def _install_macos(self, should_start: bool) -> None:
        self.stop()  # Stops a v0.1 process before it can use a legacy shared topic.
        reject_symlink_path(self.paths.macos_plist, self.paths.home)
        self.paths.macos_plist.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": MACOS_LABEL,
            "ProgramArguments": [sys.executable, str(self.paths.runtime / "codex_watch_notifier.py")],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(self.paths.config / "launchd.out.log"),
            "StandardErrorPath": str(self.paths.config / "launchd.err.log"),
            "WorkingDirectory": str(self.paths.runtime),
            "EnvironmentVariables": {"AGENTWATCH_CONFIG_DIR": str(self.paths.config)},
        }
        atomic_write(
            self.paths.macos_plist,
            plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True),
            mode=0o644,
        )
        if should_start:
            self.start()
        else:
            # Re-check after the new definition exists; a failed disable must
            # never leave a freshly installed watcher running while install
            # reports success.
            self.stop()

    def _install_linux(self, should_start: bool) -> None:
        self.stop()
        reject_symlink_path(self.paths.linux_unit, self.paths.home)
        self.paths.linux_unit.parent.mkdir(parents=True, exist_ok=True)
        unit = f"""[Unit]
Description=AgentWatch AI task notifier
After=network-online.target

[Service]
Type=simple
Environment={systemd_quote('AGENTWATCH_CONFIG_DIR=' + str(self.paths.config))}
WorkingDirectory={systemd_quote(str(self.paths.runtime))}
ExecStart={systemd_quote(sys.executable)} {systemd_quote(str(self.paths.runtime / 'codex_watch_notifier.py'))}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
        atomic_write(self.paths.linux_unit, unit.encode("utf-8"), mode=0o600)
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        if reload_result.returncode != 0:
            raise AgentWatchError("could not reload the systemd user manager")
        if should_start:
            self.start()
        else:
            self.stop()

    def _install_windows(self, should_start: bool) -> None:
        self.stop()
        run_script = self.paths.runtime / "run_notifier.ps1"
        reject_symlink_path(run_script, self.paths.config.parent)
        task_command = (
            "-NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f'-WindowStyle Hidden -File "{run_script}"'
        )
        escaped_task_command = task_command.replace("'", "''")
        register_script = (
            "$ErrorActionPreference='Stop';"
            f"$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '{escaped_task_command}';"
            "$trigger=New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;"
            "$principal=New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel LeastPrivilege;"
            "$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1);"
            f"Register-ScheduledTask -TaskName '{WINDOWS_TASK}' -Action $action -Trigger $trigger "
            "-Principal $principal -Settings $settings -Force | Out-Null"
        )
        result = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                register_script,
            ]
        )
        if result.returncode != 0:
            raise AgentWatchError("could not install the AgentWatch scheduled task")
        if should_start:
            self.start()
        else:
            self.stop()


def install_runtime(paths: InstallPaths, source: Path | None = None) -> None:
    source_root = source or Path(__file__).resolve().parent
    reject_symlink_path(paths.config, paths.config.parent)
    reject_symlink_path(paths.runtime, paths.config.parent)
    reject_symlink_path(paths.launcher, paths.home)
    paths.config.mkdir(parents=True, exist_ok=True)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(paths.config, 0o700)
        os.chmod(paths.runtime, 0o700)
    except OSError:
        pass
    for filename in RUNTIME_FILES:
        source_path = source_root / filename
        if not source_path.exists():
            raise AgentWatchError(f"installation package is missing {filename}")
        destination = paths.runtime / filename
        reject_symlink_path(destination, paths.config.parent)
        if source_path.resolve() != destination.resolve():
            atomic_write(
                destination,
                source_path.read_bytes(),
                mode=0o700 if destination.suffix == ".py" else 0o600,
            )

    env_path = paths.config / "env"
    reject_symlink_path(env_path, paths.config.parent)
    if not env_path.exists():
        atomic_write(env_path, (source_root / "env.example").read_bytes(), mode=0o600)

    paths.launcher_dir.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows":
        run_script = paths.runtime / "run_notifier.ps1"
        reject_symlink_path(run_script, paths.config.parent)
        watcher = paths.runtime / "codex_watch_notifier.py"
        out_log = paths.config / "task.out.log"
        err_log = paths.config / "task.err.log"
        powershell = f'''$ErrorActionPreference = "Stop"
$env:AGENTWATCH_CONFIG_DIR = '{str(paths.config).replace("'", "''")}'
& "{sys.executable}" "{watcher}" 1>> "{out_log}" 2>> "{err_log}"
exit $LASTEXITCODE
'''
        atomic_write(run_script, powershell.encode("utf-8"), mode=0o700)
        launcher = f'@echo off\r\n"{sys.executable}" "{paths.runtime / "agentwatch.py"}" %*\r\n'
    else:
        launcher = (
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(str(paths.runtime / "agentwatch.py"))
            + ' "$@"\n'
        )
    atomic_write(paths.launcher, launcher.encode("utf-8"), mode=0o700)


def _config_values(paths: InstallPaths) -> dict[str, str]:
    """Read only configuration that the installed background service can see.

    A shell-local BARK_URL/BARK_KEY is useful for a foreground watcher, but it
    is not reliably inherited by launchd, systemd, or Task Scheduler. Service
    readiness must therefore be based on the persistent private env file.
    """
    values: dict[str, str] = {}
    env_path = paths.config / "env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition("=")
            if separator and key.strip():
                values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        raise AgentWatchError(
            f"AgentWatch private env is unreadable or not valid UTF-8: {env_path}"
        ) from exc
    return values


def _configured_api_base(
    paths: InstallPaths,
    values: dict[str, str] | None = None,
) -> str:
    explicit = os.getenv("AGENTWATCH_API_BASE", "").strip()
    persistent = (values if values is not None else _config_values(paths)).get(
        "AGENTWATCH_API_BASE", ""
    ).strip()
    return (explicit or persistent or api_base()).rstrip("/")


def _claude_watch_enabled(paths: InstallPaths) -> bool:
    configured = _config_values(paths).get("CLAUDE_WATCH_ENABLED", "1").strip().lower()
    return configured not in {"0", "false", "no", "off"}


def _installed_claude_events_path(paths: InstallPaths) -> Path:
    configured = _config_values(paths).get("CLAUDE_WATCH_EVENTS_FILE", "").strip()
    if not configured:
        return paths.config / CLAUDE_HOOK_EVENTS_FILE_NAME
    expanded = Path(os.path.expandvars(os.path.expanduser(configured)))
    if not expanded.is_absolute():
        raise AgentWatchError("CLAUDE_WATCH_EVENTS_FILE must be an absolute path or start with ~")
    return Path(os.path.abspath(expanded))


def _claude_spool_ownership_path(paths: InstallPaths) -> Path:
    return paths.config / CLAUDE_SPOOL_OWNERSHIP_FILE_NAME


def _load_claude_spool_ownership(paths: InstallPaths) -> Path | None:
    ownership_path = _claude_spool_ownership_path(paths)
    if not os.path.lexists(ownership_path):
        return None
    reject_symlink_path(ownership_path, paths.config.parent)
    if ownership_path.is_symlink() or not ownership_path.is_file():
        raise AgentWatchError("Claude spool ownership record must be a regular file")
    try:
        payload = json.loads(ownership_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentWatchError("Claude spool ownership record is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise AgentWatchError("Claude spool ownership record has an unsupported format")
    raw_events_path = payload.get("events_path")
    if not isinstance(raw_events_path, str) or not raw_events_path.strip():
        raise AgentWatchError("Claude spool ownership record is missing events_path")
    events_path = Path(raw_events_path)
    if not events_path.is_absolute():
        raise AgentWatchError("Claude spool ownership events_path must be absolute")
    return Path(os.path.abspath(events_path))


def _save_claude_spool_ownership(paths: InstallPaths, events_path: Path) -> None:
    ownership_path = _claude_spool_ownership_path(paths)
    reject_symlink_path(ownership_path, paths.config.parent)
    payload = {
        "version": 1,
        "events_path": str(Path(os.path.abspath(events_path))),
    }
    atomic_write(
        ownership_path,
        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
        mode=0o600,
    )


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(parent)))
    except ValueError:
        return False
    return True


def _validate_installed_claude_events_path(paths: InstallPaths) -> Path:
    """Require a dedicated private spool, never an arbitrary existing file."""
    events_path = _installed_claude_events_path(paths)
    default_path = paths.config / CLAUDE_HOOK_EVENTS_FILE_NAME
    if path_has_link_component(events_path):
        raise AgentWatchError(
            "CLAUDE_WATCH_EVENTS_FILE must not contain a symlink or junction"
        )
    if os.path.lexists(events_path):
        try:
            metadata = events_path.lstat()
        except OSError as exc:
            raise AgentWatchError("CLAUDE_WATCH_EVENTS_FILE cannot be inspected") from exc
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise AgentWatchError("CLAUDE_WATCH_EVENTS_FILE must be a regular file")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AgentWatchError(
                "CLAUDE_WATCH_EVENTS_FILE must be private (0600), owner-readable "
                "and owner-writable"
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise AgentWatchError(
                "CLAUDE_WATCH_EVENTS_FILE must be owned by the current user"
            )

    if events_path != default_path:
        if _path_is_within(events_path, paths.config):
            raise AgentWatchError(
                "custom CLAUDE_WATCH_EVENTS_FILE cannot reuse AgentWatch config/runtime files"
            )
        ancestor = events_path.parent
        while True:
            if os.path.lexists(ancestor):
                try:
                    ancestor_metadata = ancestor.lstat()
                except OSError as exc:
                    raise AgentWatchError(
                        "CLAUDE_WATCH_EVENTS_FILE ancestor cannot be inspected"
                    ) from exc
                if not stat.S_ISDIR(ancestor_metadata.st_mode):
                    raise AgentWatchError(
                        "CLAUDE_WATCH_EVENTS_FILE ancestor must be a directory"
                    )
                break
            parent_ancestor = ancestor.parent
            if parent_ancestor == ancestor:
                break
            ancestor = parent_ancestor
        parent = events_path.parent
        if not os.path.lexists(parent):
            raise AgentWatchError(
                "custom CLAUDE_WATCH_EVENTS_FILE parent must already exist"
            )
        try:
            parent_metadata = parent.lstat()
        except OSError as exc:
            raise AgentWatchError(
                "CLAUDE_WATCH_EVENTS_FILE parent cannot be inspected"
            ) from exc
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise AgentWatchError(
                "CLAUDE_WATCH_EVENTS_FILE parent must be a directory"
            )
        if os.name != "nt" and stat.S_IMODE(parent_metadata.st_mode) != 0o700:
            raise AgentWatchError(
                "custom CLAUDE_WATCH_EVENTS_FILE parent must be private and "
                "owner-readable, owner-writable, owner-executable (0700)"
            )
        if hasattr(os, "getuid") and parent_metadata.st_uid != os.getuid():
            raise AgentWatchError(
                "custom CLAUDE_WATCH_EVENTS_FILE parent must be owned by the current user"
            )
        owned_path = _load_claude_spool_ownership(paths)
        if os.path.lexists(events_path) and owned_path != events_path:
            raise AgentWatchError(
                "refusing existing custom CLAUDE_WATCH_EVENTS_FILE without AgentWatch ownership"
            )

    lock_path = events_path.with_name(events_path.name + ".append.lock")
    if path_has_link_component(lock_path):
        raise AgentWatchError("Claude spool lock path must not contain a symlink or junction")
    if os.path.lexists(lock_path):
        try:
            lock_metadata = lock_path.lstat()
        except OSError as exc:
            raise AgentWatchError("Claude spool lock file cannot be inspected") from exc
        if not stat.S_ISREG(lock_metadata.st_mode) or stat.S_ISLNK(lock_metadata.st_mode):
            raise AgentWatchError("Claude spool lock must be a regular file")
        if os.name != "nt" and stat.S_IMODE(lock_metadata.st_mode) != 0o600:
            raise AgentWatchError(
                "Claude spool lock must be private (0600), owner-readable and "
                "owner-writable"
            )
        if hasattr(os, "getuid") and lock_metadata.st_uid != os.getuid():
            raise AgentWatchError(
                "Claude spool lock must be owned by the current user"
            )
    return events_path


def _installed_claude_handler(paths: InstallPaths) -> dict[str, Any]:
    return build_claude_hook_handler(
        sys.executable,
        paths.runtime / "agentwatch.py",
        _installed_claude_events_path(paths),
    )


def _claude_hook_registration_path(paths: InstallPaths) -> Path:
    return paths.config / CLAUDE_HOOK_REGISTRATION_FILE_NAME


def _load_claude_hook_registration(paths: InstallPaths) -> Path | None:
    registration_path = _claude_hook_registration_path(paths)
    if not os.path.lexists(registration_path):
        return None
    reject_symlink_path(registration_path, paths.config.parent)
    if registration_path.is_symlink() or not registration_path.is_file():
        raise AgentWatchError("Claude hook registration must be a regular file")
    try:
        payload = json.loads(registration_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentWatchError("Claude hook registration is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise AgentWatchError("Claude hook registration has an unsupported format")
    raw_settings_path = payload.get("settings_path")
    if not isinstance(raw_settings_path, str) or not raw_settings_path.strip():
        raise AgentWatchError("Claude hook registration is missing settings_path")
    settings_path = Path(raw_settings_path)
    if not settings_path.is_absolute() or settings_path.name != "settings.json":
        raise AgentWatchError("Claude hook registration settings_path must be an absolute settings.json path")
    return Path(os.path.abspath(settings_path))


def _save_claude_hook_registration(paths: InstallPaths, settings_path: Path) -> None:
    registration_path = _claude_hook_registration_path(paths)
    reject_symlink_path(registration_path, paths.config.parent)
    payload = {
        "version": 1,
        "settings_path": str(Path(os.path.abspath(settings_path))),
    }
    atomic_write(
        registration_path,
        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
        mode=0o600,
    )


def _remove_claude_hook_registration(paths: InstallPaths) -> None:
    registration_path = _claude_hook_registration_path(paths)
    if not os.path.lexists(registration_path):
        return
    reject_symlink_path(registration_path, paths.config.parent)
    if registration_path.is_symlink() or not registration_path.is_file():
        raise AgentWatchError("Claude hook registration must be a regular file")
    registration_path.unlink()


def _preflight_installed_claude_hooks(
    paths: InstallPaths,
    *,
    enabled: bool | None = None,
) -> bool:
    if enabled is None:
        enabled = _claude_watch_enabled(paths)
    if enabled:
        _validate_installed_claude_events_path(paths)
    current_settings = claude_settings_path(paths.home)
    registered_settings = _load_claude_hook_registration(paths)
    desired_handler = _installed_claude_handler(paths)
    backup_path = paths.config / "backups" / CLAUDE_SETTINGS_BACKUP_FILE
    changed = False
    if registered_settings is not None and registered_settings != current_settings:
        changed = preflight_claude_hooks(
            registered_settings,
            desired_handler,
            backup_path,
            enabled=False,
        ) or changed
    changed = preflight_claude_hooks(
        current_settings,
        desired_handler,
        backup_path,
        enabled=enabled,
    ) or changed
    return changed


def _configure_installed_claude_hooks(paths: InstallPaths, *, enabled: bool | None = None) -> bool:
    if enabled is None:
        enabled = _claude_watch_enabled(paths)
    _preflight_installed_claude_hooks(paths, enabled=enabled)
    current_settings = claude_settings_path(paths.home)
    registered_settings = _load_claude_hook_registration(paths)
    desired_handler = _installed_claude_handler(paths)
    backup_path = paths.config / "backups" / CLAUDE_SETTINGS_BACKUP_FILE
    changed = False
    if registered_settings is not None and registered_settings != current_settings:
        changed = configure_claude_hooks(
            registered_settings,
            desired_handler,
            backup_path,
            enabled=False,
        ) or changed
    if enabled:
        _save_claude_spool_ownership(paths, _validate_installed_claude_events_path(paths))
        # Persist the exact Claude settings scope before adding the Hook. If a
        # later write is interrupted, a future update can still find and repair
        # this target without leaving duplicate handlers in another scope.
        _save_claude_hook_registration(paths, current_settings)
        changed = configure_claude_hooks(
            current_settings,
            desired_handler,
            backup_path,
            enabled=True,
        ) or changed
    else:
        changed = configure_claude_hooks(
            current_settings,
            desired_handler,
            backup_path,
            enabled=False,
        ) or changed
        _remove_claude_hook_registration(paths)
    return changed


def _registered_claude_settings_for_status(paths: InstallPaths) -> tuple[Path, str | None]:
    current_settings = claude_settings_path(paths.home)
    try:
        return _load_claude_hook_registration(paths) or current_settings, None
    except AgentWatchError as exc:
        return current_settings, str(exc)


def _claude_cli_status() -> dict[str, Any]:
    executable = shutil.which("claude")
    minimum = ".".join(str(part) for part in MIN_CLAUDE_HOOK_VERSION)
    result: dict[str, Any] = {
        "cli_detected": executable is not None,
        "cli_path": executable,
        "cli_version": None,
        "minimum_cli_version": minimum,
        "cli_compatible": False,
    }
    if executable is None:
        return result
    try:
        completed = _run([executable, "--version"], timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        result["cli_version_error"] = str(exc)
        return result
    rendered = f"{completed.stdout}\n{completed.stderr}".strip()
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", rendered)
    if completed.returncode != 0 or match is None:
        result["cli_version_error"] = "could not parse claude --version"
        return result
    version = tuple(int(part) for part in match.groups())
    result["cli_version"] = ".".join(str(part) for part in version)
    result["cli_compatible"] = version >= MIN_CLAUDE_HOOK_VERSION
    return result


def _installed_claude_hook_status(paths: InstallPaths) -> dict[str, Any]:
    current_settings = claude_settings_path(paths.home)
    settings_path, registration_error = _registered_claude_settings_for_status(paths)
    events_path_error: str | None = None
    try:
        events_path = _installed_claude_events_path(paths)
    except AgentWatchError as exc:
        events_path = paths.config / CLAUDE_HOOK_EVENTS_FILE_NAME
        events_path_error = str(exc)
    status = inspect_claude_hooks(
        settings_path,
        build_claude_hook_handler(
            sys.executable,
            paths.runtime / "agentwatch.py",
            events_path,
        ),
        managed_settings_path=claude_managed_settings_path(platform.system()),
    )
    status["enabled"] = _claude_watch_enabled(paths)
    if status["enabled"] and events_path_error is None:
        try:
            _validate_installed_claude_events_path(paths)
        except AgentWatchError as exc:
            events_path_error = str(exc)
    status["events_file"] = str(events_path)
    status["events_path_safe"] = events_path_error is None
    if events_path_error is not None:
        status["events_path_error"] = events_path_error
    status["current_settings_path"] = str(current_settings)
    status["registered_settings_path"] = str(settings_path)
    status["needs_reconcile"] = settings_path != current_settings
    status.update(_claude_cli_status())
    status["policy_active"] = bool(status.get("active"))
    status["active"] = bool(status["policy_active"] and status["cli_compatible"])
    if status["needs_reconcile"]:
        # Claude currently reads a different user-settings scope than the one
        # where AgentWatch was registered. `agentwatch update` migrates it.
        status["active"] = False
    if registration_error is not None:
        status["registration_error"] = registration_error
        status["active"] = False
    if events_path_error is not None:
        status["active"] = False
    return status


def _delivery_snapshot(
    paths: InstallPaths,
    *,
    strict_token: bool = False,
    mutating: bool = False,
) -> tuple[dict[str, str], ComputerTokenStore, str | None, dict[str, Any]]:
    machine = (
        load_or_create_machine(paths.config) if mutating else load_machine(paths.config)
    )
    has_machine = machine is not None
    if machine is None:
        machine = {
            "computer_id": "",
            "computer_name": "",
            "platform": platform.system().lower(),
        }
    store = ComputerTokenStore(machine["computer_id"], paths.config)
    values = _config_values(paths)
    bark_configured = bool(values.get("BARK_URL", "").strip() or values.get("BARK_KEY", "").strip())
    mode = load_delivery_mode(paths.config)
    token: str | None = None
    # Explicit Bark-only mode must not depend on Keychain/Secret Service or an
    # AgentWatch account. Missing legacy settings still inspect a token once so
    # an existing dual-receiver installation can be migrated accurately.
    if strict_token and has_machine:
        token = store.load_strict()
    elif mode != "bark" and has_machine:
        # status/doctor are read-only but still strict: backend outages must be
        # reported instead of being flattened into "not authenticated".  A
        # legacy backend is only persisted during an explicitly mutating flow.
        token = store.load_strict() if mutating else store.load_read_only()
    if mode is None:
        mode = infer_delivery_mode(bark_configured, token is not None)
        if mode is not None and mutating:
            save_delivery_mode(mode, paths.config)
    delivery = resolve_delivery(mode, bark_configured, token is not None)
    return machine, store, token, delivery


def _status(paths: InstallPaths, service: ServiceManager) -> dict[str, Any]:
    machine, store, _token, delivery = _delivery_snapshot(paths)
    values = _config_values(paths)
    legacy_names = {
        "NTFY_URL",
        "NTFY_TOKEN",
        "CODEX_NTFY_URL",
        "ZCODE_NTFY_URL",
        "KIMI_NTFY_URL",
        "GROK_NTFY_URL",
        "CLAUDE_NTFY_URL",
    }
    legacy_keys = sorted(key for key in legacy_names if values.get(key, "").strip())
    result = {
        "version": VERSION,
        "installed": service.installed(),
        # Compatibility alias retained for v0.2.0 scripts.
        "authenticated": delivery["agentwatch_authenticated"],
        "username": machine.get("username"),
        "computer_id": machine["computer_id"],
        "computer_name": machine["computer_name"],
        "platform": machine["platform"],
        "service": service.state(),
        "credential_backend": store.backend_name(),
        "api_base": _configured_api_base(paths, values),
        "legacy_ntfy_ignored": bool(legacy_keys),
        "legacy_ntfy_keys": legacy_keys,
        "launcher": str(paths.launcher),
        "claude_hook": _installed_claude_hook_status(paths),
    }
    result.update(delivery)
    result["login_required"] = bool(
        delivery["delivery_mode"] in {"agentwatch", "both"}
        and not delivery["agentwatch_authenticated"]
    )
    result["agentwatch_login_required"] = result["login_required"]
    result["bark_configuration_required"] = bool(
        delivery["delivery_mode"] in {"bark", "both"} and not delivery["bark_configured"]
    )
    return result


def _login(username: str | None, paths: InstallPaths, service: ServiceManager) -> dict[str, Any]:
    machine = load_or_create_machine(paths.config)
    previous_mode = load_delivery_mode(paths.config)
    values = _config_values(paths)
    bark_configured = bool(values.get("BARK_URL", "").strip() or values.get("BARK_KEY", "").strip())
    if not sys.stdin.isatty():
        raise AgentWatchError("login requires an interactive terminal; passwords cannot be piped or automated")
    if username:
        entered_username = username.strip()
    else:
        print("AgentWatch 账号: ", end="", file=sys.stderr, flush=True)
        entered_username = sys.stdin.readline().strip()
    if not entered_username:
        raise AgentWatchError("account name cannot be empty")
    password = getpass.getpass("AgentWatch 密码（输入时不会显示）: ")
    if not password:
        raise AgentWatchError("password cannot be empty")
    api = AgentWatchApi(_configured_api_base(paths, values))
    try:
        response = api.login(entered_username, password, machine)
    finally:
        password = ""  # Drop the only local reference immediately after HTTPS login.

    token = str(response.get("computer_token") or "")
    store = ComputerTokenStore(machine["computer_id"], paths.config)
    target_mode = "both" if previous_mode == "bark" else previous_mode
    if target_mode is None:
        target_mode = infer_delivery_mode(bark_configured, True) or "agentwatch"
    try:
        store.save(token)
        save_machine_account(machine, str(response.get("username") or entered_username), paths.config)
        save_delivery_mode(target_mode, paths.config)
        service.start()
    except BaseException:
        revoked = False
        try:
            api.logout(token)
            revoked = True
        except ApiError as exc:
            revoked = exc.status == 401
        except AgentWatchError:
            revoked = False
        if revoked:
            try:
                store.delete()
            except AgentWatchError:
                pass
        if previous_mode is not None and previous_mode != target_mode:
            try:
                save_delivery_mode(previous_mode, paths.config)
            except (AgentWatchError, OSError):
                pass
        # A failed Android login/setup must not deliberately take a working
        # Bark-only service offline.
        try:
            if previous_mode == "bark" and bark_configured:
                service.start()
            else:
                service.stop()
        except (AgentWatchError, OSError, subprocess.SubprocessError):
            pass
        raise
    bark_configuration_required = bool(
        target_mode in {"bark", "both"} and not bark_configured
    )
    message = "private notification channel is ready"
    if bark_configuration_required:
        message += f"；{BARK_UPDATE_INSTRUCTION}"
    result = {
        "ok": True,
        "authenticated": True,
        "username": str(response.get("username") or entered_username),
        "computer_id": machine["computer_id"],
        "computer_name": machine["computer_name"],
        "platform": machine["platform"],
        "service": service.state(),
        "message": message,
    }
    result.update(resolve_delivery(target_mode, bark_configured, True))
    result["login_required"] = False
    result["agentwatch_login_required"] = False
    result["bark_configuration_required"] = bark_configuration_required
    return result


def _human_status(result: dict[str, Any]) -> None:
    print(f"AgentWatch {result['version']}")
    print(f"安装：{'已安装' if result['installed'] else '未安装'}")
    print(f"接收模式：{result.get('delivery_mode') or '需要选择'}")
    print(f"Bark：{'已配置' if result.get('bark_configured') else '未配置'}")
    print(f"Android 账号：{'已登录' if result.get('agentwatch_authenticated') else '未登录'}")
    if result.get("username"):
        print(f"账号：{result['username']}")
    print(f"电脑：{result['computer_name']} ({result['platform']})")
    print(f"后台服务：{result['service']}")
    claude_hook = result.get("claude_hook") or {}
    if not claude_hook.get("enabled"):
        claude_state = "已关闭"
    elif claude_hook.get("active"):
        claude_state = "已配置（未检测到文件策略阻止）"
    elif claude_hook.get("registration_error"):
        claude_state = "配置记录异常"
    elif claude_hook.get("needs_reconcile"):
        claude_state = "Claude 配置目录已变化，请运行 agentwatch update"
    elif claude_hook.get("configured") and claude_hook.get("policy_active") and not claude_hook.get("cli_detected"):
        claude_state = "已预配置（未检测到 Claude CLI）"
    elif claude_hook.get("configured") and claude_hook.get("policy_active") and not claude_hook.get("cli_compatible"):
        detected_version = claude_hook.get("cli_version") or "无法识别"
        claude_state = (
            f"Claude Code 版本不兼容（当前 {detected_version}，"
            f"需要 >= {claude_hook.get('minimum_cli_version')}）"
        )
    elif claude_hook.get("configured"):
        claude_state = "已配置但被 Claude 设置或策略禁用"
    else:
        claude_state = "未配置"
    print(f"Claude Code Hook：{claude_state}")
    effective = ", ".join(result.get("effective_channels") or []) or "无"
    print(f"当前可用通道：{effective}")
    if result.get("degraded"):
        missing = ", ".join(result.get("missing_channels") or [])
        print(f"状态：可用但未完全配置（缺少 {missing}）")
    print(f"凭据存储：{result['credential_backend']}")
    if result.get("legacy_ntfy_ignored"):
        print("旧版 NTFY_URL/NTFY_TOKEN：已检测到，但私有发布会忽略它们，不会双发")


def _emit(result: dict[str, Any], json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        message = result.get("message")
        if message:
            print(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentwatch", description="AgentWatch computer installer and service CLI")
    parser.add_argument("--json", action="store_true", help="Print one machine-readable JSON object.")
    parser.add_argument("--password", action=RejectPasswordAction, nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"AgentWatch {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        child = subparsers.add_parser(name, help=help_text)
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return child

    install = command("install", "Install or repair the background watcher.")
    install.add_argument("--no-login", action="store_true", help="Install only; do not prompt for an account.")
    install.add_argument(
        "--delivery",
        choices=("bark", "agentwatch", "both"),
        help="Select iPhone Bark, Android AgentWatch, or both receivers.",
    )
    install.add_argument("--username", help="Pre-fill the non-secret account name for interactive login.")
    install.add_argument("--password", action=RejectPasswordAction, nargs="?", help=argparse.SUPPRESS)
    login = command("login", "Log this computer into an AgentWatch account.")
    login.add_argument("--username", help="Pre-fill the non-secret account name.")
    login.add_argument("--password", action=RejectPasswordAction, nargs="?", help=argparse.SUPPRESS)
    command("status", "Show local authentication and background service state.")
    command("doctor", "Run local and server diagnostics without sending a notification.")
    command("update", "Install this package over the existing runtime without changing credentials.")
    command(
        "logout",
        "Revoke this computer's AgentWatch token; keep other configured channels running.",
    )
    claude_hook = command("claude-hook", argparse.SUPPRESS)
    claude_hook.add_argument("--events-file", help=argparse.SUPPRESS)
    claude_hook.add_argument("--managed-hook-id", help=argparse.SUPPRESS)
    command("uninstall", "Remove the background service and installed runtime; keep account data.")
    return parser


def _prompt_delivery_mode() -> str:
    print("请选择手机接收方式：", file=sys.stderr)
    print("  1) bark       iPhone 使用 Bark（无需 AgentWatch 登录）", file=sys.stderr)
    print("  2) agentwatch Android 使用 AgentWatch 账号", file=sys.stderr)
    print("  3) both       两端同时使用", file=sys.stderr)
    print("输入 1/2/3 或 bark/agentwatch/both: ", end="", file=sys.stderr, flush=True)
    entered = sys.stdin.readline().strip().lower()
    selected = {"1": "bark", "2": "agentwatch", "3": "both"}.get(entered, entered)
    if selected not in {"bark", "agentwatch", "both"}:
        raise DeliveryModeRequired("请选择 bark、agentwatch 或 both；无界面安装请使用 --delivery")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "claude-hook":
        # Stop hooks must never delay or block Claude because local notification
        # persistence failed. Invalid input is ignored without stdout/stderr.
        try:
            explicit_path = None
            if args.events_file:
                expanded = Path(os.path.expandvars(os.path.expanduser(args.events_file)))
                explicit_path = Path(os.path.abspath(expanded))
            ingest_claude_hook_event(events_path=explicit_path)
        except Exception:  # noqa: BLE001 - notification persistence must never block Claude.
            pass
        return 0
    paths = InstallPaths()
    service = ServiceManager(paths)
    json_output = bool(args.json)
    try:
        if args.command == "install":
            selected_delivery = args.delivery or load_delivery_mode(paths.config)
            if selected_delivery is None:
                if json_output or not sys.stdin.isatty():
                    raise DeliveryModeRequired(
                        "无法从现有配置判断接收方式；请使用 --delivery bark|agentwatch|both"
                    )
                selected_delivery = _prompt_delivery_mode()
            _preflight_installed_claude_hooks(paths)
            install_runtime(paths)
            load_or_create_machine(paths.config)
            save_delivery_mode(selected_delivery, paths.config)
            _configure_installed_claude_hooks(paths)
            result = _status(paths, service)
            service.install(should_start=result["operational"])
            result = _status(paths, service)
            if result["login_required"] and not args.no_login and not json_output:
                if result["operational"]:
                    print("Bark 已可用；继续登录 AgentWatch 以补全 Android 通道。")
                else:
                    print("AgentWatch 已安装。请登录以建立 Android 专属通知通道；密码输入不会显示。")
                result = _login(args.username, paths, service)
                result["installed"] = True
                result["launcher"] = str(paths.launcher)
                result["message"] += f"；后续命令入口：{paths.launcher}"
                _emit(result, False)
            else:
                result["ok"] = True
                pending_steps: list[str] = []
                if result["login_required"]:
                    pending_steps.append("Android 通道需亲自在终端运行 agentwatch login")
                if result["bark_configuration_required"]:
                    pending_steps.append(BARK_UPDATE_INSTRUCTION)
                if pending_steps:
                    result["message"] = "AgentWatch 已安装；" + "；".join(pending_steps)
                else:
                    result["message"] = "AgentWatch 已完成幂等安装，所选接收通道保持不变"
                _emit(result, json_output)
            return 0

        if args.command == "login":
            if not service.installed():
                raise AgentWatchError("AgentWatch is not installed; run agentwatch install first")
            result = _login(args.username, paths, service)
            result["launcher"] = str(paths.launcher)
            _emit(result, json_output)
            return 0

        if args.command == "status":
            result = _status(paths, service)
            if json_output:
                _emit(result, True)
            else:
                _human_status(result)
            service_running = result["service"] in RUNNING_SERVICE_STATES
            return 0 if result["installed"] and result["operational"] and service_running else 1

        if args.command == "doctor":
            result = _status(paths, service)
            claude_hook = result["claude_hook"]
            checks = {
                "runtime_files": all((paths.runtime / filename).exists() for filename in RUNTIME_FILES[:-1]),
                "delivery_mode_selected": result["delivery_mode"] is not None,
                "service_installed": result["installed"],
                "service_running": result["service"] in RUNNING_SERVICE_STATES,
                "legacy_ntfy_ignored": result["legacy_ntfy_ignored"],
                "claude_hook_configured": bool(
                    not claude_hook["enabled"] or claude_hook["configured"]
                ),
                "claude_hook_registration_valid": "registration_error" not in claude_hook,
                "claude_hook_scope_current": not claude_hook.get("needs_reconcile"),
                "claude_hook_policy_active": bool(
                    not claude_hook["enabled"] or claude_hook.get("policy_active")
                ),
                "claude_hook_active": bool(
                    not claude_hook["enabled"]
                    or not claude_hook.get("cli_detected")
                    or claude_hook["active"]
                ),
                "claude_events_path_safe": bool(
                    not claude_hook["enabled"]
                    or claude_hook.get("events_path_safe")
                ),
                "claude_cli_detected": bool(
                    not claude_hook["enabled"] or claude_hook.get("cli_detected")
                ),
                "claude_cli_compatible": bool(
                    not claude_hook["enabled"]
                    or not claude_hook.get("cli_detected")
                    or claude_hook.get("cli_compatible")
                ),
            }
            mode = result["delivery_mode"]
            if mode in {"bark", "both"}:
                checks["bark_configured"] = result["bark_configured"]
            agentwatch_healthy = False
            if mode in {"agentwatch", "both"}:
                checks["agentwatch_authenticated"] = result["agentwatch_authenticated"]
                if result["agentwatch_authenticated"]:
                    try:
                        AgentWatchApi(_configured_api_base(paths)).health()
                        agentwatch_healthy = True
                    except AgentWatchError:
                        agentwatch_healthy = False
                checks["server_reachable"] = agentwatch_healthy

            live = resolve_delivery(mode, result["bark_configured"], agentwatch_healthy)
            result.update(live)
            # Preserve the compatibility authentication field: server health
            # does not erase a locally stored credential.
            result["authenticated"] = result["agentwatch_authenticated"] = bool(
                result.get("authenticated")
            )
            result["checks"] = checks
            result["ok"] = bool(
                checks["runtime_files"]
                and checks["delivery_mode_selected"]
                and checks["service_installed"]
                and checks["service_running"]
                and checks["claude_hook_configured"]
                and checks["claude_hook_registration_valid"]
                and checks["claude_hook_scope_current"]
                and checks["claude_hook_policy_active"]
                and checks["claude_hook_active"]
                and checks["claude_events_path_safe"]
                and checks["claude_cli_compatible"]
                and result["operational"]
            )
            if json_output:
                _emit(result, True)
            else:
                _human_status(result)
                for name, passed in checks.items():
                    if name == "legacy_ntfy_ignored":
                        continue
                    print(f"[{'OK' if passed else 'WARN'}] {name}")
            return 0 if result["ok"] else 1

        if args.command == "update":
            _preflight_installed_claude_hooks(paths)
            install_runtime(paths)
            load_or_create_machine(paths.config)
            _delivery_snapshot(paths, mutating=True)
            _configure_installed_claude_hooks(paths)
            before = _status(paths, service)
            service.install(should_start=before["operational"])
            result = _status(paths, service)
            result.update({"ok": True, "message": "AgentWatch 已更新；账号凭据保持不变，未发送测试通知"})
            _emit(result, json_output)
            return 0

        if args.command == "logout":
            # Snapshot first so a legacy Bark+token install is migrated to
            # `both` before the token disappears.
            # This destructive flow must distinguish "no token" from an
            # unavailable Keychain/Secret Service/DPAPI backend.  A permissive
            # load could otherwise skip server-side revocation and erase the
            # only retriable local credential.
            _machine, store, token, _delivery = _delivery_snapshot(
                paths, strict_token=True, mutating=True
            )
            server_revoke_required = token is not None
            server_revoked = False
            if token:
                try:
                    AgentWatchApi(_configured_api_base(paths)).logout(token)
                    server_revoked = True
                except ApiError as exc:
                    if exc.status == 401:
                        server_revoked = True
                    else:
                        raise
            if server_revoke_required and not server_revoked:
                raise AgentWatchError("server did not revoke this computer token")
            try:
                store.delete()
            except (AgentWatchError, OSError, subprocess.SubprocessError) as exc:
                result = {
                    "ok": False,
                    "error": "local_token_cleanup_failed",
                    "partial": True,
                    "server_revoked": server_revoked,
                    "server_revoke_required": server_revoke_required,
                    "local_token_deleted": False,
                    "runtime_preserved": True,
                    "message": (
                        (
                            "电脑 token 已在服务器撤销，但本机凭据存储无法安全清除；"
                            if server_revoke_required
                            else "本机未发现电脑 token，但凭据存储无法安全清理；"
                        )
                        + "后台服务和程序保持原状，请修复本机凭据存储后重试 logout。"
                    ),
                    "detail": str(exc),
                }
                _emit(result, json_output)
                return 1
            mode_after = load_delivery_mode(paths.config)
            values_after = _config_values(paths)
            bark_after = bool(
                values_after.get("BARK_URL", "").strip()
                or values_after.get("BARK_KEY", "").strip()
            )
            delivery_after = resolve_delivery(mode_after, bark_after, False)
            if delivery_after["operational"]:
                service.start()
            else:
                service.stop()
            result = dict(delivery_after)
            result.update({
                "ok": True,
                "authenticated": False,
                "agentwatch_authenticated": False,
                "server_revoked": server_revoked,
                "server_revoke_required": server_revoke_required,
                "local_token_deleted": True,
                "message": (
                    "电脑 token 已在服务器撤销并从本机删除；其他已配置接收通道保持运行"
                    if server_revoke_required
                    else "本机没有电脑 token；其他已配置接收通道保持运行"
                ),
            })
            _emit(result, json_output)
            return 0

        if args.command == "uninstall":
            reject_symlink_path(paths.launcher, paths.home)
            reject_symlink_path(paths.runtime, paths.config.parent)
            hook_cleanup_error: str | None = None
            try:
                _configure_installed_claude_hooks(paths, enabled=False)
            except (AgentWatchError, OSError) as exc:
                hook_cleanup_error = str(exc)
            service_cleanup_error: str | None = None
            try:
                service.uninstall()
            except (AgentWatchError, OSError, subprocess.SubprocessError) as exc:
                service_cleanup_error = str(exc)
            if service_cleanup_error is not None:
                cleanup_errors = {"service": service_cleanup_error}
                if hook_cleanup_error is not None:
                    cleanup_errors["claude_hook"] = hook_cleanup_error
                result = {
                    "ok": False,
                    "error": "service_cleanup_failed",
                    "partial": True,
                    "service_removed": False,
                    "runtime_preserved": True,
                    "credentials_preserved": True,
                    "claude_hook_cleanup_failed": hook_cleanup_error is not None,
                    "message": (
                        "AgentWatch 后台服务未能确认移除；为避免仍在运行的服务或残留 Hook "
                        "指向不存在的程序，运行时和命令入口已保留。请修复后台服务状态后"
                        "重新运行 agentwatch uninstall。"
                    ),
                    "detail": service_cleanup_error,
                    "cleanup_errors": cleanup_errors,
                }
                _emit(result, json_output)
                return 1
            if hook_cleanup_error is not None:
                result = {
                    "ok": False,
                    "error": "claude_hook_cleanup_failed",
                    "partial": True,
                    "service_removed": True,
                    "runtime_preserved": True,
                    "credentials_preserved": True,
                    "message": (
                        "AgentWatch 后台服务已移除，但 Claude settings 无法安全修改；"
                        "为避免残留 Hook 指向不存在的程序，运行时和命令入口已保留。"
                        "请修复 Claude settings 后重新运行 agentwatch uninstall。"
                    ),
                    "detail": hook_cleanup_error,
                }
                _emit(result, json_output)
                return 1
            try:
                paths.launcher.unlink()
            except FileNotFoundError:
                pass
            for filename in RUNTIME_FILES:
                try:
                    (paths.runtime / filename).unlink()
                except FileNotFoundError:
                    pass
            try:
                (paths.runtime / "run_notifier.ps1").unlink()
            except FileNotFoundError:
                pass
            try:
                paths.runtime.rmdir()
            except OSError:
                pass
            result = {
                "ok": True,
                "message": "AgentWatch 后台服务、Claude Hook 和程序已卸载；本机账号 token 与历史状态已保留",
                "credentials_preserved": True,
            }
            _emit(result, json_output)
            return 0
    except DeliveryModeRequired as exc:
        error = {"ok": False, "error": "delivery_mode_required", "message": str(exc)}
    except ApiError as exc:
        error = {"ok": False, "error": exc.code, "message": exc.message, "status": exc.status}
    except (AgentWatchError, OSError, subprocess.SubprocessError) as exc:
        error = {"ok": False, "error": "local_error", "message": str(exc)}
    if json_output:
        _emit(error, True)
    else:
        print(f"AgentWatch：{error['message']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        raise SystemExit(130)
