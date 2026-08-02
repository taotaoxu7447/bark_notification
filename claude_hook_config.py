#!/usr/bin/env python3
"""Safe, idempotent Claude Code hook configuration for AgentWatch.

The notification hook is deliberately installed in the user settings scope.
It only launches the local AgentWatch ingestor; network delivery stays in the
already single-instanced background watcher.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

from agentwatch_core import AgentWatchError, atomic_write, path_has_link_component


CLAUDE_HOOK_EVENTS = ("Stop", "StopFailure")
CLAUDE_SETTINGS_FILE = "settings.json"
CLAUDE_SETTINGS_BACKUP_FILE = "claude-settings.pre-agentwatch.json"
CLAUDE_HOOK_MANAGED_ID = "io.github.taotaoxu7447.agentwatch.claude-hook.v1"


def claude_config_dir(
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    values = environment if environment is not None else os.environ
    user_home = home or Path.home()
    configured = str(values.get("CLAUDE_CONFIG_DIR") or "").strip()
    if not configured:
        return user_home / ".claude"
    expanded = Path(os.path.expandvars(os.path.expanduser(configured)))
    if not expanded.is_absolute():
        expanded = user_home / expanded
    return Path(os.path.abspath(expanded))


def claude_settings_path(
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    return claude_config_dir(home, environment) / CLAUDE_SETTINGS_FILE


def claude_managed_settings_path(
    system_name: str,
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    values = environment if environment is not None else os.environ
    if system_name == "Darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if system_name == "Linux":
        return Path("/etc/claude-code/managed-settings.json")
    if system_name == "Windows":
        program_files = str(values.get("ProgramFiles") or r"C:\Program Files")
        return Path(program_files) / "ClaudeCode" / "managed-settings.json"
    return None


def build_claude_hook_handler(
    python_executable: Path | str,
    agentwatch_script: Path,
    events_file: Path,
) -> dict[str, Any]:
    return {
        "type": "command",
        "command": os.path.abspath(str(python_executable)),
        "args": [
            os.path.abspath(str(agentwatch_script)),
            "claude-hook",
            "--events-file",
            os.path.abspath(str(events_file)),
            "--managed-hook-id",
            CLAUDE_HOOK_MANAGED_ID,
        ],
        "timeout": 5,
    }


def _normalized_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def _is_owned_handler(handler: object, desired: dict[str, Any]) -> bool:
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    args = handler.get("args")
    desired_args = desired["args"]
    if not isinstance(args, list) or len(args) < 2:
        return False
    for index, value in enumerate(args[:-1]):
        if value == "--managed-hook-id" and args[index + 1] == CLAUDE_HOOK_MANAGED_ID:
            return True
    return (
        _normalized_path(args[0]) == _normalized_path(desired_args[0])
        and args[1] == "claude-hook"
    )


def _handler_is_current(handler: object, desired: dict[str, Any]) -> bool:
    if not isinstance(handler, dict):
        return False
    return handler == desired


def _reject_symlink_path(path: Path, boundary: Path) -> None:
    target = Path(os.path.abspath(path))
    root = Path(os.path.abspath(boundary))
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise AgentWatchError(f"unsafe Claude settings path outside {root}: {target}") from None
    if path_has_link_component(target):
        raise AgentWatchError(f"refusing symlink or junction in Claude settings path: {target}")


def _load_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return {}, None
    if path.is_symlink() or not path.is_file():
        raise AgentWatchError(f"{label} must be a regular file")
    try:
        raw = path.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentWatchError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise AgentWatchError(f"{label} must contain one JSON object")
    return parsed, raw


def _merge_managed_settings(
    current: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge the managed keys relevant to user Hook availability.

    Claude merges the base managed file first and then alphabetically sorted
    drop-ins. Scalars are replaced; array values are concatenated and
    de-duplicated. We intentionally retain only keys needed for diagnostics.
    """
    merged = copy.deepcopy(current)
    relevant_keys = (
        "disableAllHooks",
        "allowManagedHooksOnly",
        "strictPluginOnlyCustomization",
        "policyHelper",
    )
    for name in relevant_keys:
        if name not in incoming:
            continue
        value = copy.deepcopy(incoming[name])
        existing = merged.get(name)
        if isinstance(existing, list) and isinstance(value, list):
            combined = copy.deepcopy(existing)
            for item in value:
                if item not in combined:
                    combined.append(copy.deepcopy(item))
            merged[name] = combined
        else:
            merged[name] = value
    return merged


