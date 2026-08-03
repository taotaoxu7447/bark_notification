#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.parse
import urllib.request

from agentwatch_core import (
    AgentWatchApi,
    AgentWatchError,
    ApiError,
    ComputerTokenStore,
    infer_delivery_mode,
    load_delivery_mode,
    load_or_create_machine,
    path_has_link_component,
    resolve_delivery,
    stable_event_id,
)


DEFAULT_STATE = "~/.codex-watch-notifier/state.json"
DEFAULT_LOG = "~/.codex-watch-notifier/notifier.log"
DEFAULT_SESSIONS_ROOT = "~/.codex/sessions"
DEFAULT_ARCHIVED_ROOT = "~/.codex/archived_sessions"
DEFAULT_SESSION_INDEX = "~/.codex/session_index.jsonl"
DEFAULT_ZCODE_LOG_ROOT = "~/.zcode/cli/log"
DEFAULT_KIMI_SESSIONS_ROOT = "~/.kimi-code/sessions"
DEFAULT_GROK_SESSIONS_ROOT = "~/.grok/sessions"
CLAUDE_HOOK_EVENTS_FILE_NAME = "claude-hook-events.jsonl"
TOOL_HOOK_EVENTS_DIR_NAME = "tool-hook-events"
DEFAULT_CLAUDE_SPOOL_MAX_BYTES = 4 * 1024 * 1024
MIN_CLAUDE_SPOOL_MAX_BYTES = 64 * 1024
DEFAULT_CLAUDE_SPOOL_MAX_AGE_SECONDS = 24 * 60 * 60
MIN_CLAUDE_SPOOL_MAX_AGE_SECONDS = 60 * 60
DEFAULT_CLAUDE_DRAIN_GRACE_SECONDS = 30
# A first-pass Stop (stop_hook_active=false) is provisional because all
# matching Claude hooks run in parallel and a sibling may still block the
# stop. Ten seconds favors timely completion alerts while retaining a short
# lookahead window. It does not cover Claude's 30-second default prompt-hook
# timeout, so installations with slow blockers should select a longer value,
# up to the full 600-second command/HTTP/MCP hook window.
DEFAULT_CLAUDE_STOP_SETTLE_SECONDS = 10
MIN_CLAUDE_STOP_SETTLE_SECONDS = 5
MAX_CLAUDE_STOP_SETTLE_SECONDS = 600
CLAUDE_DRAIN_MARKER = ".agentwatch-drain-"
DEFAULT_PUBLISHER_ID_FILE = "~/.codex-watch-notifier/publisher-id"
DEFAULT_CODEX_BARK_ICON = (
    "https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/codex-icon-large-v1.png"
)
DEFAULT_ZCODE_BARK_ICON = (
    "https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/zcode-icon-v1.png"
)
DEFAULT_KIMI_BARK_ICON = (
    "https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/kimi-icon-v1.png"
)
DEFAULT_GROK_BARK_ICON = (
    "https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/grok-icon-v1.png"
)
DEFAULT_CLAUDE_BARK_ICON = (
    "https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/"
    "android/app/src/main/res/drawable-nodpi/source_claude.png"
)
DEFAULT_PI_BARK_ICON = (
    "https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/pi-icon-v1.png"
)
DEFAULT_OPENCODE_BARK_ICON = (
    "https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/opencode-icon-v1.png"
)
CLAUDE_HOOK_SCHEMA = "agentwatch_claude_hook_v1"
TOOL_HOOK_SCHEMA = "agentwatch_tool_hook_v1"
TOOL_HOOK_MESSAGE_LIMIT_CHARS = 64 * 1024
TOOL_HOOK_EVENT_MAX_BYTES = 1024 * 1024
TOOL_HOOK_EVENT_FILE_RE = re.compile(
    r"^(?P<created>[0-9]{15,21})-(?P<source>pi|opencode)-"
    r"(?P<identity>[0-9a-f]{16})-(?P<nonce>[0-9a-f]{8})\.json$"
)
CLAUDE_HOOK_MESSAGE_LIMIT_CHARS = 64 * 1024
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
DEFAULT_MAX_EVENT_AGE_SECONDS = 3600
STATE_VERSION = 2
MAX_SENT_KEYS = 3000
DEFAULT_DELIVERY_MAX_ATTEMPTS = 2
HARD_MAX_DELIVERY_ATTEMPTS = 2
DEFAULT_DELIVERY_RETRY_DELAY_SECONDS = 60
MIN_DELIVERY_RETRY_DELAY_SECONDS = 30
MAX_DELIVERY_RETRY_DELAY_SECONDS = 86400
MAX_EXHAUSTED_DELIVERIES = 500
NTFY_PROTOCOL_VERSION = 1


class StateFileError(ValueError):
    """The watcher state is unreadable or structurally unsafe to continue from."""


class ConfigFileError(ValueError):
    """The persistent private environment cannot be read safely."""


PRIVATE_STATE_FILE_MODE = 0o600


def lexical_absolute_path(path: Path | str) -> Path:
    """Return an absolute path without erasing symlink/reparse-point evidence."""
    expanded = os.path.expandvars(os.path.expanduser(str(path)))
    return Path(os.path.abspath(expanded))