def _load_file_managed_settings(base_path: Path) -> tuple[dict[str, Any], list[str]]:
    merged: dict[str, Any] = {}
    sources: list[str] = []
    if os.path.lexists(base_path):
        payload, _raw = _load_json_object(base_path, "Claude managed settings")
        merged = _merge_managed_settings(merged, payload)
        sources.append(str(base_path))

    drop_in_dir = base_path.parent / "managed-settings.d"
    if os.path.lexists(drop_in_dir):
        if drop_in_dir.is_symlink() or not drop_in_dir.is_dir():
            raise AgentWatchError("Claude managed settings drop-in path must be a regular directory")
        try:
            candidates = sorted(drop_in_dir.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise AgentWatchError("Claude managed settings drop-in directory cannot be read") from exc
        for candidate in candidates:
            if candidate.name.startswith(".") or candidate.suffix != ".json":
                continue
            payload, _raw = _load_json_object(
                candidate,
                f"Claude managed settings drop-in {candidate.name}",
            )
            merged = _merge_managed_settings(merged, payload)
            sources.append(str(candidate))
    return merged, sources


def _validated_event_groups(hooks: dict[str, Any], event_name: str) -> list[dict[str, Any]]:
    raw_groups = hooks.get(event_name, [])
    if not isinstance(raw_groups, list):
        raise AgentWatchError(f"Claude hooks.{event_name} must be a JSON array")
    groups: list[dict[str, Any]] = []
    for group in raw_groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise AgentWatchError(f"Claude hooks.{event_name} contains an invalid hook group")
        groups.append(group)
    return groups


def _rewrite_hooks(
    settings: dict[str, Any],
    desired: dict[str, Any],
    *,
    enabled: bool,
) -> dict[str, Any]:
    updated = copy.deepcopy(settings)
    existing_hooks = updated.get("hooks")
    if existing_hooks is None:
        hooks: dict[str, Any] = {}
    elif isinstance(existing_hooks, dict):
        hooks = existing_hooks
    else:
        raise AgentWatchError("Claude settings hooks must be a JSON object")

    for event_name in CLAUDE_HOOK_EVENTS:
        groups = _validated_event_groups(hooks, event_name)
        rewritten_groups: list[dict[str, Any]] = []
        for group in groups:
            handlers = group["hooks"]
            retained = [handler for handler in handlers if not _is_owned_handler(handler, desired)]
            if retained:
                rewritten = copy.deepcopy(group)
                rewritten["hooks"] = retained
                rewritten_groups.append(rewritten)
            elif len(retained) == len(handlers):
                # An already-empty third-party group is preserved byte-for-byte
                # at the data-model level instead of being cleaned up by us.
                rewritten_groups.append(copy.deepcopy(group))
        if enabled:
            rewritten_groups.append({"hooks": [copy.deepcopy(desired)]})
        if rewritten_groups:
            hooks[event_name] = rewritten_groups
        else:
            hooks.pop(event_name, None)

    if hooks:
        updated["hooks"] = hooks
    else:
        updated.pop("hooks", None)
    return updated


def configure_claude_hooks(
    settings_path: Path,
    desired_handler: dict[str, Any],
    backup_path: Path,
    *,
    enabled: bool = True,
) -> bool:
    """Merge or remove AgentWatch hooks while preserving all unrelated settings."""
    changed = preflight_claude_hooks(
        settings_path,
        desired_handler,
        backup_path,
        enabled=enabled,
    )
    if not changed:
        return False

    current, raw = _load_json_object(settings_path, "Claude user settings")
    updated = _rewrite_hooks(current, desired_handler, enabled=enabled)
    if raw is not None and not backup_path.exists():
        _reject_symlink_path(backup_path, backup_path.parent.parent)
        atomic_write(backup_path, raw, mode=0o600)

    rendered = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write(settings_path, rendered, mode=0o600)
    return True


def preflight_claude_hooks(
    settings_path: Path,
    desired_handler: dict[str, Any],
    backup_path: Path,
    *,
    enabled: bool = True,
) -> bool:
    """Validate a future hook merge without modifying settings or backups."""
    config_root = settings_path.parent
    _reject_symlink_path(settings_path, config_root.parent)
    current, raw = _load_json_object(settings_path, "Claude user settings")
    updated = _rewrite_hooks(current, desired_handler, enabled=enabled)
    if updated == current:
        return False
    if os.path.lexists(backup_path) and (
        backup_path.is_symlink() or not backup_path.is_file()
    ):
        raise AgentWatchError("Claude settings backup must be a regular file")
    if raw is not None and not backup_path.exists():
        _reject_symlink_path(backup_path, backup_path.parent.parent)
    return True


def inspect_claude_hooks(
    settings_path: Path,
    desired_handler: dict[str, Any],
    *,
    managed_settings_path: Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "settings_path": str(settings_path),
        "configured": False,
        "active": False,
        "events": {event_name: False for event_name in CLAUDE_HOOK_EVENTS},
        "disable_all_hooks": False,
        "managed_policy_blocked": False,
        "managed_policy_sources": [],
        "managed_policy_dynamic": False,
        "policy_scope": "user_and_file_managed_settings",
        "runtime_policy_verification": "run /status inside Claude Code",
    }
    try:
        settings, _raw = _load_json_object(settings_path, "Claude user settings")
        hooks = settings.get("hooks", {})
        if hooks is None:
            hooks = {}
        if not isinstance(hooks, dict):
            raise AgentWatchError("Claude settings hooks must be a JSON object")
        for event_name in CLAUDE_HOOK_EVENTS:
            groups = _validated_event_groups(hooks, event_name)
            result["events"][event_name] = any(
                _handler_is_current(handler, desired_handler)
                for group in groups
                for handler in group["hooks"]
            )
        result["disable_all_hooks"] = settings.get("disableAllHooks") is True
    except AgentWatchError as exc:
        result["error"] = str(exc)
        return result

    if managed_settings_path is not None:
        try:
            managed, managed_sources = _load_file_managed_settings(managed_settings_path)
            result["managed_policy_sources"] = managed_sources
            strict_customization = managed.get("strictPluginOnlyCustomization")
            strict_blocks_hooks = bool(
                strict_customization is True
                or (
                    isinstance(strict_customization, list)
                    and "hooks" in strict_customization
                )
            )
            result["managed_policy_blocked"] = bool(
                managed.get("disableAllHooks") is True
                or managed.get("allowManagedHooksOnly") is True
                or strict_blocks_hooks
            )
            result["managed_policy_dynamic"] = "policyHelper" in managed
        except AgentWatchError as exc:
            result["managed_policy_error"] = str(exc)

    result["configured"] = all(result["events"].values())
    result["active"] = bool(
        result["configured"]
        and not result["disable_all_hooks"]
        and not result["managed_policy_blocked"]
        and not result["managed_policy_dynamic"]
        and "managed_policy_error" not in result
    )
    return result