def prepare_private_file_parent(path: Path, description: str) -> Path:
    """Create and validate a parent without following linked descendants."""
    target = lexical_absolute_path(path)
    if path_has_link_component(target):
        raise StateFileError(f"{description} must not contain a symlink or junction: {target}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise StateFileError(f"cannot create {description} directory: {target.parent}") from exc
    if path_has_link_component(target):
        raise StateFileError(f"{description} must not contain a symlink or junction: {target}")
    try:
        parent_metadata = target.parent.lstat()
    except OSError as exc:
        raise StateFileError(f"cannot inspect {description} directory: {target.parent}") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
        raise StateFileError(f"{description} parent must be a real directory: {target.parent}")
    return target


def validate_private_regular_descriptor(descriptor: int, description: str) -> os.stat_result:
    """Validate and privatize an already-open state/lock file descriptor."""
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise StateFileError(f"cannot inspect {description}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise StateFileError(f"{description} must be a regular file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise StateFileError(f"{description} must be owned by the current user")
    try:
        os.fchmod(descriptor, PRIVATE_STATE_FILE_MODE)
    except (AttributeError, OSError):
        if os.name != "nt":
            raise StateFileError(f"cannot make {description} private") from None
    if os.name != "nt":
        try:
            private_metadata = os.fstat(descriptor)
        except OSError as exc:
            raise StateFileError(f"cannot verify {description} permissions") from exc
        if stat.S_IMODE(private_metadata.st_mode) != PRIVATE_STATE_FILE_MODE:
            raise StateFileError(f"{description} must have private 0600 permissions")
        metadata = private_metadata
    return metadata


def path_matches_open_file(path: Path, metadata: os.stat_result) -> bool:
    """Return whether a lexical path still names the opened non-link file."""
    if path_has_link_component(path):
        return False
    try:
        current = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(current, "st_file_attributes", 0) & reparse_flag:
        return False
    if hasattr(os.path, "isjunction") and os.path.isjunction(path):
        return False
    # Some Windows filesystems report zero inode/device values. The no-link and
    # regular-file checks remain authoritative there; compare identity whenever
    # the platform supplies meaningful values.
    expected_identity = (getattr(metadata, "st_dev", 0), getattr(metadata, "st_ino", 0))
    current_identity = (getattr(current, "st_dev", 0), getattr(current, "st_ino", 0))
    if all(expected_identity) and all(current_identity):
        return expected_identity == current_identity
    return True


def fsync_directory(path: Path, description: str) -> None:
    """Persist a directory entry update on platforms that support directory fsync."""
    if os.name == "nt":
        return
    if path_has_link_component(path):
        raise StateFileError(f"{description} directory must not contain a symlink or junction")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StateFileError(f"cannot open {description} directory for sync") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise StateFileError(f"{description} parent must be a directory")
        os.fsync(descriptor)
    except OSError as exc:
        raise StateFileError(f"cannot sync {description} directory") from exc
    finally:
        os.close(descriptor)


def validate_existing_private_file(path: Path, description: str) -> None:
    """Validate an existing state-owned file without following its final path."""
    if not os.path.lexists(path):
        return
    if path_has_link_component(path):
        raise StateFileError(f"{description} must not contain a symlink or junction: {path}")
    try:
        path_metadata = path.lstat()
    except OSError as exc:
        raise StateFileError(f"cannot inspect {description}: {path}") from exc
    if not stat.S_ISREG(path_metadata.st_mode) or stat.S_ISLNK(path_metadata.st_mode):
        raise StateFileError(f"{description} must be a regular file: {path}")
    if hasattr(os, "getuid") and path_metadata.st_uid != os.getuid():
        raise StateFileError(f"{description} must be owned by the current user: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOINHERIT"):
        flags |= os.O_NOINHERIT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StateFileError(f"cannot safely open {description}: {path}") from exc
    try:
        metadata = validate_private_regular_descriptor(descriptor, description)
        if not path_matches_open_file(path, metadata):
            raise StateFileError(f"{description} changed while it was being inspected: {path}")
    finally:
        os.close(descriptor)


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def absolute_path_without_symlink_resolution(value: str) -> Path:
    """Expand a configured path without turning its final symlink into a target path."""
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(os.path.abspath(expanded))


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except (OSError, UnicodeError) as exc:
        raise ConfigFileError(
            f"cannot read private watcher config {path}: {type(exc).__name__}"
        ) from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = os.path.expandvars(os.path.expanduser(value))


def default_env_path() -> Path:
    config_root = (
        os.getenv("CODEX_WATCH_CONFIG_DIR")
        or os.getenv("AGENTWATCH_CONFIG_DIR")
        or "~/.codex-watch-notifier"
    )
    return expand_path(os.getenv("CODEX_WATCH_ENV", config_root + "/env"))


def utc_to_local(value: Any) -> str:
    if value is None or value == "":
        return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    parsed = parse_timestamp(value)
    if parsed is None:
        return str(value)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def compact(text: str, limit: int = 900) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "..."


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw not in {"0", "false", "False", "no", "No", "off", "Off"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def claude_stop_settle_seconds() -> int:
    configured = env_int(
        "CLAUDE_WATCH_STOP_SETTLE_SECONDS",
        DEFAULT_CLAUDE_STOP_SETTLE_SECONDS,
    )
    return min(
        max(configured, MIN_CLAUDE_STOP_SETTLE_SECONDS),
        MAX_CLAUDE_STOP_SETTLE_SECONDS,
    )


def include_workspace_in_notifications() -> bool:
    return env_flag("NOTIFY_INCLUDE_WORKSPACE", True)


def include_message_excerpt_in_notifications() -> bool:
    return env_flag("NOTIFY_INCLUDE_MESSAGE", True)


def notify_subagents_enabled() -> bool:
    return env_flag("CODEX_WATCH_NOTIFY_SUBAGENTS", False)


def notification_body_max_chars() -> int:
    return max(env_int("NOTIFY_BODY_MAX_CHARS", 1100), 0)


def delivery_max_attempts() -> int:
    configured = env_int("NOTIFY_DELIVERY_MAX_ATTEMPTS", DEFAULT_DELIVERY_MAX_ATTEMPTS)
    return min(max(configured, 1), HARD_MAX_DELIVERY_ATTEMPTS)


def delivery_retry_delay_seconds() -> int:
    configured = env_int("NOTIFY_DELIVERY_RETRY_DELAY_SECONDS", DEFAULT_DELIVERY_RETRY_DELAY_SECONDS)
    return min(max(configured, MIN_DELIVERY_RETRY_DELAY_SECONDS), MAX_DELIVERY_RETRY_DELAY_SECONDS)


def _safe_protocol_identifier(value: str, limit: int) -> str:
    normalized = "".join(character.lower() for character in value if character.isalnum() or character in {"_", "-"})
    return normalized[:limit]


def publisher_instance_id() -> str:
    """Return a stable, non-secret ID so different publisher machines cannot collide."""
    configured = _safe_protocol_identifier(os.getenv("AGENT_WATCH_PUBLISHER_ID", "").strip(), 24)
    if configured:
        return configured

    path = expand_path(os.getenv("AGENT_WATCH_PUBLISHER_ID_FILE", DEFAULT_PUBLISHER_ID_FILE))
    try:
        existing = _safe_protocol_identifier(path.read_text(encoding="utf-8").strip(), 24)
        if existing:
            return existing
    except OSError:
        pass

    generated = secrets.token_hex(6)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(generated + "\n")
        return generated
    except FileExistsError:
        try:
            existing = _safe_protocol_identifier(path.read_text(encoding="utf-8").strip(), 24)
            if existing:
                return existing
        except OSError:
            pass
    except OSError:
        pass

    # A read-only home directory should not stop notifications. This fallback is
    # stable on one account but reveals neither the hostname nor the home path.
    fallback_source = f"{platform.node()}:{Path.home()}"
    return hashlib.sha256(fallback_source.encode("utf-8")).hexdigest()[:12]


def ntfy_source(event: dict[str, Any]) -> str:
    prefix = str(event.get("event_type") or "").partition("_")[0].lower()
    return prefix if prefix in {"codex", "zcode", "kimi", "grok", "claude", "pi", "opencode"} else "codex"


def ntfy_sequence_id(event: dict[str, Any]) -> str:
    stable_id = _safe_protocol_identifier(str(event.get("stable_id") or ""), 40)
    if not stable_id:
        return ""
    return f"aw{NTFY_PROTOCOL_VERSION}_{publisher_instance_id()}_{stable_id}"


def ntfy_tags_with_protocol(raw_tags: str, event: dict[str, Any]) -> str:
    tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
    protocol_tags = [f"agentwatch_v{NTFY_PROTOCOL_VERSION}", f"source_{ntfy_source(event)}"]
    for tag in protocol_tags:
        if tag not in tags:
            tags.append(tag)
    return ",".join(tags)


def ntfy_icon(event: dict[str, Any]) -> str:
    source = ntfy_source(event)
    defaults = {
        "codex": DEFAULT_CODEX_BARK_ICON,
        "zcode": DEFAULT_ZCODE_BARK_ICON,
        "kimi": DEFAULT_KIMI_BARK_ICON,
        "grok": DEFAULT_GROK_BARK_ICON,
        "claude": DEFAULT_CLAUDE_BARK_ICON,
        "pi": DEFAULT_PI_BARK_ICON,
        "opencode": DEFAULT_OPENCODE_BARK_ICON,
    }
    configured = str(event.get("bark_icon") or os.getenv(f"{source.upper()}_BARK_ICON", "") or defaults[source]).strip()
    parsed = urllib.parse.urlparse(configured)
    return configured if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def parse_timestamp(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000_000:
                timestamp /= 1_000_000
            elif timestamp > 10_000_000_000:
                timestamp /= 1_000
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    try:
        if text.isdigit():
            return parse_timestamp(int(text))
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def max_event_age_seconds() -> float | None:
    raw = os.getenv("CODEX_WATCH_MAX_EVENT_AGE_SECONDS", str(DEFAULT_MAX_EVENT_AGE_SECONDS)).strip()
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_MAX_EVENT_AGE_SECONDS)
    if value <= 0:
        return None
    return value


def event_age_seconds(timestamp: Any) -> float | None:
    parsed = parse_timestamp(timestamp)
    if parsed is None:
        return None
    return (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()


def is_stale_event(event: dict[str, Any]) -> tuple[bool, float | None, float | None]:
    max_age = max_event_age_seconds()
    age = event_age_seconds(event.get("timestamp"))
    if max_age is None or age is None:
        return False, age, max_age
    return age > max_age, age, max_age


def file_head_hash(path: Path, limit: int = 4096) -> str:
    try:
        with path.open("rb") as handle:
            return hashlib.sha256(handle.read(limit)).hexdigest()[:24]
    except OSError:
        return ""


def shell_quote_for_applescript(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Logger:
    def __init__(self, log_path: Path | None, verbose: bool = False) -> None:
        self.log_path = log_path
        self.verbose = verbose
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str, *, always_stdout: bool = False) -> None:
        line = f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {message}"
        if self.verbose or always_stdout:
            print(line, flush=True)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class InstanceLockBusy(RuntimeError):
    pass


class SingleInstanceLock:
    """Hold an OS-backed lock for one state file without relying on stale PID files."""

    def __init__(self, path: Path) -> None:
        self.path = lexical_absolute_path(path)
        self.handle: Any = None

    def acquire(self) -> bool:
        if self.handle is not None:
            return True
        try:
            self.path = prepare_private_file_parent(self.path, "watcher lock")
            validate_existing_private_file(self.path, "watcher lock")
        except StateFileError:
            return False
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOINHERIT"):
            flags |= os.O_NOINHERIT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = -1
        handle: Any = None
        try:
            descriptor = os.open(self.path, flags, PRIVATE_STATE_FILE_MODE)
            metadata = validate_private_regular_descriptor(descriptor, "watcher lock")
            if not path_matches_open_file(self.path, metadata):
                return False
            handle = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = -1
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not path_matches_open_file(self.path, metadata):
                return False
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
            fsync_directory(self.path.parent, "watcher lock")
            if not path_matches_open_file(self.path, metadata):
                return False
            self.handle = handle
            handle = None
            return True
        except (OSError, StateFileError):
            return False
        finally:
            # Closing a failed handle also releases an OS lock that may already
            # have been acquired before a later identity or durability check.
            if handle is not None:
                handle.close()
            elif descriptor >= 0:
                os.close(descriptor)

    def release(self) -> None:
        handle = self.handle
        if handle is None:
            return
        self.handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> SingleInstanceLock:
        if not self.acquire():
            raise InstanceLockBusy(str(self.path))
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.release()


class Notifier:
    def __init__(self, dry_run: bool, log: Logger) -> None:
        self.dry_run = dry_run
        self.log = log
        self.computer = load_or_create_machine()
        self.delivery_mode = load_delivery_mode()
        self.bark_configured = bool(
            (os.getenv("BARK_URL") or "").strip() or (os.getenv("BARK_KEY") or "").strip()
        )
        self.computer_token: str | None = None
        # Bark-only operation deliberately avoids all AgentWatch credential
        # backends. A legacy install with no settings inspects the token once so
        # its established channel combination can be inferred.
        if self.delivery_mode != "bark":
            self.computer_token = ComputerTokenStore(self.computer["computer_id"]).load()
        if self.delivery_mode is None:
            self.delivery_mode = infer_delivery_mode(
                self.bark_configured,
                self.computer_token is not None,
            )
        self.delivery = resolve_delivery(
            self.delivery_mode,
            self.bark_configured,
            self.computer_token is not None,
        )
        self.disabled_channels: set[str] = set()
        self.channels = self._discover_channels()
        self.completed_channels: set[str] = set()
        self.last_successful_channels: set[str] = set()

    def _discover_channels(self) -> list[str]:
        # Only selected, ready phone receivers enter the fan-out. In
        # particular, stale Bark variables cannot make agentwatch-only send to
        # an iPhone, and legacy ntfy variables are never a production channel.
        channels: list[str] = list(self.delivery["effective_channels"])
        if os.getenv("CODEX_NOTIFY_WEBHOOK_URL"):
            channels.append("generic_webhook")
        if os.getenv("WECOM_WEBHOOK_URL") or os.getenv("WECHAT_WORK_WEBHOOK"):
            channels.append("wecom")
        if os.getenv("CODEX_NOTIFY_COMMAND"):
            channels.append("command")
        if platform.system() == "Darwin" and os.getenv("CODEX_WATCH_MACOS_NOTIFICATION", "1") not in {
            "0",
            "false",
            "False",
        }:
            channels.append("macos")
        if self.dry_run and not channels:
            channels.append("dry_run")
        return channels

    def send(self, title: str, body: str, event: dict[str, Any]) -> bool:
        """Run one fan-out round; persistent retry timing is owned by the file processor."""
        if self.dry_run:
            print("\n--- dry-run notification ---", flush=True)
            print(title, flush=True)
            print(body, flush=True)
            print(json.dumps(event, ensure_ascii=False, indent=2), flush=True)
            print("--- end notification ---\n", flush=True)
            return True

        active_channels = [channel for channel in self.channels if channel not in self.disabled_channels]
        if not active_channels:
            if self.delivery_mode is None:
                self.log("no receiver mode selected; run agentwatch install --delivery bark|agentwatch|both")
            else:
                missing = ",".join(self.delivery["missing_channels"]) or "selected receiver"
                self.log(f"no selected phone receiver is ready: {missing}")
            return False

        previously_completed = set(self.completed_channels)
        self.last_successful_channels = set()
        channel_results: dict[str, bool] = {}
        for channel in active_channels:
            if channel in previously_completed:
                channel_results[channel] = True
                continue
            try:
                if channel == "bark":
                    channel_results[channel] = self._send_bark(title, body, event)
                elif channel == "agentwatch":
                    channel_results[channel] = self._send_agentwatch(title, body, event)
                elif channel == "ntfy":
                    # Kept only as an internal compatibility seam for old unit
                    # tests. v0.2 channel discovery never selects direct ntfy,
                    # so a shared topic cannot receive a duplicate delivery.
                    channel_results[channel] = self._send_ntfy(title, body, event)
                elif channel == "generic_webhook":
                    channel_results[channel] = self._send_generic_webhook(title, body, event)
                elif channel == "wecom":
                    channel_results[channel] = self._send_wecom(title, body)
                elif channel == "command":
                    channel_results[channel] = self._send_command(title, body, event)
                elif channel == "macos":
                    channel_results[channel] = self._send_macos(title, body)
            except Exception as exc:  # noqa: BLE001 - log all channel failures and keep other channels alive.
                self.log(f"channel {channel} failed: {exc}")
                channel_results[channel] = False

        self.last_successful_channels = {
            channel
            for channel, succeeded in channel_results.items()
            if succeeded and channel not in previously_completed
        }
        active_after_round = [
            channel for channel in self.channels if channel not in self.disabled_channels
        ]
        active_phone_channels = [
            channel for channel in active_after_round if channel in {"bark", "agentwatch"}
        ]
        if self.delivery_mode in {"bark", "agentwatch", "both"} and not active_phone_channels:
            return False
        external_channels = [
            channel for channel in active_after_round if channel not in {"macos", "dry_run"}
        ]
        required_channels = external_channels or active_after_round
        # A local macOS banner must never conceal a failed phone delivery. Each
        # successful channel is persisted by the caller and skipped on retry,
        # so the second round sends only channels that actually failed.
        return bool(required_channels) and all(channel_results.get(channel, False) for channel in required_channels)

    def _http_post(
        self,
        url: str,
        payload: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> bool:
        headers = {"Content-Type": content_type, "User-Agent": "codex-watch-notifier/1.0"}
        if extra_headers:
            headers.update({key: value for key, value in extra_headers.items() if value})
        request = urllib.request.Request(
            url,
            data=payload,
            headers=headers,
            method="POST",
        )
        timeout = float(os.getenv("CODEX_NOTIFY_HTTP_TIMEOUT", "12"))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            response_body = response.read(300).decode("utf-8", errors="replace")
        if 200 <= status < 300:
            return True
        self.log(f"http webhook returned status={status}: {response_body}")
        return False

    def _send_bark(self, title: str, body: str, event: dict[str, Any]) -> bool:
        url = (os.getenv("BARK_URL") or "").strip()
        if not url:
            key = os.environ["BARK_KEY"].strip()
            url = f"https://api.day.app/{urllib.parse.quote(key)}"
        group = (
            str(event.get("bark_group") or "").strip()
            or os.getenv("CODEX_BARK_GROUP")
            or os.getenv("BARK_GROUP", "Codex")
        )
        payload = {
            "title": title,
            "body": body,
            "group": group,
            "level": os.getenv("BARK_LEVEL", "timeSensitive"),
        }
        stable_id = str(event.get("stable_id") or "").strip()
        if stable_id:
            # Bark maps this stable string to APNs CollapseID and its archive primary key.
            # A delayed retry therefore updates/collapses the same visible notification.
            payload["id"] = f"agent-watch-{stable_id}"
        if "bark_icon" in event:
            icon = str(event.get("bark_icon") or "").strip()
        else:
            icon = (os.getenv("CODEX_BARK_ICON") or os.getenv("BARK_ICON") or "").strip()
        if icon:
            payload["icon"] = icon
        data = urllib.parse.urlencode(payload).encode("utf-8")
        return self._http_post(url, data, "application/x-www-form-urlencoded")

    def _send_agentwatch(self, title: str, body: str, event: dict[str, Any]) -> bool:
        token = self.computer_token
        if not token:
            self.log("AgentWatch private publish skipped: run agentwatch login")
            return False
        priority = str(event.get("agentwatch_priority") or os.getenv("AGENTWATCH_PRIORITY", "default")).strip()
        try:
            AgentWatchApi().publish(
                token,
                event_id=stable_event_id(event, self.computer["computer_id"]),
                source=ntfy_source(event),
                title=compact(title, 160),
                body=compact(body, 3200),
                priority=priority or None,
            )
        except ApiError as exc:
            if exc.status == 401:
                self.computer_token = None
                self.disabled_channels.add("agentwatch")
                self.delivery = resolve_delivery(
                    self.delivery_mode,
                    self.bark_configured,
                    False,
                )
                try:
                    ComputerTokenStore(self.computer["computer_id"]).delete()
                except AgentWatchError as delete_exc:
                    self.log(f"could not remove revoked AgentWatch credential: {delete_exc}")
                self.log("AgentWatch computer token was revoked; run agentwatch login again")
                # A revoked credential is terminal, not a transient delivery
                # failure. In `both`, send() now evaluates the surviving Bark
                # channel and finishes this event without a duplicate retry.
                return False
            raise
        return True

    def _send_ntfy(self, title: str, body: str, event: dict[str, Any]) -> bool:
        prefix = str(event.get("event_type") or "").partition("_")[0].upper()
        if prefix not in {"CODEX", "ZCODE", "KIMI", "GROK", "CLAUDE", "PI", "OPENCODE"}:
            prefix = "CODEX"
        url = (
            str(event.get("ntfy_url") or "").strip()
            or os.getenv(f"{prefix}_NTFY_URL", "").strip()
            or os.getenv("NTFY_URL", "").strip()
        )
        if not url:
            self.log(f"ntfy channel skipped: set NTFY_URL or {prefix}_NTFY_URL")
            return False

        priority = (
            str(event.get("ntfy_priority") or "").strip()
            or os.getenv(f"{prefix}_NTFY_PRIORITY", "").strip()
            or os.getenv("NTFY_PRIORITY", "default").strip()
        )
        tags = (
            str(event.get("ntfy_tags") or "").strip()
            or os.getenv(f"{prefix}_NTFY_TAGS", "").strip()
            or os.getenv("NTFY_TAGS", "").strip()
        )
        tags = ntfy_tags_with_protocol(tags, event)
        query_params = {"title": title}
        if priority:
            query_params["priority"] = priority
        if tags:
            query_params["tags"] = tags
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        url = url + separator + urllib.parse.urlencode(query_params)

        headers = {}
        token = os.getenv("NTFY_TOKEN", "").strip()
        if token:
            if token.lower().startswith(("bearer ", "basic ")):
                headers["Authorization"] = token
            else:
                headers["Authorization"] = f"Bearer {token}"

        sequence_id = ntfy_sequence_id(event)
        if sequence_id:
            # ntfy relays sequence_id to WebSocket subscribers. A delayed HTTP
            # retry therefore updates the same logical event instead of ringing
            # the Android client twice.
            headers["X-Sequence-ID"] = sequence_id
        icon = ntfy_icon(event)
        if icon:
            headers["X-Icon"] = icon

        return self._http_post(url, body.encode("utf-8"), "text/plain; charset=utf-8", headers)

    def _send_generic_webhook(self, title: str, body: str, event: dict[str, Any]) -> bool:
        url = os.environ["CODEX_NOTIFY_WEBHOOK_URL"].strip()
        payload = json.dumps({"title": title, "body": body, "event": event}, ensure_ascii=False).encode("utf-8")
        return self._http_post(url, payload, "application/json; charset=utf-8")

    def _send_wecom(self, title: str, body: str) -> bool:
        url = (os.getenv("WECOM_WEBHOOK_URL") or os.getenv("WECHAT_WORK_WEBHOOK") or "").strip()
        content = f"**{title}**\n\n{body}"
        payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}, ensure_ascii=False).encode(
            "utf-8"
        )
        return self._http_post(url, payload, "application/json; charset=utf-8")

    def _send_command(self, title: str, body: str, event: dict[str, Any]) -> bool:
        command = os.environ["CODEX_NOTIFY_COMMAND"]
        env = os.environ.copy()
        env["CODEX_NOTIFY_TITLE"] = title
        env["CODEX_NOTIFY_BODY"] = body
        env["CODEX_NOTIFY_EVENT_JSON"] = json.dumps(event, ensure_ascii=False)
        timeout = float(os.getenv("CODEX_NOTIFY_COMMAND_TIMEOUT", "30"))
        completed = subprocess.run(command, shell=True, env=env, timeout=timeout, check=False)
        return completed.returncode == 0

    def _send_macos(self, title: str, body: str) -> bool:
        short_body = compact(body, 220)
        script = (
            "display notification "
            + shell_quote_for_applescript(short_body)
            + " with title "
            + shell_quote_for_applescript(title)
        )
        completed = subprocess.run(["/usr/bin/osascript", "-e", script], timeout=12, check=False)
        return completed.returncode == 0


def load_state(path: Path) -> dict[str, Any]:
    path = lexical_absolute_path(path)
    if not os.path.lexists(path):
        return {
            "version": STATE_VERSION,
            "initialized": False,
            "files": {},
            "sent": {},
            "delivery_attempts": {},
            "delivery_stats": {},
        }
    validate_existing_private_file(path, "watcher state")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOINHERIT"):
        flags |= os.O_NOINHERIT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = validate_private_regular_descriptor(descriptor, "watcher state")
        if not path_matches_open_file(path, metadata):
            raise StateFileError(f"watcher state changed while it was being opened: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            state = json.load(handle)
    except StateFileError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateFileError(f"cannot read watcher state {path}: {type(exc).__name__}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(state, dict):
        raise StateFileError(f"watcher state {path} must contain a JSON object")
    if "version" in state and (
        isinstance(state["version"], bool) or not isinstance(state["version"], int)
    ):
        raise StateFileError(f"watcher state {path} has an invalid version")
    for key in ("initialized", "zcode_initialized", "kimi_initialized", "grok_initialized"):
        if key in state and not isinstance(state[key], bool):
            raise StateFileError(f"watcher state {path} field {key!r} must be boolean")
    if "claude_initialized" in state and not isinstance(state["claude_initialized"], str):
        raise StateFileError(
            f"watcher state {path} field 'claude_initialized' must be a path string"
        )
    if "tool_hooks_initialized" in state and not isinstance(state["tool_hooks_initialized"], str):
        raise StateFileError(
            f"watcher state {path} field 'tool_hooks_initialized' must be a path string"
        )
    for key in ("files", "sent", "delivery_attempts", "delivery_stats"):
        value = state.get(key, {})
        if not isinstance(value, dict):
            raise StateFileError(f"watcher state {path} field {key!r} must be an object")
    files = state.get("files", {})
    if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in files.items()):
        raise StateFileError(f"watcher state {path} contains an invalid files entry")
    numeric_file_fields = {
        "offset",
        "size",
        "updated_at",
        "new_file_at",
        "invalid_records_skipped",
        "stale_events_skipped",
        "claude_provisional_stops_suppressed",
        "claude_stop_settle_offset",
        "claude_stop_settle_received_at",
        "claude_spool_started_at",
        "drain_stable_since",
        "drain_stable_size",
    }
    for entry in files.values():
        if any(
            field in entry
            and (
                isinstance(entry[field], bool)
                or not isinstance(entry[field], (int, float))
            )
            for field in numeric_file_fields
        ):
            raise StateFileError(f"watcher state {path} contains invalid file counters")
    sent = state.get("sent", {})
    if any(
        not isinstance(key, str)
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        for key, value in sent.items()
    ):
        raise StateFileError(f"watcher state {path} contains an invalid sent entry")
    delivery_attempts = state.get("delivery_attempts", {})
    if any(
        not isinstance(key, str) or not isinstance(value, dict)
        for key, value in delivery_attempts.items()
    ):
        raise StateFileError(f"watcher state {path} contains an invalid delivery entry")
    numeric_delivery_fields = {
        "attempts",
        "first_attempt_at",
        "last_attempt_at",
        "next_retry_at",
        "exhausted_at",
        "line_offset",
    }
    for entry in delivery_attempts.values():
        if any(
            field in entry
            and entry[field] is not None
            and (
                isinstance(entry[field], bool)
                or not isinstance(entry[field], (int, float))
            )
            for field in numeric_delivery_fields
        ):
            raise StateFileError(f"watcher state {path} contains invalid delivery counters")
    delivery_stats = state.get("delivery_stats", {})
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in delivery_stats.values()
    ):
        raise StateFileError(f"watcher state {path} contains invalid delivery statistics")
    try:
        state["version"] = max(int(state.get("version", 1)), STATE_VERSION)
    except (TypeError, ValueError):
        state["version"] = STATE_VERSION
    state.setdefault("initialized", False)
    state.setdefault("files", {})
    state.setdefault("sent", {})
    state.setdefault("delivery_attempts", {})
    state.setdefault("delivery_stats", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path = prepare_private_file_parent(path, "watcher state")
    validate_existing_private_file(path, "watcher state")
    state["version"] = STATE_VERSION
    sent = state.get("sent", {})
    if len(sent) > MAX_SENT_KEYS:
        state["sent"] = dict(sorted(sent.items(), key=lambda item: item[1])[-MAX_SENT_KEYS:])
    delivery_attempts = state.setdefault("delivery_attempts", {})
    exhausted = [
        (stable_id, entry)
        for stable_id, entry in delivery_attempts.items()
        if isinstance(entry, dict) and entry.get("status") == "exhausted"
    ]
    if len(exhausted) > MAX_EXHAUSTED_DELIVERIES:
        exhausted.sort(key=lambda item: int(item[1].get("exhausted_at", 0) or 0))
        remove_count = len(exhausted) - MAX_EXHAUSTED_DELIVERIES
        for stable_id, _entry in exhausted[:remove_count]:
            delivery_attempts.pop(stable_id, None)
        stats = state.setdefault("delivery_stats", {})
        stats["pruned_exhausted_total"] = int(stats.get("pruned_exhausted_total", 0) or 0) + remove_count
    encoded = (
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOINHERIT"):
        flags |= os.O_NOINHERIT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    temporary: Path | None = None
    descriptor = -1
    temporary_metadata: os.stat_result | None = None
    try:
        for _attempt in range(16):
            candidate = path.parent / (
                f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            )
            try:
                descriptor = os.open(candidate, flags, PRIVATE_STATE_FILE_MODE)
            except FileExistsError:
                continue
            except OSError as exc:
                raise StateFileError(f"cannot create private watcher state temporary file") from exc
            temporary = candidate
            break
        if temporary is None or descriptor < 0:
            raise StateFileError("cannot allocate a unique watcher state temporary file")

        temporary_metadata = validate_private_regular_descriptor(
            descriptor, "watcher state temporary file"
        )
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise StateFileError("could not write the complete watcher state")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1

        if not path_matches_open_file(temporary, temporary_metadata):
            raise StateFileError("watcher state temporary file changed before publication")
        # Revalidate the destination immediately before publication. `replace`
        # would not follow a final symlink, but refusing it also avoids deleting
        # an unexpected foreign directory entry.
        validate_existing_private_file(path, "watcher state")
        os.replace(temporary, path)
        temporary = None
        validate_existing_private_file(path, "watcher state")
        fsync_directory(path.parent, "watcher state")
    except StateFileError:
        raise
    except OSError as exc:
        raise StateFileError(f"cannot persist watcher state {path}: {type(exc).__name__}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Never broaden cleanup after a failed atomic state write.
                pass


def delivery_attempts_for_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    attempts = state.setdefault("delivery_attempts", {})
    if not isinstance(attempts, dict):
        attempts = {}
        state["delivery_attempts"] = attempts
    return attempts


def has_active_delivery_at(state: dict[str, Any], path: Path, line_offset: int) -> bool:
    expected_path = str(path)
    for entry in delivery_attempts_for_state(state).values():
        if not isinstance(entry, dict) or entry.get("status") not in {"attempting", "retry_wait"}:
            continue
        try:
            entry_offset = int(entry.get("line_offset", -1))
        except (TypeError, ValueError):
            entry_offset = -1
        if entry.get("log_path") == expected_path and entry_offset == line_offset:
            return True
    return False


def delivery_checkpoint(checkpoint: Callable[[], None] | None) -> None:
    if checkpoint is not None:
        checkpoint()


def mark_delivery_exhausted(
    state: dict[str, Any],
    stable_id: str,
    entry: dict[str, Any],
    now: int,
    result: str,
) -> None:
    was_exhausted = entry.get("status") == "exhausted"
    entry.update(
        {
            "status": "exhausted",
            "next_retry_at": None,
            "exhausted_at": now,
            "last_result": result,
        }
    )
    delivery_attempts_for_state(state)[stable_id] = entry
    if not was_exhausted:
        stats = state.setdefault("delivery_stats", {})
        stats["exhausted_total"] = int(stats.get("exhausted_total", 0) or 0) + 1


def deliver_event_with_bounded_retry(
    *,
    state: dict[str, Any],
    rec: dict[str, Any],
    notifier: Notifier,
    log: Logger,
    event: dict[str, Any],
    stable_id: str,
    source: str,
    path: Path,
    line_offset: int,
    line_end: int,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    """Deliver once or schedule one delayed retry without ever exceeding two rounds."""
    now = int(time.time())
    max_attempts = delivery_max_attempts()
    retry_delay = delivery_retry_delay_seconds()
    attempts_by_id = delivery_attempts_for_state(state)
    raw_entry = attempts_by_id.get(stable_id)
    entry = raw_entry if isinstance(raw_entry, dict) else {}
    attempts = max(int(entry.get("attempts", 0) or 0), 0)
    status = str(entry.get("status") or "")

    if status == "exhausted":
        rec["offset"] = line_end
        return "exhausted"

    if status == "attempting":
        if attempts >= max_attempts:
            mark_delivery_exhausted(state, stable_id, entry, now, "outcome_unknown_after_restart")
            rec["offset"] = line_end
            delivery_checkpoint(checkpoint)
            log(f"delivery exhausted without another send for {source} {stable_id} after an interrupted attempt")
            return "exhausted"
        next_retry_at = max(
            int(entry.get("next_retry_at", 0) or 0),
            int(entry.get("last_attempt_at", now) or now) + retry_delay,
        )
        if now < next_retry_at:
            entry["status"] = "retry_wait"
            entry["next_retry_at"] = next_retry_at
            attempts_by_id[stable_id] = entry
            rec["offset"] = line_offset
            delivery_checkpoint(checkpoint)
            return "waiting"

    if status == "retry_wait":
        next_retry_at = int(entry.get("next_retry_at", 0) or 0)
        if now < next_retry_at:
            rec["offset"] = line_offset
            return "waiting"

    if attempts >= max_attempts:
        mark_delivery_exhausted(state, stable_id, entry, now, "attempt_limit_reached")
        rec["offset"] = line_end
        delivery_checkpoint(checkpoint)
        return "exhausted"

    attempt_number = attempts + 1
    session_id = str(event.get("thread_id") or event.get("session_id") or "")
    entry.update(
        {
            "status": "attempting",
            "attempts": attempt_number,
            "first_attempt_at": int(entry.get("first_attempt_at", now) or now),
            "last_attempt_at": now,
            "next_retry_at": None,
            "last_result": "attempting",
            "source": source,
            "event_type": str(event.get("event_type") or ""),
            "session_id": session_id,
            "log_path": str(path),
            "line_offset": line_offset,
        }
    )
    attempts_by_id[stable_id] = entry
    rec["offset"] = line_offset
    event["stable_id"] = stable_id

    completed_channels = entry.get("completed_channels", [])
    if not isinstance(completed_channels, list):
        completed_channels = []
    if hasattr(notifier, "completed_channels"):
        notifier.completed_channels = {
            str(channel) for channel in completed_channels if isinstance(channel, str) and channel
        }

    # Write-ahead is deliberate: a crash cannot reset the retry allowance and create a notification storm.
    delivery_checkpoint(checkpoint)
    try:
        sent = bool(notifier.send(event["notification_title"], event["notification_body"], event))
    except Exception as exc:  # noqa: BLE001 - a notifier failure consumes this bounded attempt.
        log(f"notifier raised for {source} {stable_id}: {exc}")
        sent = False
    finished_at = int(time.time())

    successful_channels = getattr(notifier, "last_successful_channels", set())
    if isinstance(successful_channels, (set, list, tuple)):
        completed = {
            str(channel) for channel in completed_channels if isinstance(channel, str) and channel
        }
        completed.update(str(channel) for channel in successful_channels if str(channel))
        if completed:
            entry["completed_channels"] = sorted(completed)

    if sent:
        state.setdefault("sent", {})[stable_id] = finished_at
        attempts_by_id.pop(stable_id, None)
        rec["offset"] = line_end
        delivery_checkpoint(checkpoint)
        return "sent"

    entry["last_result"] = "incomplete_channels"
    if attempt_number < max_attempts:
        entry["status"] = "retry_wait"
        entry["next_retry_at"] = finished_at + retry_delay
        attempts_by_id[stable_id] = entry
        rec["offset"] = line_offset
        delivery_checkpoint(checkpoint)
        log(
            f"delivery failed for {source} {stable_id}; "
            f"one final retry scheduled in {retry_delay}s (attempt {attempt_number}/{max_attempts})"
        )
        return "retry_scheduled"

    mark_delivery_exhausted(state, stable_id, entry, finished_at, "incomplete_channels")
    rec["offset"] = line_end
    delivery_checkpoint(checkpoint)
    log(f"delivery exhausted for {source} {stable_id} after {attempt_number} attempts; no more automatic sends")
    return "exhausted"


def rollout_files(roots: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file() and root.name.endswith(".jsonl"):
            paths.append(root)
        elif root.name == "archived_sessions":
            paths.extend(root.glob("rollout-*.jsonl"))
        else:
            paths.extend(root.glob("**/rollout-*.jsonl"))
    return sorted(set(paths), key=lambda path: str(path))


def zcode_log_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file() and root.name.endswith(".jsonl"):
        return [root]
    return sorted(root.glob("zcode-*.jsonl"), key=lambda path: str(path))


def kimi_wire_files(root: Path, include_subagents: bool | None = None) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file() and root.name == "wire.jsonl":
        return [root]
    if include_subagents is None:
        include_subagents = env_flag("KIMI_WATCH_NOTIFY_SUBAGENTS", False)
    pattern = "**/agents/*/wire.jsonl" if include_subagents else "**/agents/main/wire.jsonl"
    return sorted(root.glob(pattern), key=lambda path: str(path))


def grok_event_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file() and root.name == "events.jsonl":
        return [root]
    return sorted(root.glob("**/events.jsonl"), key=lambda path: str(path))


def regular_file_without_symlink(path: Path) -> bool:
    if path_has_link_component(path):
        return False
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def claude_hook_event_files(path: Path) -> list[Path]:
    if not regular_file_without_symlink(path):
        return []
    return [path]


def tool_hook_event_files(root: Path) -> list[Path]:
    if path_has_link_component(root):
        return []
    try:
        metadata = root.lstat()
    except OSError:
        return []
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return []
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        return []
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        return []
    paths: list[Path] = []
    try:
        candidates = sorted(root.glob("*.json"), key=lambda path: path.name)
    except OSError:
        return []
    for path in candidates:
        if read_owned_tool_hook_event(path) is not None:
            paths.append(path)
    return paths


def read_owned_tool_hook_event_snapshot(
    path: Path,
) -> tuple[dict[str, Any], str, int] | None:
    """Read one owned queue item from a no-follow fd and return its identity."""
    match = TOOL_HOOK_EVENT_FILE_RE.fullmatch(path.name)
    if match is None or path_has_link_component(path):
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            return None
        if metadata.st_size > TOOL_HOOK_EVENT_MAX_BYTES:
            return None
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            return None
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
            return None
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        return None
    try:
        record = json.loads(raw[:-1].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    parsed = validated_tool_hook_record(record)
    if parsed is None or parsed["source"] != match.group("source"):
        return None
    identity = hashlib.sha256(
        f"{parsed['source']}\0{parsed['session_id']}\0{parsed['event_id']}".encode("utf-8")
    ).hexdigest()[:16]
    if identity != match.group("identity"):
        return None
    return record, f"{metadata.st_dev}:{metadata.st_ino}", metadata.st_size


def read_owned_tool_hook_event(path: Path) -> dict[str, Any] | None:
    """Return one authenticated AgentWatch queue record without trusting its name alone."""
    snapshot = read_owned_tool_hook_event_snapshot(path)
    return snapshot[0] if snapshot is not None else None


def claude_drain_prefix(spool_path: Path) -> str:
    return f".{spool_path.name}{CLAUDE_DRAIN_MARKER}"


def parse_claude_drain_path(spool_path: Path, drain_path: Path) -> tuple[int, int, str] | None:
    prefix = claude_drain_prefix(spool_path)
    if drain_path.parent != spool_path.parent or not drain_path.name.startswith(prefix):
        return None
    parts = drain_path.name[len(prefix) :].split("-")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    token = parts[2]
    if len(token) != 8 or any(character not in "0123456789abcdef" for character in token):
        return None
    return int(parts[0]), int(parts[1]), token


def claude_drain_files(spool_path: Path) -> list[Path]:
    try:
        prefix = claude_drain_prefix(spool_path)
        candidates = (path for path in spool_path.parent.iterdir() if path.name.startswith(prefix))
        return sorted(
            (
                path
                for path in candidates
                if parse_claude_drain_path(spool_path, path) is not None
                and regular_file_without_symlink(path)
            ),
            key=lambda path: str(path),
        )
    except OSError:
        return []


def owned_claude_drain_files(spool_path: Path, state: dict[str, Any]) -> list[Path]:
    """Return only drains whose persisted inode identity still belongs to us."""
    files = state.setdefault("files", {})
    owned: list[Path] = []
    for path in claude_drain_files(spool_path):
        rec = files.get(str(path))
        if not isinstance(rec, dict) or not rec.get("claude_drain"):
            continue
        if rec.get("claude_spool_path") != str(spool_path) or rec.get("foreign_replacement"):
            continue
        expected_identity = str(rec.get("file_identity") or "")
        if expected_identity and file_identity(path) == expected_identity:
            owned.append(path)
    return owned


def claude_drain_baseline_offset(spool_path: Path, drain_path: Path) -> int | None:
    parsed = parse_claude_drain_path(spool_path, drain_path)
    return parsed[0] if parsed is not None else None


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_session_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(30):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "session_meta":
                    payload = record.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    return {
                        "thread_id": payload.get("id") or path.stem,
                        "cwd": payload.get("cwd") or "",
                        "source": payload.get("source") or "",
                        "originator": payload.get("originator") or "",
                        "thread_source": payload.get("thread_source") or "",
                        "parent_thread_id": payload.get("parent_thread_id") or "",
                    }
    except OSError:
        pass
    return {
        "thread_id": path.stem,
        "cwd": "",
        "source": "",
        "originator": "",
        "thread_source": "",
        "parent_thread_id": "",
    }


def is_subagent_session(meta: dict[str, Any]) -> bool:
    return str(meta.get("thread_source") or "").strip().lower() == "subagent"


def load_thread_title(thread_id: str) -> str:
    index_path = expand_path(os.getenv("CODEX_SESSION_INDEX", DEFAULT_SESSION_INDEX))
    if not index_path.exists():
        return ""

    title = ""
    try:
        with index_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("id") == thread_id:
                    title = str(record.get("thread_name") or "")
    except OSError:
        return ""
    return title.strip()


def classify_task_complete(message: str, agent_name: str = "Codex") -> tuple[str, str]:
    text = compact(message, 1600)
    lowered = text.lower()

    needs_attention_markers = [
        "需要你",
        "等你",
        "你确认",
        "请确认",
        "是否",
        "要不要",
        "可以吗",
        "你看",
        "如果你",
        "我建议",
        "我准备",
        "下一步",
        "做不了",
        "无法",
        "失败",
        "报错",
        "blocked",
        "cannot",
        "can't",
        "failed",
        "error",
        "confirm",
        "should i",
    ]
    completion_markers = [
        "已完成",
        "完成了",
        "完成。",
        "改完了",
        "修复",
        "验证通过",
        "已创建",
        "已安装",
        "已处理",
        "done",
        "completed",
    ]

    if any(marker in lowered for marker in needs_attention_markers):
        return "需要处理", "根据最后回复判断，可能需要你确认、接手或处理异常"
    if any(marker in lowered for marker in completion_markers):
        return "完成", "根据最后回复判断，任务大概率已完成"
    return "已停下", f"{agent_name} 已结束本轮；当前版本没有写出更细的官方状态"


def trigger_from_record(
    path: Path,
    offset: int,
    record: Any,
    extra_types: set[str],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    if record.get("type") != "event_msg":
        return None

    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if event_type not in {"task_complete", "turn_aborted"} and event_type not in extra_types:
        return None

    meta = meta or load_session_meta(path)
    if is_subagent_session(meta) and not notify_subagents_enabled():
        return None
    timestamp = record.get("timestamp")
    message = payload.get("last_agent_message") or payload.get("reason") or payload.get("message") or ""

    if event_type == "task_complete":
        status, status_detail = classify_task_complete(str(message))
        if status == "完成":
            title = "Codex 已完成"
        elif status == "需要处理":
            title = "Codex 需要处理"
        else:
            title = "Codex 已停下"
    elif event_type == "turn_aborted":
        title = "Codex 会话已中止"
        status = "被中止或停止"
        status_detail = "当前 turn 被中断"
    else:
        title = f"Codex 事件: {event_type}"
        status = event_type or "event"
        status_detail = "自定义事件触发"

    thread_id = meta.get("thread_id") or path.stem
    thread_title = load_thread_title(str(thread_id))
    display_name = thread_title or str(thread_id)[:8]
    short_thread = str(thread_id)[:8]
    cwd = meta.get("cwd") or "(unknown cwd)"
    local_time = utc_to_local(timestamp)
    event = {
        "event_type": event_type,
        "timestamp": timestamp,
        "local_time": local_time,
        "thread_id": thread_id,
        "turn_id": payload.get("turn_id"),
        "thread_title": thread_title,
        "status": status,
        "status_detail": status_detail,
        "cwd": cwd,
        "rollout_path": str(path),
        "offset": offset,
        "message": message,
    }

    body_parts = [
        f"状态: {status}",
        f"判断: {status_detail}",
        f"会话: {display_name}",
        f"线程: {short_thread}",
        f"时间: {local_time}",
    ]
    if include_workspace_in_notifications():
        body_parts.append(f"目录: {cwd}")
    if message and include_message_excerpt_in_notifications() and notification_body_max_chars() > 0:
        body_parts.extend(["", compact(message, notification_body_max_chars())])
    body = "\n".join(body_parts)
    event["notification_title"] = f"{title}: {compact(display_name, 42)}"
    event["notification_body"] = body
    return event


def codex_event_stable_id(event: dict[str, Any]) -> str:
    thread_id = str(event.get("thread_id") or "")
    event_type = str(event.get("event_type") or "")
    turn_id = str(event.get("turn_id") or "").strip()
    if turn_id:
        source = f"codex:{thread_id}:{event_type}:turn:{turn_id}"
    else:
        message = str(event.get("message") or "")
        message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()[:24]
        source = f"codex:{thread_id}:{event_type}:time:{event.get('timestamp')}:message:{message_hash}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def trigger_from_zcode_record(path: Path, offset: int, record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    if record.get("message") != "ZCode Protocol background turn completed":
        return None

    context = record.get("context") or {}
    if not isinstance(context, dict):
        return None
    session_id = str(record.get("sessionId") or "")
    input_id = str(context.get("inputId") or "")
    query_id = str(context.get("queryId") or "")
    workspace = str(context.get("workspacePath") or "")
    display_name = Path(workspace).name if workspace else (session_id or "ZCode")
    timestamp = record.get("timestamp")
    local_time = utc_to_local(timestamp)
    duration_ms = record.get("durationMs")
    duration = ""
    if isinstance(duration_ms, (int, float)):
        duration = f"{duration_ms / 1000:.1f}s"

    event = {
        "event_type": "zcode_turn_completed",
        "timestamp": timestamp,
        "local_time": local_time,
        "session_id": session_id,
        "input_id": input_id,
        "query_id": query_id,
        "status": "完成",
        "status_detail": "ZCode background turn completed",
        "cwd": workspace or "(unknown workspace)",
        "log_path": str(path),
        "offset": offset,
        "duration_ms": duration_ms,
        "bark_group": os.getenv("ZCODE_BARK_GROUP", "ZCode"),
        "bark_icon": os.getenv("ZCODE_BARK_ICON", ""),
        "ntfy_url": os.getenv("ZCODE_NTFY_URL", ""),
        "ntfy_tags": os.getenv("ZCODE_NTFY_TAGS", "zap,computer"),
    }

    body_parts = [
        "状态: 完成",
        "判断: ZCode 本轮已结束",
        f"会话: {display_name}",
        f"时间: {local_time}",
    ]
    if include_workspace_in_notifications():
        body_parts.append(f"目录: {workspace or '(unknown workspace)'}")
    if session_id:
        body_parts.append(f"Session: {session_id[:12]}")
    if query_id:
        body_parts.append(f"Query: {query_id[:12]}")
    if input_id:
        body_parts.append(f"Input: {input_id[:12]}")
    if duration:
        body_parts.append(f"耗时: {duration}")

    event["notification_title"] = f"ZCode 已完成: {compact(display_name, 42)}"
    event["notification_body"] = "\n".join(body_parts)
    return event


def zcode_event_stable_id(event: dict[str, Any], path: Path, line_offset: int) -> str:
    session_id = str(event.get("session_id") or "")
    query_id = str(event.get("query_id") or "")
    input_id = str(event.get("input_id") or "")
    event_type = str(event.get("event_type") or "")
    timestamp = str(event.get("timestamp") or "")
    if query_id:
        source = f"zcode:{session_id}:{event_type}:query:{query_id}"
    elif input_id:
        source = f"zcode:{session_id}:{event_type}:input:{input_id}"
    elif session_id or timestamp:
        source = f"zcode:{session_id}:{event_type}:time:{timestamp}"
    else:
        source = f"zcode:{path}:{line_offset}:{event_type}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def _claude_record_string(
    record: dict[str, Any],
    key: str,
    *,
    limit: int,
    required: bool = False,
) -> str | None:
    value = record.get(key)
    if not isinstance(value, str) or len(value) > limit:
        return None
    normalized = value.strip()
    if required and not normalized:
        return None
    return normalized


def validated_claude_hook_record(record: Any) -> dict[str, Any] | None:
    """Accept only the exact private-spool contract emitted by agentwatch hook."""
    if not isinstance(record, dict) or record.get("schema") != CLAUDE_HOOK_SCHEMA:
        return None
    hook_event_name = record.get("hook_event_name")
    if hook_event_name not in {"Stop", "StopFailure"}:
        return None

    session_id = _claude_record_string(record, "session_id", required=True, limit=256)
    prompt_id = _claude_record_string(record, "prompt_id", limit=256)
    transcript_path = _claude_record_string(
        record, "transcript_path", required=True, limit=4096
    )
    cwd = _claude_record_string(record, "cwd", required=True, limit=4096)
    message = _claude_record_string(
        record, "last_assistant_message", limit=CLAUDE_HOOK_MESSAGE_LIMIT_CHARS
    )
    message_hash = _claude_record_string(
        record, "last_assistant_message_sha256", required=True, limit=64
    )
    error = _claude_record_string(record, "error", limit=64)
    error_details = _claude_record_string(record, "error_details", limit=4096)
    if None in {
        session_id,
        prompt_id,
        transcript_path,
        cwd,
        message,
        message_hash,
        error,
        error_details,
    }:
        return None
    if len(message_hash) != 64 or any(
        character not in "0123456789abcdef" for character in message_hash
    ):
        return None

    transcript_size = record.get("transcript_size")
    if isinstance(transcript_size, bool) or not isinstance(transcript_size, int) or transcript_size < -1:
        return None
    received_at = record.get("received_at")
    if isinstance(received_at, bool) or not isinstance(received_at, int) or received_at <= 0:
        return None
    stop_hook_active = record.get("stop_hook_active", False)
    has_background_tasks = record.get("has_background_tasks", False)
    has_session_crons = record.get("has_session_crons", False)
    if (
        not isinstance(stop_hook_active, bool)
        or not isinstance(has_background_tasks, bool)
        or not isinstance(has_session_crons, bool)
    ):
        return None

    if hook_event_name == "StopFailure":
        if (
            error not in CLAUDE_STOP_FAILURE_ERRORS
            or stop_hook_active
            or has_background_tasks
            or has_session_crons
        ):
            return None
    else:
        if not message or error or error_details or has_background_tasks or has_session_crons:
            return None

    return {
        "hook_event_name": hook_event_name,
        "session_id": session_id,
        "prompt_id": prompt_id,
        "transcript_path": transcript_path,
        "transcript_size": transcript_size,
        "cwd": cwd,
        "received_at": received_at,
        "last_assistant_message": message,
        "last_assistant_message_sha256": message_hash,
        "error": error,
        "error_details": error_details,
        "stop_hook_active": stop_hook_active,
        "has_background_tasks": has_background_tasks,
        "has_session_crons": has_session_crons,
    }


def trigger_from_claude_hook_record(path: Path, offset: int, record: dict[str, Any]) -> dict[str, Any] | None:
    parsed = validated_claude_hook_record(record)
    if parsed is None:
        return None

    hook_event_name = parsed["hook_event_name"]
    session_id = parsed["session_id"]
    prompt_id = parsed["prompt_id"]
    transcript_path = parsed["transcript_path"]
    transcript_size = parsed["transcript_size"]
    cwd = parsed["cwd"]
    timestamp = parsed["received_at"]
    message = parsed["last_assistant_message"]
    message_hash = parsed["last_assistant_message_sha256"]
    error = parsed["error"]
    error_details = parsed["error_details"]
    stop_hook_active = parsed["stop_hook_active"]

    if hook_event_name == "StopFailure":
        status = "需要处理"
        status_detail = f"Claude Code 本轮因 {error} 结束"
        title = "Claude Code 需要处理"
        event_type = "claude_turn_attention"
    else:
        status, status_detail = classify_task_complete(message, "Claude Code")
        if status == "完成":
            title = "Claude Code 已结束本轮"
            event_type = "claude_turn_completed"
        elif status == "需要处理":
            title = "Claude Code 需要处理"
            event_type = "claude_turn_attention"
        else:
            title = "Claude Code 已结束本轮"
            event_type = "claude_turn_completed"

    error_hash = hashlib.sha256(f"{error}\0{error_details}".encode("utf-8")).hexdigest()
    if prompt_id:
        stable_source = f"claude\0{hook_event_name.lower()}\0{session_id}\0{prompt_id}"
    else:
        stable_source = json.dumps(
            [session_id, transcript_path, transcript_size, message_hash, error_hash],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    stable_id = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:24]
    local_time = utc_to_local(timestamp)
    display_name = Path(cwd).name or session_id[:12]
    event = {
        "event_type": event_type,
        "timestamp": timestamp,
        "local_time": local_time,
        "session_id": session_id,
        "prompt_id": prompt_id,
        "status": status,
        "status_detail": status_detail,
        "cwd": cwd,
        "transcript_path": transcript_path,
        "transcript_size": transcript_size,
        "stop_hook_active": stop_hook_active,
        "log_path": str(path),
        "offset": offset,
        "message": message,
        "error": error,
        "error_details": error_details,
        "stable_id": stable_id,
        "bark_group": os.getenv("CLAUDE_BARK_GROUP", "Claude Code"),
        "bark_icon": os.getenv("CLAUDE_BARK_ICON", DEFAULT_CLAUDE_BARK_ICON),
        "ntfy_url": os.getenv("CLAUDE_NTFY_URL", ""),
        "ntfy_tags": os.getenv("CLAUDE_NTFY_TAGS", "robot,computer"),
    }
    body_parts = [
        f"状态: {status}",
        f"判断: {status_detail}",
        f"会话: {display_name}",
        f"时间: {local_time}",
    ]
    if include_workspace_in_notifications():
        body_parts.append(f"目录: {cwd}")
    if message and include_message_excerpt_in_notifications() and notification_body_max_chars() > 0:
        body_parts.extend(["", compact(message, notification_body_max_chars())])
    event["notification_title"] = f"{title}: {compact(display_name, 42)}"
    event["notification_body"] = "\n".join(body_parts)
    return event


def validated_tool_hook_record(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict) or record.get("schema") != TOOL_HOOK_SCHEMA:
        return None
    source = _claude_record_string(record, "source", required=True, limit=16)
    event_name = _claude_record_string(record, "event_name", required=True, limit=32)
    session_id = _claude_record_string(record, "session_id", required=True, limit=256)
    event_id = _claude_record_string(record, "event_id", required=True, limit=256)
    cwd = _claude_record_string(record, "cwd", required=True, limit=4096)
    parent_session = _claude_record_string(record, "parent_session", limit=4096)
    stop_reason = _claude_record_string(record, "stop_reason", limit=128)
    # Assistant text is hashed exactly as emitted. Do not strip leading or
    # trailing whitespace here or a valid record's digest would no longer
    # match the private ingestor's digest.
    message_value = record.get("message")
    message = (
        message_value
        if isinstance(message_value, str) and len(message_value) <= TOOL_HOOK_MESSAGE_LIMIT_CHARS
        else None
    )
    message_hash = _claude_record_string(
        record,
        "message_sha256",
        required=True,
        limit=64,
    )
    if None in {
        source,
        event_name,
        session_id,
        event_id,
        cwd,
        parent_session,
        stop_reason,
        message,
        message_hash,
    }:
        return None
    expected_event = {"pi": "agent_settled", "opencode": "session.idle"}.get(source)
    if event_name != expected_event:
        return None
    outcome = record.get("outcome")
    if outcome not in {"completed", "error", "cancelled"}:
        return None
    timestamp = record.get("timestamp")
    if parse_timestamp(timestamp) is None:
        return None
    received_at = record.get("received_at")
    if isinstance(received_at, bool) or not isinstance(received_at, int) or received_at <= 0:
        return None
    if (
        len(message_hash) != 64
        or any(character not in "0123456789abcdef" for character in message_hash)
        or hashlib.sha256(message.encode("utf-8")).hexdigest() != message_hash
    ):
        return None
    return {
        "source": source,
        "event_name": event_name,
        "session_id": session_id,
        "event_id": event_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "parent_session": parent_session,
        "outcome": outcome,
        "stop_reason": stop_reason,
        "message": message,
        "message_sha256": message_hash,
        "received_at": received_at,
    }


def trigger_from_tool_hook_record(path: Path, offset: int, record: dict[str, Any]) -> dict[str, Any] | None:
    parsed = validated_tool_hook_record(record)
    if parsed is None:
        return None
    source = parsed["source"]
    if not env_flag(f"{source.upper()}_WATCH_ENABLED", True):
        return None
    parent_session = parsed["parent_session"]
    if source == "opencode" and parent_session:
        return None
    if (
        source == "pi"
        and parent_session
        and not env_flag("PI_WATCH_NOTIFY_FORKED_SESSIONS", False)
    ):
        return None

    labels = {"pi": "Pi Agent", "opencode": "OpenCode"}
    tool_name = labels[source]
    outcome = parsed["outcome"]
    message = parsed["message"]
    if outcome == "completed":
        status, status_detail = classify_task_complete(message, tool_name)
        title = {
            "完成": f"{tool_name} 已完成",
            "需要处理": f"{tool_name} 需要处理",
            "已停下": f"{tool_name} 已停下",
        }[status]
        event_type = f"{source}_turn_completed"
    elif outcome == "cancelled":
        status = "已取消"
        status_detail = f"{tool_name} 本轮被取消或停止"
        title = f"{tool_name} 已取消"
        event_type = f"{source}_turn_cancelled"
    else:
        status = "需要处理"
        detail = parsed["stop_reason"] or "error"
        status_detail = f"{tool_name} 本轮因 {compact(detail, 80)} 结束"
        title = f"{tool_name} 需要处理"
        event_type = f"{source}_turn_error"

    session_id = parsed["session_id"]
    event_id = parsed["event_id"]
    cwd = parsed["cwd"]
    display_name = Path(cwd).name or session_id[:12]
    timestamp = parsed["timestamp"]
    local_time = utc_to_local(timestamp)
    stable_source = f"{source}\0{session_id}\0{event_id}\0{outcome}"
    default_icons = {
        "pi": DEFAULT_PI_BARK_ICON,
        "opencode": DEFAULT_OPENCODE_BARK_ICON,
    }
    event = {
        "event_type": event_type,
        "timestamp": timestamp,
        "local_time": local_time,
        "session_id": session_id,
        "event_id": event_id,
        "status": status,
        "status_detail": status_detail,
        "cwd": cwd,
        "parent_session": parent_session,
        "log_path": str(path),
        "offset": offset,
        "message": message,
        "stable_id": hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:24],
        "bark_group": os.getenv(f"{source.upper()}_BARK_GROUP", tool_name),
        "bark_icon": os.getenv(f"{source.upper()}_BARK_ICON", default_icons[source]),
        "ntfy_url": os.getenv(f"{source.upper()}_NTFY_URL", ""),
        "ntfy_tags": os.getenv(f"{source.upper()}_NTFY_TAGS", "robot,computer"),
    }
    body_parts = [
        f"状态: {status}",
        f"判断: {status_detail}",
        f"会话: {display_name}",
        f"时间: {local_time}",
    ]
    if include_workspace_in_notifications():
        body_parts.append(f"目录: {cwd}")
    if message and include_message_excerpt_in_notifications() and notification_body_max_chars() > 0:
        body_parts.extend(["", compact(message, notification_body_max_chars())])
    event["notification_title"] = f"{title}: {compact(display_name, 42)}"
    event["notification_body"] = "\n".join(body_parts)
    return event


def load_kimi_session_meta(path: Path) -> dict[str, Any]:
    try:
        session_dir = path.parents[2]
        agent_id = path.parent.name
    except IndexError:
        return {}

    state = load_json_object(session_dir / "state.json")
    agents = state.get("agents") if isinstance(state.get("agents"), dict) else {}
    agent = agents.get(agent_id) if isinstance(agents.get(agent_id), dict) else {}
    session_id = session_dir.name.removeprefix("session_")
    parent_agent_id = str(agent.get("parentAgentId") or "")
    agent_type = str(agent.get("type") or "")
    return {
        "session_id": str(state.get("id") or session_id),
        "session_title": str(state.get("title") or ""),
        "cwd": str(state.get("workDir") or ""),
        "agent_id": agent_id,
        "agent_type": agent_type,
        "parent_agent_id": parent_agent_id,
        "is_subagent": agent_id != "main" or (agent_type and agent_type != "main") or bool(parent_agent_id),
    }


def load_kimi_turn_message(path: Path, end_offset: int, turn_id: str, step: Any) -> str:
    parts_by_step: dict[str, list[str]] = {}
    try:
        with path.open("rb") as handle:
            while handle.tell() < end_offset:
                line_start = handle.tell()
                line = handle.readline()
                if not line or line_start >= end_offset:
                    break
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                event = record.get("event") if record.get("type") == "context.append_loop_event" else None
                if not isinstance(event, dict) or event.get("type") != "content.part":
                    continue
                if str(event.get("turnId") or "") != turn_id:
                    continue
                part = event.get("part")
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = str(part.get("text") or "")
                if text:
                    parts_by_step.setdefault(str(event.get("step")), []).append(text)
    except OSError:
        return ""

    requested_step = str(step)
    if requested_step in parts_by_step:
        return "".join(parts_by_step[requested_step]).strip()
    if not parts_by_step:
        return ""
    latest_step = list(parts_by_step)[-1]
    return "".join(parts_by_step[latest_step]).strip()


def trigger_from_kimi_record(path: Path, offset: int, record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("type") != "context.append_loop_event":
        return None
    loop_event = record.get("event")
    if not isinstance(loop_event, dict) or loop_event.get("type") != "step.end":
        return None
    if loop_event.get("finishReason") != "end_turn":
        return None

    meta = load_kimi_session_meta(path)
    if meta.get("is_subagent") and not env_flag("KIMI_WATCH_NOTIFY_SUBAGENTS", False):
        return None

    turn_id = str(loop_event.get("turnId") or "")
    message = load_kimi_turn_message(path, offset, turn_id, loop_event.get("step"))
    status, status_detail = classify_task_complete(message, "Kimi Code")
    if status == "完成":
        title = "Kimi Code 已完成"
    elif status == "需要处理":
        title = "Kimi Code 需要处理"
    else:
        title = "Kimi Code 已停下"

    session_id = str(meta.get("session_id") or path.parent.name)
    cwd = str(meta.get("cwd") or "(unknown workspace)")
    session_title = str(meta.get("session_title") or "")
    display_name = session_title or (Path(cwd).name if cwd != "(unknown workspace)" else session_id[:12])
    timestamp = record.get("time") or loop_event.get("time")
    local_time = utc_to_local(timestamp)
    stable_source = f"kimi:{session_id}:{meta.get('agent_id')}:{turn_id}:{loop_event.get('step')}"
    event = {
        "event_type": "kimi_turn_completed",
        "timestamp": timestamp,
        "local_time": local_time,
        "session_id": session_id,
        "session_title": session_title,
        "agent_id": meta.get("agent_id"),
        "turn_id": turn_id,
        "status": status,
        "status_detail": status_detail,
        "cwd": cwd,
        "log_path": str(path),
        "offset": offset,
        "message": message,
        "stable_id": hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:24],
        "bark_group": os.getenv("KIMI_BARK_GROUP", "Kimi Code"),
        "bark_icon": os.getenv("KIMI_BARK_ICON", DEFAULT_KIMI_BARK_ICON),
        "ntfy_url": os.getenv("KIMI_NTFY_URL", ""),
        "ntfy_tags": os.getenv("KIMI_NTFY_TAGS", "robot,computer"),
    }
    body_parts = [
        f"状态: {status}",
        f"判断: {status_detail}",
        f"会话: {display_name}",
        f"时间: {local_time}",
    ]
    if include_workspace_in_notifications():
        body_parts.append(f"目录: {cwd}")
    if message and include_message_excerpt_in_notifications() and notification_body_max_chars() > 0:
        body_parts.extend(["", compact(message, notification_body_max_chars())])
    event["notification_title"] = f"{title}: {compact(display_name, 42)}"
    event["notification_body"] = "\n".join(body_parts)
    return event


def load_grok_session_meta(path: Path) -> dict[str, Any]:
    summary = load_json_object(path.parent / "summary.json")
    info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
    return {
        "session_id": str(info.get("id") or path.parent.name),
        "session_title": str(summary.get("generated_title") or summary.get("session_summary") or ""),
        "cwd": str(info.get("cwd") or ""),
        "parent_session_id": str(summary.get("parent_session_id") or info.get("parent_session_id") or ""),
    }


def load_grok_last_assistant_message(path: Path) -> str:
    history_path = path.parent / "chat_history.jsonl"
    message = ""
    try:
        with history_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "assistant":
                    continue
                content = record.get("content")
                if isinstance(content, str) and content.strip():
                    message = content.strip()
    except OSError:
        return ""
    return message


def trigger_from_grok_record(path: Path, offset: int, record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("type") != "turn_ended":
        return None
    outcome = str(record.get("outcome") or "")
    if outcome not in {"completed", "error", "cancelled"}:
        return None

    meta = load_grok_session_meta(path)
    if meta.get("parent_session_id") and not env_flag("GROK_WATCH_NOTIFY_SUBAGENTS", False):
        return None

    message = load_grok_last_assistant_message(path) if outcome == "completed" else ""
    if outcome == "completed":
        status, status_detail = classify_task_complete(message, "Grok Build")
        title = {
            "完成": "Grok Build 已完成",
            "需要处理": "Grok Build 需要处理",
            "已停下": "Grok Build 已停下",
        }[status]
        event_type = "grok_turn_completed"
    elif outcome == "error":
        status = "需要处理"
        status_detail = "Grok Build 本轮执行失败"
        title = "Grok Build 失败"
        event_type = "grok_turn_error"
    else:
        status = "已取消"
        status_detail = "Grok Build 本轮被取消或停止"
        title = "Grok Build 已取消"
        event_type = "grok_turn_cancelled"

    session_id = str(meta.get("session_id") or path.parent.name)
    cwd = str(meta.get("cwd") or "(unknown workspace)")
    session_title = str(meta.get("session_title") or "")
    display_name = session_title or (Path(cwd).name if cwd != "(unknown workspace)" else session_id[:12])
    timestamp = record.get("ts") or record.get("timestamp")
    local_time = utc_to_local(timestamp)
    if timestamp:
        stable_source = f"grok:{session_id}:{outcome}:{timestamp}"
    else:
        stable_source = f"grok:{session_id}:{outcome}:{path}:{offset}"
    event = {
        "event_type": event_type,
        "timestamp": timestamp,
        "local_time": local_time,
        "session_id": session_id,
        "session_title": session_title,
        "parent_session_id": meta.get("parent_session_id"),
        "status": status,
        "status_detail": status_detail,
        "cwd": cwd,
        "log_path": str(path),
        "offset": offset,
        "message": message,
        "stable_id": hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:24],
        "bark_group": os.getenv("GROK_BARK_GROUP", "Grok Build"),
        "bark_icon": os.getenv("GROK_BARK_ICON", DEFAULT_GROK_BARK_ICON),
        "ntfy_url": os.getenv("GROK_NTFY_URL", ""),
        "ntfy_tags": os.getenv("GROK_NTFY_TAGS", "robot,computer"),
    }
    body_parts = [
        f"状态: {status}",
        f"判断: {status_detail}",
        f"会话: {display_name}",
        f"时间: {local_time}",
    ]
    if include_workspace_in_notifications():
        body_parts.append(f"目录: {cwd}")
    if message and include_message_excerpt_in_notifications() and notification_body_max_chars() > 0:
        body_parts.extend(["", compact(message, notification_body_max_chars())])
    event["notification_title"] = f"{title}: {compact(display_name, 42)}"
    event["notification_body"] = "\n".join(body_parts)
    return event


def process_file(
    path: Path,
    state: dict[str, Any],
    notifier: Notifier,
    extra_types: set[str],
    log: Logger,
    checkpoint: Callable[[], None] | None = None,
) -> int:
    files = state.setdefault("files", {})
    key = str(path)
    rec = files.setdefault(key, {"offset": 0})
    try:
        size = path.stat().st_size
    except OSError as exc:
        log(f"cannot stat {path}: {exc}")
        return 0

    offset = int(rec.get("offset", 0))
    previous_size = int(rec.get("size", 0) or 0)
    previous_head_hash = str(rec.get("head_hash") or "")
    current_head_hash = file_head_hash(path)
    meta = load_session_meta(path)
    subagent_suppressed = is_subagent_session(meta) and not notify_subagents_enabled()
    if subagent_suppressed and not rec.get("subagent_suppression_logged"):
        rec["subagent_suppression_logged"] = True
        log(f"subagent notifications suppressed for {meta.get('thread_id') or path.stem} from {path.name}")
    if (
        previous_head_hash
        and current_head_hash
        and current_head_hash != previous_head_hash
        and offset > 0
        and not has_active_delivery_at(state, path, offset)
    ):
        rec["offset"] = size
        rec["size"] = size
        rec["head_hash"] = current_head_hash
        rec["updated_at"] = int(time.time())
        log(f"rollout appears rewritten; baselined at EOF without replaying history: {path.name}")
        return 0
    if offset > size:
        rec["offset"] = size
        rec["size"] = size
        rec["head_hash"] = current_head_hash or file_head_hash(path)
        rec["updated_at"] = int(time.time())
        log(f"rollout shrank; baselined at EOF without replaying history: {path.name}")
        return 0

    sent_count = 0
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                line_end = handle.tell()
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    rec["offset"] = line_end
                    continue
                if not isinstance(record, dict):
                    invalid = int(rec.get("invalid_records_skipped", 0) or 0) + 1
                    rec["invalid_records_skipped"] = invalid
                    rec["offset"] = line_end
                    continue
                try:
                    event = trigger_from_record(path, line_offset, record, extra_types, meta)
                except Exception as exc:  # noqa: BLE001 - isolate one poisoned rollout record.
                    invalid = int(rec.get("invalid_records_skipped", 0) or 0) + 1
                    rec["invalid_records_skipped"] = invalid
                    if invalid <= 3 or invalid in {10, 50, 100} or invalid % 1000 == 0:
                        log(
                            f"skipped invalid Codex record from {path.name}: "
                            f"{type(exc).__name__}"
                        )
                    rec["offset"] = line_end
                    continue
                if not event:
                    rec["offset"] = line_end
                    continue

                stable_id = codex_event_stable_id(event)
                stale, age, max_age = is_stale_event(event)
                if stale:
                    skipped = int(rec.get("stale_events_skipped", 0) or 0) + 1
                    rec["stale_events_skipped"] = skipped
                    if skipped <= 3 or skipped in {10, 50, 100, 250, 500} or skipped % 1000 == 0:
                        log(
                            "skipped stale "
                            f"{event['event_type']} for {event['thread_id']} from {path.name} "
                            f"(age={age:.0f}s, max={max_age:.0f}s)"
                        )
                    delivery_attempts_for_state(state).pop(stable_id, None)
                    rec["offset"] = line_end
                    continue

                if stable_id in state.setdefault("sent", {}):
                    delivery_attempts_for_state(state).pop(stable_id, None)
                    rec["offset"] = line_end
                    continue

                # Keep rewrite metadata in the same write-ahead checkpoint as the delivery attempt.
                rec["size"] = size
                rec["head_hash"] = current_head_hash or file_head_hash(path)
                outcome = deliver_event_with_bounded_retry(
                    state=state,
                    rec=rec,
                    notifier=notifier,
                    log=log,
                    event=event,
                    stable_id=stable_id,
                    source="codex",
                    path=path,
                    line_offset=line_offset,
                    line_end=line_end,
                    checkpoint=checkpoint,
                )
                if outcome == "sent":
                    sent_count += 1
                    log(f"sent {event['event_type']} for {event['thread_id']} from {path.name}")
                elif outcome in {"waiting", "retry_scheduled"}:
                    break
    except OSError as exc:
        log(f"cannot read {path}: {exc}")
    finally:
        rec["size"] = size
        rec["head_hash"] = current_head_hash or file_head_hash(path)
        rec["updated_at"] = int(time.time())
    return sent_count


def process_zcode_file(
    path: Path,
    state: dict[str, Any],
    notifier: Notifier,
    log: Logger,
    checkpoint: Callable[[], None] | None = None,
) -> int:
    files = state.setdefault("files", {})
    key = str(path)
    rec = files.setdefault(key, {"offset": 0})
    try:
        size = path.stat().st_size
    except OSError as exc:
        log(f"cannot stat {path}: {exc}")
        return 0

    offset = int(rec.get("offset", 0))
    if offset > size:
        offset = 0

    sent_count = 0
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                line_end = handle.tell()
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    rec["offset"] = line_end
                    continue
                if not isinstance(record, dict):
                    invalid = int(rec.get("invalid_records_skipped", 0) or 0) + 1
                    rec["invalid_records_skipped"] = invalid
                    rec["offset"] = line_end
                    continue
                try:
                    event = trigger_from_zcode_record(path, line_offset, record)
                except Exception as exc:  # noqa: BLE001 - isolate one poisoned log record.
                    invalid = int(rec.get("invalid_records_skipped", 0) or 0) + 1
                    rec["invalid_records_skipped"] = invalid
                    if invalid <= 3 or invalid in {10, 50, 100} or invalid % 1000 == 0:
                        log(
                            f"skipped invalid ZCode record from {path.name}: "
                            f"{type(exc).__name__}"
                        )
                    rec["offset"] = line_end
                    continue
                if not event:
                    rec["offset"] = line_end
                    continue

                stable_id = zcode_event_stable_id(event, path, line_offset)
                if stable_id in state.setdefault("sent", {}):
                    delivery_attempts_for_state(state).pop(stable_id, None)
                    rec["offset"] = line_end
                    continue

                outcome = deliver_event_with_bounded_retry(
                    state=state,
                    rec=rec,
                    notifier=notifier,
                    log=log,
                    event=event,
                    stable_id=stable_id,
                    source="zcode",
                    path=path,
                    line_offset=line_offset,
                    line_end=line_end,
                    checkpoint=checkpoint,
                )
                if outcome == "sent":
                    sent_count += 1
                    log(f"sent {event['event_type']} for {event['session_id']} from {path.name}")
                elif outcome in {"waiting", "retry_scheduled"}:
                    break
    except OSError as exc:
        log(f"cannot read {path}: {exc}")
    finally:
        rec["size"] = size
        rec["updated_at"] = int(time.time())
    return sent_count


def claude_is_provisional_stop_record(record: dict[str, Any]) -> bool:
    parsed = validated_claude_hook_record(record)
    return bool(
        parsed
        and parsed["hook_event_name"] == "Stop"
        and parsed["stop_hook_active"] is False
    )


def claude_stop_transcript_grew(record: dict[str, Any]) -> bool:
    """Observe transcript growth without treating it as proof of continuation.

    Claude documents transcript writes as asynchronous. A normal final Stop can
    therefore be followed by a delayed transcript flush. Growth is only
    corroborating evidence when a matching stop_hook_active=true record is also
    present later in the private spool; growth by itself never suppresses an
    otherwise valid notification.
    """
    if not claude_is_provisional_stop_record(record):
        return False
    transcript_size = record["transcript_size"]
    if transcript_size < 0:
        return False
    transcript_path = Path(
        os.path.expandvars(os.path.expanduser(record["transcript_path"].strip()))
    )
    try:
        return transcript_path.stat().st_size > transcript_size
    except OSError:
        return False


def claude_terminal_hook_record_follows(
    handle: Any,
    path: Path,
    provisional: dict[str, Any],
) -> str:
    """Look ahead for a matching true Stop or StopFailure without advancing state."""
    if not claude_is_provisional_stop_record(provisional):
        return ""
    provisional_event = trigger_from_claude_hook_record(path, handle.tell(), provisional)
    if provisional_event is None:
        return ""
    session_id = provisional_event["session_id"]
    prompt_id = provisional_event["prompt_id"]
    transcript_path = provisional_event["transcript_path"]
    provisional_stable_id = str(provisional_event.get("stable_id") or "")
    position = handle.tell()
    try:
        while True:
            candidate_offset = handle.tell()
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                return ""
            try:
                candidate = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(candidate, dict):
                continue
            candidate_notification = trigger_from_claude_hook_record(
                path, candidate_offset, candidate
            )
            if candidate_notification is None:
                continue
            candidate_event = candidate.get("hook_event_name")
            if (
                candidate_notification["session_id"] != session_id
                or candidate_notification["transcript_path"] != transcript_path
            ):
                continue
            if candidate_event == "Stop" and candidate_notification["stop_hook_active"] is not True:
                continue
            candidate_prompt_id = candidate_notification["prompt_id"]
            if prompt_id and candidate_prompt_id != prompt_id:
                continue
            if candidate_event == "Stop":
                if (
                    not provisional_stable_id
                    or candidate_notification.get("stable_id")
                    != provisional_stable_id
                ):
                    continue
                return "continuation Stop"
            # Stop and StopFailure intentionally use different stable IDs, but
            # prompt_id is the official shared per-turn correlation key on the
            # supported Claude version. Without it, do not let an ambiguous
            # later failure suppress a valid provisional notification.
            if prompt_id and candidate_prompt_id == prompt_id:
                return "StopFailure"
    finally:
        handle.seek(position)


def claude_stop_settle_disposition(
    record: dict[str, Any],
    *,
    now: int | None = None,
) -> str:
    """Return ready or waiting for one provisional Claude Stop record.

    Claude starts every matching Stop hook in parallel. A false
    stop_hook_active value therefore means only "first Stop pass", not that
    every sibling hook has allowed the turn to stop. Keep that record unread
    for a bounded settle window. A caller separately looks ahead for the
    matching true continuation record before allowing the false record through.

    Structurally invalid and non-Stop records remain "ready" so the normal
    parser can skip them without letting a poisoned line pin the spool.
    """
    if not claude_is_provisional_stop_record(record):
        return "ready"

    received_at = record.get("received_at")
    if isinstance(received_at, bool) or not isinstance(received_at, int):
        return "ready"
    current_time = int(time.time()) if now is None else int(now)
    settle_seconds = claude_stop_settle_seconds()
    # A wildly future timestamp is an invalid external spool record. Do not
    # let it hold the queue indefinitely; the regular parser will handle it.
    if received_at <= 0 or received_at > current_time + settle_seconds:
        return "ready"
    return "ready" if current_time - received_at >= settle_seconds else "waiting"


def clear_claude_stop_settle_state(file_state: dict[str, Any]) -> None:
    file_state.pop("claude_stop_settle_offset", None)
    file_state.pop("claude_stop_settle_received_at", None)


def process_external_file(
    path: Path,
    state: dict[str, Any],
    notifier: Notifier,
    log: Logger,
    kind: str,
    trigger: Any,
    checkpoint: Callable[[], None] | None = None,
) -> int:
    files = state.setdefault("files", {})
    rec = files.setdefault(str(path), {"offset": 0, "kind": kind})
    try:
        size = path.stat().st_size
    except OSError as exc:
        log(f"cannot stat {path}: {exc}")
        return 0

    offset = int(rec.get("offset", 0))
    if offset > size:
        rec["offset"] = size
        rec["size"] = size
        rec["updated_at"] = int(time.time())
        log(f"{kind} log shrank; baselined at EOF without replaying history: {path.name}")
        return 0

    sent_count = 0
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                line_end = handle.tell()
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    rec["offset"] = line_end
                    continue
                if not isinstance(record, dict):
                    rec["offset"] = line_end
                    continue
                if kind == "Claude Code":
                    transcript_grew = claude_stop_transcript_grew(record)
                    terminal_followup = claude_terminal_hook_record_follows(
                        handle, path, record
                    )
                    if terminal_followup:
                        clear_claude_stop_settle_state(rec)
                        rec["claude_provisional_stops_suppressed"] = int(
                            rec.get("claude_provisional_stops_suppressed", 0) or 0
                        ) + 1
                        rec["offset"] = line_end
                        evidence = (
                            f"matching {terminal_followup} and transcript growth"
                            if transcript_grew
                            else f"matching {terminal_followup}"
                        )
                        log(
                            "suppressed provisional Claude Code Stop after "
                            f"{evidence}: {path.name}"
                        )
                        continue
                    disposition = claude_stop_settle_disposition(record)
                    if disposition == "waiting":
                        if rec.get("claude_stop_settle_offset") != line_offset:
                            rec["claude_stop_settle_offset"] = line_offset
                            rec["claude_stop_settle_received_at"] = record.get(
                                "received_at"
                            )
                            log(
                                "holding provisional Claude Code Stop until "
                                f"the {claude_stop_settle_seconds()}s settle window closes: "
                                f"{path.name}"
                            )
                        # This is intentionally before trigger/delivery: no
                        # network call, attempt counter, checkpoint, or offset
                        # advance occurs while sibling Stop hooks can still
                        # change Claude's decision.
                        break
                    clear_claude_stop_settle_state(rec)
                try:
                    event = trigger(path, line_offset, record)
                except Exception as exc:  # noqa: BLE001 - one poisoned record must not stop all sources.
                    invalid = int(rec.get("invalid_records_skipped", 0) or 0) + 1
                    rec["invalid_records_skipped"] = invalid
                    if invalid <= 3 or invalid in {10, 50, 100} or invalid % 1000 == 0:
                        log(f"skipped invalid {kind} record from {path.name}: {type(exc).__name__}")
                    rec["offset"] = line_end
                    continue
                if not event:
                    rec["offset"] = line_end
                    continue

                stable_id = str(event.get("stable_id") or "")
                if not stable_id:
                    stable_source = f"{kind}:{path}:{line_offset}:{event.get('event_type')}:{event.get('timestamp')}"
                    stable_id = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:24]
                stale, age, max_age = is_stale_event(event)
                if stale:
                    skipped = int(rec.get("stale_events_skipped", 0) or 0) + 1
                    rec["stale_events_skipped"] = skipped
                    if skipped <= 3 or skipped in {10, 50, 100, 250, 500} or skipped % 1000 == 0:
                        log(
                            f"skipped stale {event['event_type']} for {event.get('session_id')} "
                            f"from {path.name} (age={age:.0f}s, max={max_age:.0f}s)"
                        )
                    delivery_attempts_for_state(state).pop(stable_id, None)
                    rec["offset"] = line_end
                    continue

                if stable_id in state.setdefault("sent", {}):
                    delivery_attempts_for_state(state).pop(stable_id, None)
                    rec["offset"] = line_end
                    continue

                outcome = deliver_event_with_bounded_retry(
                    state=state,
                    rec=rec,
                    notifier=notifier,
                    log=log,
                    event=event,
                    stable_id=stable_id,
                    source=kind.lower().replace(" ", "_"),
                    path=path,
                    line_offset=line_offset,
                    line_end=line_end,
                    checkpoint=checkpoint,
                )
                if outcome == "sent":
                    sent_count += 1
                    log(f"sent {event['event_type']} for {event.get('session_id')} from {path.name}")
                elif outcome in {"waiting", "retry_scheduled"}:
                    break
    except OSError as exc:
        log(f"cannot read {path}: {exc}")
    finally:
        rec["size"] = size
        rec["kind"] = kind
        rec["updated_at"] = int(time.time())
    return sent_count


def initialize_tool_hook_events(
    state: dict[str, Any],
    root: Path,
    *,
    process_existing: bool,
    log: Logger,
) -> bool:
    key = str(root)
    if state.get("tool_hooks_initialized") == key:
        return False
    files = state.setdefault("files", {})
    now = int(time.time())
    count = 0
    for path in tool_hook_event_files(root):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files[str(path)] = {
            "offset": 0 if process_existing else size,
            "size": size,
            "updated_at": now,
            "kind": "Tool Hook",
            "tool_hook_owned": True,
            "file_identity": file_identity(path),
        }
        count += 1
    state["tool_hooks_initialized"] = key
    action = "from BOF" if process_existing else "at EOF"
    log(
        f"initialized Pi/OpenCode hook event queue {action} for {count} file(s): {root}",
        always_stdout=True,
    )
    return True


def cleanup_consumed_tool_hook_event(
    path: Path,
    state: dict[str, Any],
    log: Logger,
    checkpoint: Callable[[], None] | None = None,
) -> bool:
    files = state.setdefault("files", {})
    rec = files.get(str(path))
    if (
        not isinstance(rec, dict)
        or rec.get("kind") != "Tool Hook"
        or rec.get("tool_hook_owned") is not True
    ):
        return False
    if has_active_delivery_for_path(state, path):
        return False
    try:
        metadata = path.lstat()
        offset = int(rec.get("offset", 0) or 0)
    except (OSError, TypeError, ValueError):
        return False
    if offset < metadata.st_size:
        return False
    expected_identity = str(rec.get("file_identity") or "")
    actual_identity = f"{metadata.st_dev}:{metadata.st_ino}"
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not expected_identity
        or expected_identity != actual_identity
        or read_owned_tool_hook_event(path) is None
    ):
        rec["foreign_replacement"] = True
        return False
    try:
        path.unlink()
    except OSError as exc:
        log(f"cannot retire consumed tool hook event {path.name}: {exc}")
        return False
    files.pop(str(path), None)
    delivery_checkpoint(checkpoint)
    return True


def process_tool_hook_event_file(
    path: Path,
    state: dict[str, Any],
    notifier: Notifier,
    log: Logger,
    checkpoint: Callable[[], None] | None = None,
) -> int:
    files = state.setdefault("files", {})
    rec = files.get(str(path))
    snapshot = read_owned_tool_hook_event_snapshot(path)
    if snapshot is None:
        return 0
    record, actual_identity, size = snapshot
    if not isinstance(rec, dict):
        rec = {
            "offset": 0,
            "kind": "Tool Hook",
            "tool_hook_owned": True,
            "file_identity": actual_identity,
            "updated_at": int(time.time()),
        }
        files[str(path)] = rec
    elif rec.get("file_identity") and rec.get("file_identity") != actual_identity:
        rec.update(
            {
                "offset": size,
                "size": size,
                "updated_at": int(time.time()),
                "foreign_replacement": True,
            }
        )
        log(f"ignored replaced tool hook event file: {path.name}")
        return 0
    elif actual_identity and not rec.get("file_identity"):
        rec["file_identity"] = actual_identity
    rec["tool_hook_owned"] = True

    rec["kind"] = "Tool Hook"
    rec["size"] = size
    rec["updated_at"] = int(time.time())
    try:
        offset = int(rec.get("offset", 0) or 0)
    except (TypeError, ValueError):
        offset = size
    if offset not in {0, size}:
        rec["offset"] = size
        rec["foreign_replacement"] = True
        return 0
    if offset == size:
        cleanup_consumed_tool_hook_event(path, state, log, checkpoint)
        return 0

    event = trigger_from_tool_hook_record(path, 0, record)
    if not event:
        rec["offset"] = size
        cleanup_consumed_tool_hook_event(path, state, log, checkpoint)
        return 0
    stable_id = str(event.get("stable_id") or "")
    if not stable_id:
        rec["offset"] = size
        cleanup_consumed_tool_hook_event(path, state, log, checkpoint)
        return 0
    stale, age, max_age = is_stale_event(event)
    if stale:
        rec["stale_events_skipped"] = int(rec.get("stale_events_skipped", 0) or 0) + 1
        delivery_attempts_for_state(state).pop(stable_id, None)
        rec["offset"] = size
        log(
            f"skipped stale Tool Hook event for {event.get('session_id')} "
            f"from {path.name} (age={age:.0f}s, max={max_age:.0f}s)"
        )
        cleanup_consumed_tool_hook_event(path, state, log, checkpoint)
        return 0
    if stable_id in state.setdefault("sent", {}):
        delivery_attempts_for_state(state).pop(stable_id, None)
        rec["offset"] = size
        cleanup_consumed_tool_hook_event(path, state, log, checkpoint)
        return 0

    outcome = deliver_event_with_bounded_retry(
        state=state,
        rec=rec,
        notifier=notifier,
        log=log,
        event=event,
        stable_id=stable_id,
        source="tool_hook",
        path=path,
        line_offset=0,
        line_end=size,
        checkpoint=checkpoint,
    )
    sent = 1 if outcome == "sent" else 0
    if outcome == "sent":
        log(f"sent {event['event_type']} for {event.get('session_id')} from {path.name}")
    cleanup_consumed_tool_hook_event(path, state, log, checkpoint)
    return sent


def baseline_existing_files(state: dict[str, Any], roots: list[Path], log: Logger) -> None:
    files = state.setdefault("files", {})
    count = 0
    for path in rollout_files(roots):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files[str(path)] = {
            "offset": size,
            "size": size,
            "head_hash": file_head_hash(path),
            "updated_at": int(time.time()),
        }
        count += 1
    state["initialized"] = True
    log(f"initialized baseline at EOF for {count} rollout files", always_stdout=True)


def baseline_existing_zcode_files(state: dict[str, Any], root: Path, log: Logger) -> None:
    files = state.setdefault("files", {})
    count = 0
    for path in zcode_log_files(root):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files[str(path)] = {"offset": size, "size": size, "updated_at": int(time.time()), "kind": "zcode"}
        count += 1
    state["zcode_initialized"] = True
    log(f"initialized ZCode baseline at EOF for {count} log files", always_stdout=True)


def baseline_external_files(
    state: dict[str, Any],
    paths: list[Path],
    initialized_key: str,
    kind: str,
    log: Logger,
) -> None:
    files = state.setdefault("files", {})
    count = 0
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files[str(path)] = {"offset": size, "size": size, "updated_at": int(time.time()), "kind": kind}
        count += 1
    state[initialized_key] = True
    log(f"initialized {kind} baseline at EOF for {count} event files", always_stdout=True)


def claude_spool_max_bytes() -> int:
    configured = env_int("CLAUDE_WATCH_SPOOL_MAX_BYTES", DEFAULT_CLAUDE_SPOOL_MAX_BYTES)
    return max(configured, MIN_CLAUDE_SPOOL_MAX_BYTES)


def claude_spool_max_age_seconds() -> int:
    configured = env_int(
        "CLAUDE_WATCH_SPOOL_MAX_AGE_SECONDS",
        DEFAULT_CLAUDE_SPOOL_MAX_AGE_SECONDS,
    )
    return max(configured, MIN_CLAUDE_SPOOL_MAX_AGE_SECONDS)


def claude_drain_grace_seconds() -> int:
    configured = env_int("CLAUDE_WATCH_DRAIN_GRACE_SECONDS", DEFAULT_CLAUDE_DRAIN_GRACE_SECONDS)
    return max(configured, DEFAULT_CLAUDE_DRAIN_GRACE_SECONDS)


def infer_claude_spool_started_at(path: Path, now: int | None = None) -> int:
    """Infer a pre-existing live spool generation's start without postponing TTL."""
    if not regular_file_without_symlink(path):
        return 0
    try:
        metadata = path.lstat()
    except OSError:
        return 0
    if metadata.st_size <= 0:
        return 0
    current_time = int(time.time()) if now is None else max(int(now), 0)
    candidates = [metadata.st_mtime, metadata.st_ctime]
    birth_time = getattr(metadata, "st_birthtime", None)
    if isinstance(birth_time, (int, float)):
        candidates.append(birth_time)
    timestamps = [
        int(value)
        for value in candidates
        if isinstance(value, (int, float)) and value > 0
    ]
    if not timestamps:
        return current_time
    return min(current_time, min(timestamps))


def file_identity(path: Path) -> str:
    if not regular_file_without_symlink(path):
        return ""
    try:
        metadata = path.lstat()
    except OSError:
        return ""
    return f"{metadata.st_dev}:{metadata.st_ino}"


def has_active_delivery_for_path(state: dict[str, Any], path: Path) -> bool:
    expected = str(path)
    return any(
        isinstance(entry, dict)
        and entry.get("log_path") == expected
        and entry.get("status") in {"attempting", "retry_wait"}
        for entry in delivery_attempts_for_state(state).values()
    )


def discard_active_deliveries_for_path(state: dict[str, Any], path: Path) -> int:
    expected = str(path)
    attempts = delivery_attempts_for_state(state)
    related_paths = {expected}
    for candidate_path, rec in state.setdefault("files", {}).items():
        if isinstance(rec, dict) and rec.get("claude_spool_path") == expected:
            related_paths.add(str(candidate_path))
    abandoned = 0
    now = int(time.time())
    for entry in attempts.values():
        if not isinstance(entry, dict) or entry.get("log_path") not in related_paths:
            continue
        if entry.get("status") not in {"attempting", "retry_wait"}:
            continue
        entry.update(
            {
                "status": "exhausted",
                "next_retry_at": None,
                "exhausted_at": now,
                "last_result": "abandoned_source_change",
            }
        )
        abandoned += 1
    if abandoned:
        stats = state.setdefault("delivery_stats", {})
        stats["abandoned_source_changes"] = int(
            stats.get("abandoned_source_changes", 0) or 0
        ) + abandoned
    return abandoned


def initialize_claude_spool(
    state: dict[str, Any],
    spool_path: Path,
    *,
    process_existing: bool,
    log: Logger,
) -> bool:
    """Bind Claude initialization to one path and safely recover drain rotations.

    A missing spool is represented with offset zero so the first future Hook is
    delivered. An already-existing spool at a newly selected path is baselined
    at EOF unless the explicit process-existing option was requested.
    """
    files = state.setdefault("files", {})
    key = str(spool_path)
    previous_path = state.get("claude_initialized")
    path_changed = previous_path != key
    current_missing = key not in files
    changed = False
    now = int(time.time())

    if path_changed or current_missing:
        if path_changed and isinstance(previous_path, str) and previous_path:
            discard_active_deliveries_for_path(state, Path(previous_path))
        size = 0
        if regular_file_without_symlink(spool_path):
            try:
                size = spool_path.stat().st_size
            except OSError:
                size = 0
        offset = 0 if process_existing else size
        files[key] = {
            "offset": offset,
            "size": size,
            "updated_at": now,
            "kind": "Claude Code",
            "file_identity": file_identity(spool_path),
            "claude_spool_started_at": infer_claude_spool_started_at(spool_path, now),
        }
        state["claude_initialized"] = key
        changed = True

        for drain_path in claude_drain_files(spool_path):
            try:
                drain_size = drain_path.stat().st_size
            except OSError:
                continue
            files[str(drain_path)] = {
                "offset": 0 if process_existing else drain_size,
                "size": drain_size,
                "updated_at": now,
                "kind": "Claude Code",
                "claude_drain": True,
                "drain_stable_size": drain_size,
                "drain_stable_since": now,
                "file_identity": file_identity(drain_path),
                "claude_spool_path": key,
            }

        action = "from BOF" if process_existing else "at EOF"
        log(
            f"initialized Claude Code spool {action} for path {spool_path}",
            always_stdout=True,
        )
        return changed

    current = files.get(key)
    actual_identity = file_identity(spool_path)
    if isinstance(current, dict):
        expected_identity = str(current.get("file_identity") or "")
        if actual_identity and expected_identity and actual_identity != expected_identity:
            try:
                replacement_size = spool_path.stat().st_size
            except OSError:
                replacement_size = 0
            current.update(
                {
                    "offset": 0 if process_existing else replacement_size,
                    "size": replacement_size,
                    "updated_at": now,
                    "file_identity": actual_identity,
                    "claude_spool_started_at": infer_claude_spool_started_at(
                        spool_path, now
                    ),
                }
            )
            discard_active_deliveries_for_path(state, spool_path)
            changed = True
            action = "from BOF" if process_existing else "at EOF"
            log(f"Claude Code spool inode changed; baselined replacement {action}: {spool_path}")
        elif actual_identity and not expected_identity:
            current["file_identity"] = actual_identity
            changed = True
        try:
            recorded_start = int(current.get("claude_spool_started_at", 0) or 0)
        except (TypeError, ValueError):
            recorded_start = 0
        inferred_start = infer_claude_spool_started_at(spool_path, now)
        replacement_start: int | None = None
        if recorded_start <= 0 and inferred_start > 0:
            replacement_start = inferred_start
        elif recorded_start > now:
            replacement_start = inferred_start if inferred_start > 0 else now
        elif recorded_start > 0 and inferred_start == 0 and actual_identity:
            # A still-identical regular file is now empty. Do not reset the TTL
            # merely because a missing/inaccessible path could not be inspected.
            replacement_start = 0
        if replacement_start is not None:
            current["claude_spool_started_at"] = replacement_start
            changed = True

    # A drain absent from state means rotation completed on disk but the state
    # checkpoint was interrupted. Its filename records the already-consumed
    # prefix, so only bytes appended to the old inode after rename are replayed.
    recovered_drain = False
    for drain_path in claude_drain_files(spool_path):
        drain_key = str(drain_path)
        if drain_key in files:
            existing = files.get(drain_key)
            if isinstance(existing, dict):
                expected_identity = str(existing.get("file_identity") or "")
                actual_identity = file_identity(drain_path)
                if expected_identity and actual_identity != expected_identity:
                    existing["foreign_replacement"] = True
                    changed = True
            continue
        try:
            drain_size = drain_path.stat().st_size
        except OSError:
            continue
        encoded_offset = claude_drain_baseline_offset(spool_path, drain_path)
        offset = drain_size if encoded_offset is None else min(encoded_offset, drain_size)
        files[drain_key] = {
            "offset": offset,
            "size": drain_size,
            "updated_at": now,
            "kind": "Claude Code",
            "claude_drain": True,
            "drain_stable_size": drain_size,
            "drain_stable_since": now,
            "file_identity": file_identity(drain_path),
            "claude_spool_path": key,
        }
        recovered_drain = True
        changed = True
        log(f"recovered Claude Code spool drain {drain_path.name}")

    if recovered_drain:
        # The current path may already be a fresh file created between rename
        # and the interrupted checkpoint. Start at zero to avoid dropping it;
        # stable event IDs prevent a completed record from being sent twice.
        current = files.get(key)
        if isinstance(current, dict):
            try:
                current_size = (
                    spool_path.stat().st_size
                    if regular_file_without_symlink(spool_path)
                    else 0
                )
            except OSError:
                current_size = 0
            current["offset"] = 0
            current["size"] = current_size
            current["updated_at"] = now
            current["file_identity"] = file_identity(spool_path)
            current["claude_spool_started_at"] = infer_claude_spool_started_at(
                spool_path, now
            )
    return changed


def rotate_consumed_claude_spool(
    spool_path: Path,
    state: dict[str, Any],
    log: Logger,
    checkpoint: Callable[[], None] | None = None,
) -> bool:
    """Rotate a fully consumed size- or age-expired spool without truncation.

    At most one drain is retained. Writers that opened the old inode before the
    rename continue appending to that drain; writers opening the configured path
    afterwards append to the new spool. The drain is still watched until it has
    been fully consumed and stable past the Hook timeout safety window.
    """
    if owned_claude_drain_files(spool_path, state) or not regular_file_without_symlink(spool_path):
        return False
    try:
        metadata = spool_path.lstat()
    except OSError:
        return False
    size = metadata.st_size

    files = state.setdefault("files", {})
    key = str(spool_path)
    rec = files.get(key)
    if not isinstance(rec, dict):
        return False
    try:
        offset = int(rec.get("offset", 0) or 0)
        started_at = int(rec.get("claude_spool_started_at", 0) or 0)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    size_expired = size >= claude_spool_max_bytes()
    age_seconds = max(now - started_at, 0) if started_at > 0 else 0
    age_expired = bool(
        size > 0
        and started_at > 0
        and age_seconds >= claude_spool_max_age_seconds()
    )
    if not size_expired and not age_expired:
        return False
    if offset < size or has_active_delivery_for_path(state, spool_path):
        return False

    drain_name = (
        f"{claude_drain_prefix(spool_path)}{size}-{time.time_ns()}-{secrets.token_hex(4)}"
    )
    drain_path = spool_path.parent / drain_name
    try:
        spool_path.rename(drain_path)
    except OSError as exc:
        log(f"cannot rotate Claude Code spool {spool_path}: {exc}")
        return False

    try:
        descriptor = os.open(spool_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # A Hook won the creation race and already owns the new live spool.
        pass
    except OSError as exc:
        log(f"Claude Code spool rotated; new live spool will be created by the next Hook: {exc}")
    else:
        os.close(descriptor)

    drain_rec = dict(rec)
    drain_rec.update(
        {
            "offset": size,
            "size": size,
            "updated_at": now,
            "kind": "Claude Code",
            "claude_drain": True,
            "drain_stable_size": size,
            "drain_stable_since": now,
            "file_identity": f"{metadata.st_dev}:{metadata.st_ino}",
            "claude_spool_path": key,
        }
    )
    files[str(drain_path)] = drain_rec
    try:
        live_size = spool_path.stat().st_size if regular_file_without_symlink(spool_path) else 0
    except OSError:
        live_size = 0
    files[key] = {
        "offset": 0,
        "size": live_size,
        "updated_at": now,
        "kind": "Claude Code",
        "file_identity": file_identity(spool_path),
        "claude_spool_started_at": now if live_size > 0 else 0,
    }
    for entry in delivery_attempts_for_state(state).values():
        if isinstance(entry, dict) and entry.get("log_path") == key:
            entry["log_path"] = str(drain_path)
    delivery_checkpoint(checkpoint)
    reasons = []
    if size_expired:
        reasons.append(f"size={size}")
    if age_expired:
        reasons.append(f"age={age_seconds}s")
    log(f"rotated fully consumed Claude Code spool ({', '.join(reasons)})")
    return True


def retire_stable_claude_drain(
    drain_path: Path,
    state: dict[str, Any],
    log: Logger,
    checkpoint: Callable[[], None] | None = None,
) -> bool:
    """Remove a consumed drain only after it outlives all configured Hook writers."""
    files = state.setdefault("files", {})
    key = str(drain_path)
    rec = files.get(key)
    if not isinstance(rec, dict) or not rec.get("claude_drain"):
        return False
    spool_value = rec.get("claude_spool_path")
    if not isinstance(spool_value, str) or not spool_value:
        return False
    spool_path = Path(spool_value)
    if parse_claude_drain_path(spool_path, drain_path) is None:
        return False
    if not regular_file_without_symlink(drain_path):
        files.pop(key, None)
        delivery_checkpoint(checkpoint)
        return False
    try:
        metadata = drain_path.lstat()
        offset = int(rec.get("offset", 0) or 0)
    except (OSError, TypeError, ValueError):
        return False
    actual_identity = f"{metadata.st_dev}:{metadata.st_ino}"
    expected_identity = str(rec.get("file_identity") or "")
    if not expected_identity or actual_identity != expected_identity:
        rec["foreign_replacement"] = True
        delivery_checkpoint(checkpoint)
        log(f"ignored replaced Claude Code spool drain {drain_path.name}")
        return False
    size = metadata.st_size
    now = int(time.time())
    previous_size = int(rec.get("drain_stable_size", -1) or 0)
    if offset < size or previous_size != size:
        rec["drain_stable_size"] = size
        rec["drain_stable_since"] = now
        return False
    stable_since = int(rec.get("drain_stable_since", now) or now)
    if now - stable_since < claude_drain_grace_seconds():
        return False

    # No process can newly open the private drain name. After the grace window
    # (six times the installed Hook timeout), all pre-rename writers are gone.
    try:
        current = drain_path.lstat()
        if current.st_dev != metadata.st_dev or current.st_ino != metadata.st_ino or current.st_size != size:
            return False
        drain_path.unlink()
    except OSError as exc:
        log(f"cannot retire Claude Code spool drain {drain_path}: {exc}")
        return False
    files.pop(key, None)
    delivery_checkpoint(checkpoint)
    log(f"retired consumed Claude Code spool drain {drain_path.name}")
    return True


def build_roots(args: argparse.Namespace) -> list[Path]:
    roots = [expand_path(value) for value in (args.sessions_root or [DEFAULT_SESSIONS_ROOT])]
    include_archived = args.include_archived or os.getenv("CODEX_WATCH_INCLUDE_ARCHIVED") in {"1", "true", "True"}
    if include_archived:
        roots.append(expand_path(DEFAULT_ARCHIVED_ROOT))
    return roots


def build_zcode_log_root(args: argparse.Namespace) -> Path:
    return expand_path(getattr(args, "zcode_log_root", None) or os.getenv("ZCODE_WATCH_LOG_ROOT", DEFAULT_ZCODE_LOG_ROOT))


def zcode_watch_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "disable_zcode", False):
        return False
    return os.getenv("ZCODE_WATCH_ENABLED", "1") not in {"0", "false", "False"}


def build_kimi_sessions_root(args: argparse.Namespace) -> Path:
    return expand_path(
        getattr(args, "kimi_sessions_root", None)
        or os.getenv("KIMI_WATCH_SESSIONS_ROOT", DEFAULT_KIMI_SESSIONS_ROOT)
    )


def kimi_watch_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "disable_kimi", False):
        return False
    return env_flag("KIMI_WATCH_ENABLED", True)


def build_grok_sessions_root(args: argparse.Namespace) -> Path:
    return expand_path(
        getattr(args, "grok_sessions_root", None)
        or os.getenv("GROK_WATCH_SESSIONS_ROOT", DEFAULT_GROK_SESSIONS_ROOT)
    )


def grok_watch_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "disable_grok", False):
        return False
    return env_flag("GROK_WATCH_ENABLED", True)


def build_claude_hook_events_file(args: argparse.Namespace) -> Path:
    configured = getattr(args, "claude_hook_events_file", None) or os.getenv("CLAUDE_WATCH_EVENTS_FILE", "")
    if configured:
        return absolute_path_without_symlink_resolution(configured)
    config_root = (
        os.getenv("CODEX_WATCH_CONFIG_DIR")
        or os.getenv("AGENTWATCH_CONFIG_DIR")
        or "~/.codex-watch-notifier"
    )
    return absolute_path_without_symlink_resolution(str(Path(config_root) / CLAUDE_HOOK_EVENTS_FILE_NAME))


def claude_watch_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "disable_claude", False):
        return False
    return env_flag("CLAUDE_WATCH_ENABLED", True)


def build_tool_hook_events_dir(args: argparse.Namespace) -> Path:
    configured = getattr(args, "tool_hook_events_dir", None)
    if configured:
        return absolute_path_without_symlink_resolution(configured)
    config_root = (
        os.getenv("CODEX_WATCH_CONFIG_DIR")
        or os.getenv("AGENTWATCH_CONFIG_DIR")
        or "~/.codex-watch-notifier"
    )
    return absolute_path_without_symlink_resolution(
        str(Path(config_root) / TOOL_HOOK_EVENTS_DIR_NAME)
    )


def tool_hooks_watch_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "disable_tool_hooks", False):
        return False
    return env_flag("PI_WATCH_ENABLED", True) or env_flag("OPENCODE_WATCH_ENABLED", True)


def parse_extra_event_types() -> set[str]:
    raw = os.getenv("CODEX_WATCH_EXTRA_EVENT_TYPES", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def send_test_notification(args: argparse.Namespace, log: Logger) -> int:
    notifier = Notifier(args.dry_run, log)
    event = {
        "event_type": "test",
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "local_time": dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "thread_id": "test",
        "cwd": str(Path.cwd()),
        "rollout_path": "",
        "offset": 0,
        "message": "Codex Watch Notifier test",
    }
    ok = notifier.send("Codex 测试提醒", "这是一条测试提醒。收到它说明通知通道可用。", event)
    return 0 if ok else 1


def send_zcode_test_notification(args: argparse.Namespace, log: Logger) -> int:
    notifier = Notifier(args.dry_run, log)
    event = {
        "event_type": "zcode_test",
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "local_time": dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "session_id": "test",
        "cwd": str(Path.cwd()),
        "message": "ZCode Watch Notifier test",
        "bark_group": os.getenv("ZCODE_BARK_GROUP", "ZCode"),
        "bark_icon": os.getenv("ZCODE_BARK_ICON", ""),
        "ntfy_url": os.getenv("ZCODE_NTFY_URL", ""),
        "ntfy_tags": os.getenv("ZCODE_NTFY_TAGS", "zap,computer"),
    }
    ok = notifier.send("ZCode 测试提醒", "这是一条 ZCode 测试提醒。收到它说明 ZCode 分组和图标配置可用。", event)
    return 0 if ok else 1


def send_external_test_notification(
    args: argparse.Namespace,
    log: Logger,
    tool_name: str,
    event_prefix: str,
) -> int:
    notifier = Notifier(args.dry_run, log)
    env_prefix = event_prefix.upper()
    default_icon = {
        "KIMI": DEFAULT_KIMI_BARK_ICON,
        "GROK": DEFAULT_GROK_BARK_ICON,
        "CLAUDE": DEFAULT_CLAUDE_BARK_ICON,
        "PI": DEFAULT_PI_BARK_ICON,
        "OPENCODE": DEFAULT_OPENCODE_BARK_ICON,
    }.get(env_prefix, "")
    event = {
        "event_type": f"{event_prefix}_test",
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "local_time": dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "session_id": "test",
        "cwd": str(Path.cwd()),
        "message": f"{tool_name} Watch Notifier test",
        "bark_group": os.getenv(f"{env_prefix}_BARK_GROUP", tool_name),
        "bark_icon": os.getenv(f"{env_prefix}_BARK_ICON", default_icon),
        "ntfy_url": os.getenv(f"{env_prefix}_NTFY_URL", ""),
        "ntfy_tags": os.getenv(f"{env_prefix}_NTFY_TAGS", "robot,computer"),
    }
    ok = notifier.send(
        f"{tool_name} 测试提醒",
        f"这是一条 {tool_name} 测试提醒。收到它说明分组和图标配置可用。",
        event,
    )
    return 0 if ok else 1


def print_check(name: str, ok: bool, detail: str = "") -> None:
    status = "OK" if ok else "WARN"
    suffix = f" - {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")


def launch_agent_state() -> str:
    if platform.system() != "Darwin":
        return "not applicable"
    label = os.getenv("CODEX_WATCH_LAUNCH_AGENT_LABEL", "com.xutao.codex-watch-notifier")
    target = f"gui/{os.getuid()}/{label}"
    try:
        completed = subprocess.run(
            ["launchctl", "print", target],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({exc})"
    if completed.returncode != 0:
        return "not loaded"
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state ="):
            return stripped.split("=", 1)[1].strip()
    return "loaded"


def count_paths(paths: list[Path]) -> int:
    return len(paths)


def doctor(args: argparse.Namespace, log: Logger) -> int:
    del log
    env_path = default_env_path()
    state_path = expand_path(args.state)
    log_path = expand_path(args.log)
    roots = build_roots(args)
    zcode_root = build_zcode_log_root(args)
    kimi_root = build_kimi_sessions_root(args)
    grok_root = build_grok_sessions_root(args)
    claude_events_file = build_claude_hook_events_file(args)
    tool_hook_events_dir = build_tool_hook_events_dir(args)
    notifier = Notifier(False, Logger(None))

    print("Codex Watch Notifier doctor")
    print(f"Platform: {platform.system()} {platform.release()}")
    print(f"Config: {env_path}")
    print_check("config file", env_path.exists(), "chmod 600 recommended" if env_path.exists() else "run installer")
    print_check(
        "receiver mode selected",
        notifier.delivery_mode is not None,
        notifier.delivery_mode or "run agentwatch install --delivery bark|agentwatch|both",
    )
    print_check("notification channels", bool(notifier.channels), ",".join(notifier.channels) or "none configured")
    if notifier.delivery_mode in {"bark", "both"}:
        print_check("Bark configured", notifier.bark_configured, "BARK_URL/BARK_KEY")
    if notifier.delivery_mode in {"agentwatch", "both"}:
        print_check(
            "AgentWatch computer login",
            notifier.computer_token is not None,
            f"computer_id={notifier.computer['computer_id']}",
        )
    if notifier.delivery.get("degraded"):
        print_check(
            "receiver configuration complete",
            False,
            "working via " + ",".join(notifier.delivery["effective_channels"])
            + "; missing " + ",".join(notifier.delivery["missing_channels"]),
        )
    legacy_ntfy = any(
        os.getenv(name)
        for name in (
            "NTFY_URL",
            "NTFY_TOKEN",
            "CODEX_NTFY_URL",
            "ZCODE_NTFY_URL",
            "KIMI_NTFY_URL",
            "GROK_NTFY_URL",
            "CLAUDE_NTFY_URL",
        )
    )
    if legacy_ntfy:
        print_check(
            "legacy ntfy configuration ignored",
            False,
            "private delivery publishes only through the account-bound /publish API; no duplicate is sent",
        )
    subagent_policy = "enabled" if notify_subagents_enabled() else "main sessions only"
    print(f"Codex subagent notifications: {subagent_policy}")
    print_check("Codex sessions root", any(root.exists() for root in roots), ", ".join(str(root) for root in roots))
    print_check("Codex rollout files", count_paths(rollout_files(roots)) > 0, f"{count_paths(rollout_files(roots))} file(s)")
    print_check("ZCode watch enabled", zcode_watch_enabled(args), f"root={zcode_root}")
    if zcode_watch_enabled(args):
        print_check("ZCode log root", zcode_root.exists(), str(zcode_root))
        print_check("ZCode log files", count_paths(zcode_log_files(zcode_root)) > 0, f"{count_paths(zcode_log_files(zcode_root))} file(s)")
    kimi_policy = "enabled" if env_flag("KIMI_WATCH_NOTIFY_SUBAGENTS", False) else "main agent only"
    print(f"Kimi subagent notifications: {kimi_policy}")
    print_check("Kimi watch enabled", kimi_watch_enabled(args), f"root={kimi_root}")
    if kimi_watch_enabled(args):
        print_check("Kimi sessions root", kimi_root.exists(), str(kimi_root))
        print_check("Kimi wire files", count_paths(kimi_wire_files(kimi_root)) > 0, f"{count_paths(kimi_wire_files(kimi_root))} file(s)")
    grok_policy = "enabled" if env_flag("GROK_WATCH_NOTIFY_SUBAGENTS", False) else "main sessions only"
    print(f"Grok child-session notifications: {grok_policy}")
    print_check("Grok watch enabled", grok_watch_enabled(args), f"root={grok_root}")
    if grok_watch_enabled(args):
        print_check("Grok sessions root", grok_root.exists(), str(grok_root))
        print_check("Grok event files", count_paths(grok_event_files(grok_root)) > 0, f"{count_paths(grok_event_files(grok_root))} file(s)")
    print_check("Claude Code watch enabled", claude_watch_enabled(args), f"events={claude_events_file}")
    if claude_watch_enabled(args):
        print_check(
            "Claude Code hook event spool",
            regular_file_without_symlink(claude_events_file),
            str(claude_events_file),
        )
    print_check(
        "Pi/OpenCode hook queue enabled",
        tool_hooks_watch_enabled(args),
        f"events={tool_hook_events_dir}",
    )
    if tool_hooks_watch_enabled(args):
        queue_safe = not path_has_link_component(tool_hook_events_dir)
        print_check("Pi/OpenCode hook queue path", queue_safe, str(tool_hook_events_dir))
    print_check("state file", state_path.exists(), str(state_path))
    state_file_valid = True
    delivery_state: dict[str, Any] = {}
    if state_path.exists():
        try:
            delivery_state = load_state(state_path)
        except StateFileError as exc:
            state_file_valid = False
            print_check("state data valid", False, str(exc))
            delivery_state = {}
        else:
            print_check("state data valid", True, "preserved offsets and delivery state")
    delivery_entries = delivery_state.get("delivery_attempts", {})
    if not isinstance(delivery_entries, dict):
        delivery_entries = {}
    waiting_count = sum(
        1
        for entry in delivery_entries.values()
        if isinstance(entry, dict) and entry.get("status") in {"attempting", "retry_wait"}
    )
    exhausted_count = sum(
        1
        for entry in delivery_entries.values()
        if isinstance(entry, dict) and entry.get("status") == "exhausted"
    )
    print(
        "Delivery policy: "
        f"max_attempts={delivery_max_attempts()} retry_delay={delivery_retry_delay_seconds()}s "
        "(hard cap prevents repeated alerts)"
    )
    print_check("pending delivery retries", waiting_count == 0, f"{waiting_count} waiting")
    print_check("exhausted deliveries", exhausted_count == 0, f"{exhausted_count} recorded")
    print_check("log file", log_path.exists(), str(log_path))
    if platform.system() == "Darwin":
        print_check("LaunchAgent", launch_agent_state() == "running", launch_agent_state())
    elif platform.system() == "Linux":
        print_check("background service", False, "use a systemd user service")
    elif platform.system() == "Windows":
        print_check("background service", False, "use Task Scheduler or a startup shortcut")
    else:
        print_check("background service", False, "manual setup required")
    print(f"Privacy: workspace={include_workspace_in_notifications()} message={include_message_excerpt_in_notifications()} max_chars={notification_body_max_chars()}")

    if log_path.exists():
        print("\nRecent notifier log:")
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
            for line in lines:
                print(line)
        except OSError as exc:
            print(f"cannot read log: {exc}")
    return 0 if state_file_valid else 1


def replay_file(args: argparse.Namespace, log: Logger) -> int:
    path = expand_path(args.replay_file)
    state = {"version": 1, "initialized": True, "files": {str(path): {"offset": 0}}, "sent": {}}
    notifier = Notifier(args.dry_run, log)
    sent = process_file(path, state, notifier, parse_extra_event_types(), log)
    log(f"replay finished: {sent} notification(s)", always_stdout=True)
    return 0


def run_watcher(args: argparse.Namespace, log: Logger, state_path: Path) -> int:
    roots = build_roots(args)
    zcode_enabled = zcode_watch_enabled(args)
    zcode_root = build_zcode_log_root(args)
    kimi_enabled = kimi_watch_enabled(args)
    kimi_root = build_kimi_sessions_root(args)
    grok_enabled = grok_watch_enabled(args)
    grok_root = build_grok_sessions_root(args)
    claude_enabled = claude_watch_enabled(args)
    claude_events_file = build_claude_hook_events_file(args)
    tool_hooks_enabled = tool_hooks_watch_enabled(args)
    tool_hook_events_dir = build_tool_hook_events_dir(args)
    last_state_error = ""
    while True:
        try:
            state = load_state(state_path)
            break
        except StateFileError as exc:
            message = str(exc)
            if message != last_state_error:
                log(
                    message
                    + "; watcher is paused without changing offsets or delivery state",
                    always_stdout=True,
                )
                last_state_error = message
            if args.once:
                return 78
            time.sleep(max(float(args.poll_interval), 30.0))

    def checkpoint() -> None:
        save_state(state_path, state)

    did_baseline = False
    if not state.get("initialized") and not args.process_existing:
        baseline_existing_files(state, roots, log)
        did_baseline = True
    else:
        state["initialized"] = True
    if zcode_enabled:
        if not state.get("zcode_initialized") and not args.process_existing:
            baseline_existing_zcode_files(state, zcode_root, log)
            did_baseline = True
        else:
            state["zcode_initialized"] = True
    if kimi_enabled:
        if not state.get("kimi_initialized") and not args.process_existing:
            baseline_external_files(
                state,
                kimi_wire_files(kimi_root, include_subagents=True),
                "kimi_initialized",
                "Kimi Code",
                log,
            )
            did_baseline = True
        else:
            state["kimi_initialized"] = True
    if grok_enabled:
        if not state.get("grok_initialized") and not args.process_existing:
            baseline_external_files(
                state,
                grok_event_files(grok_root),
                "grok_initialized",
                "Grok Build",
                log,
            )
            did_baseline = True
        else:
            state["grok_initialized"] = True
    if claude_enabled:
        claude_needs_baseline = bool(
            state.get("claude_initialized") != str(claude_events_file)
            or str(claude_events_file) not in state.setdefault("files", {})
        )
        initialize_claude_spool(
            state,
            claude_events_file,
            process_existing=args.process_existing,
            log=log,
        )
        if claude_needs_baseline and not args.process_existing:
            did_baseline = True
    if tool_hooks_enabled:
        tool_hooks_need_baseline = state.get("tool_hooks_initialized") != str(
            tool_hook_events_dir
        )
        if initialize_tool_hook_events(
            state,
            tool_hook_events_dir,
            process_existing=args.process_existing,
            log=log,
        ) and tool_hooks_need_baseline and not args.process_existing:
            did_baseline = True
    if did_baseline:
        save_state(state_path, state)

    notifier = Notifier(args.dry_run, log)
    extra_types = parse_extra_event_types()
    log(f"watching {', '.join(str(root) for root in roots)} with channels={notifier.channels}", always_stdout=True)
    if zcode_enabled:
        log(f"watching ZCode {zcode_root} with channels={notifier.channels}", always_stdout=True)
    if kimi_enabled:
        log(f"watching Kimi Code {kimi_root} with channels={notifier.channels}", always_stdout=True)
    if grok_enabled:
        log(f"watching Grok Build {grok_root} with channels={notifier.channels}", always_stdout=True)
    if claude_enabled:
        log(f"watching Claude Code hook events {claude_events_file} with channels={notifier.channels}", always_stdout=True)
    if tool_hooks_enabled:
        log(
            f"watching Pi/OpenCode hook events {tool_hook_events_dir} with channels={notifier.channels}",
            always_stdout=True,
        )

    while True:
        for path in rollout_files(roots):
            if str(path) not in state.setdefault("files", {}):
                state["files"][str(path)] = {
                    "offset": 0,
                    "head_hash": file_head_hash(path),
                    "new_file_at": int(time.time()),
                }
                log(f"new rollout discovered: {path}")
            process_file(path, state, notifier, extra_types, log, checkpoint)
        if zcode_enabled:
            for path in zcode_log_files(zcode_root):
                if str(path) not in state.setdefault("files", {}):
                    state["files"][str(path)] = {"offset": 0, "new_file_at": int(time.time()), "kind": "zcode"}
                    log(f"new ZCode log discovered: {path}")
                process_zcode_file(path, state, notifier, log, checkpoint)
        if kimi_enabled:
            for path in kimi_wire_files(kimi_root):
                if str(path) not in state.setdefault("files", {}):
                    state["files"][str(path)] = {"offset": 0, "new_file_at": int(time.time()), "kind": "Kimi Code"}
                    log(f"new Kimi Code session discovered: {path}")
                process_external_file(
                    path,
                    state,
                    notifier,
                    log,
                    "Kimi Code",
                    trigger_from_kimi_record,
                    checkpoint,
                )
        if grok_enabled:
            for path in grok_event_files(grok_root):
                if str(path) not in state.setdefault("files", {}):
                    state["files"][str(path)] = {"offset": 0, "new_file_at": int(time.time()), "kind": "Grok Build"}
                    log(f"new Grok Build session discovered: {path}")
                process_external_file(
                    path,
                    state,
                    notifier,
                    log,
                    "Grok Build",
                    trigger_from_grok_record,
                    checkpoint,
                )
        if claude_enabled:
            if initialize_claude_spool(
                state,
                claude_events_file,
                process_existing=args.process_existing,
                log=log,
            ):
                checkpoint()
            drain_paths = claude_drain_files(claude_events_file)
            for path in drain_paths + claude_hook_event_files(claude_events_file):
                if path in drain_paths:
                    drain_rec = state.setdefault("files", {}).get(str(path))
                    if not isinstance(drain_rec, dict) or drain_rec.get("foreign_replacement"):
                        continue
                process_external_file(
                    path,
                    state,
                    notifier,
                    log,
                    "Claude Code",
                    trigger_from_claude_hook_record,
                    checkpoint,
                )
                if path in drain_paths:
                    retire_stable_claude_drain(path, state, log, checkpoint)
            rotate_consumed_claude_spool(claude_events_file, state, log, checkpoint)
        if tool_hooks_enabled:
            if initialize_tool_hook_events(
                state,
                tool_hook_events_dir,
                process_existing=args.process_existing,
                log=log,
            ):
                checkpoint()
            for path in tool_hook_event_files(tool_hook_events_dir):
                process_tool_hook_event_file(path, state, notifier, log, checkpoint)
                # Each hook record is a separate queue item. Preserve strict
                # FIFO semantics: while the oldest item is waiting for its
                # bounded retry (or has been replaced unexpectedly), do not
                # let a newer completion jump ahead of it.
                queue_record = state.setdefault("files", {}).get(str(path))
                if isinstance(queue_record, dict):
                    try:
                        queue_size = path.lstat().st_size
                        queue_offset = int(queue_record.get("offset", 0) or 0)
                    except (OSError, TypeError, ValueError):
                        break
                    if (
                        queue_record.get("foreign_replacement")
                        or has_active_delivery_for_path(state, path)
                        or queue_offset < queue_size
                    ):
                        break
        save_state(state_path, state)
        if args.once:
            return 0
        time.sleep(max(args.poll_interval, 0.5))


def main() -> int:
    env_path = default_env_path()
    last_config_error = ""
    one_shot_flags = {
        "--once",
        "--doctor",
        "--replay-file",
        "--test",
        "--test-zcode",
        "--test-kimi",
        "--test-grok",
        "--test-claude",
        "--test-pi",
        "--test-opencode",
    }
    while True:
        try:
            load_env_file(env_path)
            break
        except ConfigFileError as exc:
            message = str(exc)
            if message != last_config_error:
                print(
                    message
                    + "; watcher is paused without using fallback delivery configuration",
                    file=sys.stderr,
                )
                last_config_error = message
            if any(flag in sys.argv[1:] for flag in one_shot_flags):
                return 78
            time.sleep(30.0)
    parser = argparse.ArgumentParser(description="Notify when Codex rollout sessions complete or stop.")
    parser.add_argument("--sessions-root", action="append", help="Root containing rollout-*.jsonl files.")
    parser.add_argument("--state", default=os.getenv("CODEX_WATCH_STATE", DEFAULT_STATE), help="State JSON path.")
    parser.add_argument("--log", default=os.getenv("CODEX_WATCH_LOG", DEFAULT_LOG), help="Log file path.")
    parser.add_argument("--poll-interval", type=float, default=float(os.getenv("CODEX_WATCH_POLL_INTERVAL", "2")))
    parser.add_argument("--once", action="store_true", help="Process currently appended data once and exit.")
    parser.add_argument("--process-existing", action="store_true", help="Do not baseline old files on first run.")
    parser.add_argument("--include-archived", action="store_true", help="Also scan ~/.codex/archived_sessions.")
    parser.add_argument("--zcode-log-root", help="Root containing ZCode zcode-*.jsonl log files.")
    parser.add_argument("--disable-zcode", action="store_true", help="Disable ZCode log notifications.")
    parser.add_argument("--kimi-sessions-root", help="Root containing Kimi Code session wire.jsonl files.")
    parser.add_argument("--disable-kimi", action="store_true", help="Disable Kimi Code notifications.")
    parser.add_argument("--grok-sessions-root", help="Root containing Grok Build session events.jsonl files.")
    parser.add_argument("--disable-grok", action="store_true", help="Disable Grok Build notifications.")
    parser.add_argument("--claude-hook-events-file", help="JSONL spool written by the official Claude Code hooks.")
    parser.add_argument("--disable-claude", action="store_true", help="Disable Claude Code hook notifications.")
    parser.add_argument("--tool-hook-events-dir", help="Private event queue written by Pi and OpenCode integrations.")
    parser.add_argument("--disable-tool-hooks", action="store_true", help="Disable Pi and OpenCode integration notifications.")
    parser.add_argument("--dry-run", action="store_true", help="Print notifications instead of sending them.")
    parser.add_argument("--verbose", action="store_true", help="Also print log lines to stdout.")
    parser.add_argument("--test", action="store_true", help="Send one test notification and exit.")
    parser.add_argument("--test-zcode", action="store_true", help="Send one ZCode test notification and exit.")
    parser.add_argument("--test-kimi", action="store_true", help="Send one Kimi Code test notification and exit.")
    parser.add_argument("--test-grok", action="store_true", help="Send one Grok Build test notification and exit.")
    parser.add_argument("--test-claude", action="store_true", help="Send one Claude Code test notification and exit.")
    parser.add_argument("--test-pi", action="store_true", help="Send one Pi Agent test notification and exit.")
    parser.add_argument("--test-opencode", action="store_true", help="Send one OpenCode test notification and exit.")
    parser.add_argument("--doctor", action="store_true", help="Check configuration, log roots, and LaunchAgent status.")
    parser.add_argument("--replay-file", help="Replay one rollout file from the beginning and exit.")
    args = parser.parse_args()

    log_path = None if args.dry_run else expand_path(args.log)
    log = Logger(log_path, verbose=args.verbose or args.dry_run)

    if args.test:
        return send_test_notification(args, log)
    if args.test_zcode:
        return send_zcode_test_notification(args, log)
    if args.test_kimi:
        return send_external_test_notification(args, log, "Kimi Code", "kimi")
    if args.test_grok:
        return send_external_test_notification(args, log, "Grok Build", "grok")
    if args.test_claude:
        return send_external_test_notification(args, log, "Claude Code", "claude")
    if args.test_pi:
        return send_external_test_notification(args, log, "Pi Agent", "pi")
    if args.test_opencode:
        return send_external_test_notification(args, log, "OpenCode", "opencode")
    if args.doctor:
        return doctor(args, log)
    # Keep these paths lexical so the safety checks can still see a configured
    # symlink or junction instead of resolving it to an apparently safe target.
    state_path = lexical_absolute_path(args.state)
    lock_path = lexical_absolute_path(
        os.getenv("CODEX_WATCH_LOCK", str(state_path) + ".lock")
    )
    try:
        with SingleInstanceLock(lock_path):
            if args.replay_file:
                return replay_file(args, log)
            return run_watcher(args, log, state_path)
    except InstanceLockBusy:
        log(f"another notifier already holds {lock_path}; exiting without sending", always_stdout=True)
        return 75


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        raise SystemExit(130)
