#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import urllib.parse
import urllib.request


DEFAULT_STATE = "~/.codex-watch-notifier/state.json"
DEFAULT_LOG = "~/.codex-watch-notifier/notifier.log"
DEFAULT_SESSIONS_ROOT = "~/.codex/sessions"
DEFAULT_ARCHIVED_ROOT = "~/.codex/archived_sessions"
DEFAULT_SESSION_INDEX = "~/.codex/session_index.jsonl"
DEFAULT_ZCODE_LOG_ROOT = "~/.zcode/cli/log"
DEFAULT_MAX_EVENT_AGE_SECONDS = 3600
MAX_SENT_KEYS = 3000


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def utc_to_local(value: str | None) -> str:
    if not value:
        return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return value


def compact(text: str, limit: int = 900) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "..."


def parse_timestamp(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000_000:
            timestamp /= 1_000_000
        elif timestamp > 10_000_000_000:
            timestamp /= 1_000
        return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)

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


class Notifier:
    def __init__(self, dry_run: bool, log: Logger) -> None:
        self.dry_run = dry_run
        self.log = log
        self.channels = self._discover_channels()

    def _discover_channels(self) -> list[str]:
        channels: list[str] = []
        if os.getenv("BARK_URL") or os.getenv("BARK_KEY"):
            channels.append("bark")
        if os.getenv("SERVERCHAN_SENDKEY"):
            channels.append("serverchan")
        if os.getenv("CODEX_NOTIFY_WEBHOOK_URL"):
            channels.append("generic_webhook")
        if os.getenv("WECOM_WEBHOOK_URL") or os.getenv("WECHAT_WORK_WEBHOOK"):
            channels.append("wecom")
        if os.getenv("CODEX_NOTIFY_COMMAND"):
            channels.append("command")
        if os.getenv("CODEX_WATCH_MACOS_NOTIFICATION", "1") not in {"0", "false", "False"}:
            channels.append("macos")
        if self.dry_run and not channels:
            channels.append("dry_run")
        return channels

    def send(self, title: str, body: str, event: dict[str, Any]) -> bool:
        if self.dry_run:
            print("\n--- dry-run notification ---", flush=True)
            print(title, flush=True)
            print(body, flush=True)
            print(json.dumps(event, ensure_ascii=False, indent=2), flush=True)
            print("--- end notification ---\n", flush=True)
            return True

        if not self.channels:
            self.log("no notification channel configured; set SERVERCHAN_SENDKEY or CODEX_NOTIFY_WEBHOOK_URL")
            return False

        ok = False
        for channel in self.channels:
            try:
                if channel == "bark":
                    ok = self._send_bark(title, body, event) or ok
                elif channel == "serverchan":
                    ok = self._send_serverchan(title, body) or ok
                elif channel == "generic_webhook":
                    ok = self._send_generic_webhook(title, body, event) or ok
                elif channel == "wecom":
                    ok = self._send_wecom(title, body) or ok
                elif channel == "command":
                    ok = self._send_command(title, body, event) or ok
                elif channel == "macos":
                    ok = self._send_macos(title, body) or ok
            except Exception as exc:  # noqa: BLE001 - log all channel failures and keep other channels alive.
                self.log(f"channel {channel} failed: {exc}; retrying once")
                time.sleep(1)
                try:
                    if channel == "bark":
                        ok = self._send_bark(title, body, event) or ok
                    elif channel == "serverchan":
                        ok = self._send_serverchan(title, body) or ok
                    elif channel == "generic_webhook":
                        ok = self._send_generic_webhook(title, body, event) or ok
                    elif channel == "wecom":
                        ok = self._send_wecom(title, body) or ok
                    elif channel == "command":
                        ok = self._send_command(title, body, event) or ok
                    elif channel == "macos":
                        ok = self._send_macos(title, body) or ok
                except Exception as retry_exc:  # noqa: BLE001
                    self.log(f"channel {channel} retry failed: {retry_exc}")
        return ok

    def _http_post(self, url: str, payload: bytes, content_type: str) -> bool:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": content_type, "User-Agent": "codex-watch-notifier/1.0"},
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

    def _send_serverchan(self, title: str, body: str) -> bool:
        sendkey = os.environ["SERVERCHAN_SENDKEY"].strip()
        url = f"https://sctapi.ftqq.com/{urllib.parse.quote(sendkey)}.send"
        data = urllib.parse.urlencode({"title": title, "text": title, "desp": body}).encode("utf-8")
        return self._http_post(url, data, "application/x-www-form-urlencoded")

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
        icon = (
            str(event.get("bark_icon") or "").strip()
            or os.getenv("CODEX_BARK_ICON")
            or os.getenv("BARK_ICON")
            or ""
        ).strip()
        if icon:
            payload["icon"] = icon
        data = urllib.parse.urlencode(payload).encode("utf-8")
        return self._http_post(url, data, "application/x-www-form-urlencoded")

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
    if not path.exists():
        return {"version": 1, "initialized": False, "files": {}, "sent": {}}
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    state.setdefault("version", 1)
    state.setdefault("initialized", False)
    state.setdefault("files", {})
    state.setdefault("sent", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sent = state.get("sent", {})
    if len(sent) > MAX_SENT_KEYS:
        state["sent"] = dict(sorted(sent.items(), key=lambda item: item[1])[-MAX_SENT_KEYS:])
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


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
                if record.get("type") == "session_meta":
                    payload = record.get("payload") or {}
                    return {
                        "thread_id": payload.get("id") or path.stem,
                        "cwd": payload.get("cwd") or "",
                        "source": payload.get("source") or "",
                        "originator": payload.get("originator") or "",
                        "thread_source": payload.get("thread_source") or "",
                    }
    except OSError:
        pass
    return {"thread_id": path.stem, "cwd": "", "source": "", "originator": "", "thread_source": ""}


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
                if record.get("id") == thread_id:
                    title = str(record.get("thread_name") or "")
    except OSError:
        return ""
    return title.strip()


def classify_task_complete(message: str) -> tuple[str, str]:
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
    return "已停下", "Codex 已结束本轮；当前版本没有写出更细的官方状态"


def trigger_from_record(path: Path, offset: int, record: dict[str, Any], extra_types: set[str]) -> dict[str, Any] | None:
    if record.get("type") != "event_msg":
        return None

    payload = record.get("payload") or {}
    event_type = payload.get("type")
    if event_type not in {"task_complete", "turn_aborted"} and event_type not in extra_types:
        return None

    meta = load_session_meta(path)
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
        f"目录: {cwd}",
    ]
    if message:
        body_parts.extend(["", compact(message, 1100)])
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


def trigger_from_zcode_record(path: Path, offset: int, record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("message") != "ZCode Protocol background turn completed":
        return None

    context = record.get("context") or {}
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
    }

    body_parts = [
        "状态: 完成",
        "判断: ZCode 本轮已结束",
        f"会话: {display_name}",
        f"时间: {local_time}",
        f"目录: {workspace or '(unknown workspace)'}",
    ]
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


def process_file(
    path: Path,
    state: dict[str, Any],
    notifier: Notifier,
    extra_types: set[str],
    log: Logger,
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
    if previous_head_hash and current_head_hash and current_head_hash != previous_head_hash and offset > 0:
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
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    rec["offset"] = handle.tell()
                    continue

                event = trigger_from_record(path, line_offset, record, extra_types)
                rec["offset"] = handle.tell()
                if not event:
                    continue

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
                    continue

                stable_id = codex_event_stable_id(event)
                if stable_id in state.setdefault("sent", {}):
                    continue

                title = event["notification_title"]
                body = event["notification_body"]
                if notifier.send(title, body, event):
                    state["sent"][stable_id] = int(time.time())
                    sent_count += 1
                    log(f"sent {event['event_type']} for {event['thread_id']} from {path.name}")
                else:
                    log(f"failed to send {event['event_type']} for {event['thread_id']} from {path.name}")
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
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    rec["offset"] = handle.tell()
                    continue

                event = trigger_from_zcode_record(path, line_offset, record)
                rec["offset"] = handle.tell()
                if not event:
                    continue

                stable_id_src = (
                    f"{path}:{line_offset}:{event['event_type']}:{event.get('query_id') or event.get('timestamp')}"
                )
                stable_id = hashlib.sha256(stable_id_src.encode("utf-8")).hexdigest()[:24]
                if stable_id in state.setdefault("sent", {}):
                    continue

                title = event["notification_title"]
                body = event["notification_body"]
                if notifier.send(title, body, event):
                    state["sent"][stable_id] = int(time.time())
                    sent_count += 1
                    log(f"sent {event['event_type']} for {event['session_id']} from {path.name}")
                else:
                    log(f"failed to send {event['event_type']} for {event['session_id']} from {path.name}")
    except OSError as exc:
        log(f"cannot read {path}: {exc}")
    finally:
        rec["size"] = size
        rec["updated_at"] = int(time.time())
    return sent_count


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


def build_roots(args: argparse.Namespace) -> list[Path]:
    roots = [expand_path(value) for value in (args.sessions_root or [DEFAULT_SESSIONS_ROOT])]
    include_archived = args.include_archived or os.getenv("CODEX_WATCH_INCLUDE_ARCHIVED") in {"1", "true", "True"}
    if include_archived:
        roots.append(expand_path(DEFAULT_ARCHIVED_ROOT))
    return roots


def build_zcode_log_root(args: argparse.Namespace) -> Path:
    return expand_path(args.zcode_log_root or os.getenv("ZCODE_WATCH_LOG_ROOT", DEFAULT_ZCODE_LOG_ROOT))


def zcode_watch_enabled(args: argparse.Namespace) -> bool:
    if args.disable_zcode:
        return False
    return os.getenv("ZCODE_WATCH_ENABLED", "1") not in {"0", "false", "False"}


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
    }
    ok = notifier.send("ZCode 测试提醒", "这是一条 ZCode 测试提醒。收到它说明 ZCode 分组和图标配置可用。", event)
    return 0 if ok else 1


def replay_file(args: argparse.Namespace, log: Logger) -> int:
    path = expand_path(args.replay_file)
    state = {"version": 1, "initialized": True, "files": {str(path): {"offset": 0}}, "sent": {}}
    notifier = Notifier(args.dry_run, log)
    sent = process_file(path, state, notifier, parse_extra_event_types(), log)
    log(f"replay finished: {sent} notification(s)", always_stdout=True)
    return 0


def main() -> int:
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
    parser.add_argument("--dry-run", action="store_true", help="Print notifications instead of sending them.")
    parser.add_argument("--verbose", action="store_true", help="Also print log lines to stdout.")
    parser.add_argument("--test", action="store_true", help="Send one test notification and exit.")
    parser.add_argument("--test-zcode", action="store_true", help="Send one ZCode test notification and exit.")
    parser.add_argument("--replay-file", help="Replay one rollout file from the beginning and exit.")
    args = parser.parse_args()

    log_path = None if args.dry_run else expand_path(args.log)
    log = Logger(log_path, verbose=args.verbose or args.dry_run)

    if args.test:
        return send_test_notification(args, log)
    if args.test_zcode:
        return send_zcode_test_notification(args, log)
    if args.replay_file:
        return replay_file(args, log)

    roots = build_roots(args)
    zcode_enabled = zcode_watch_enabled(args)
    zcode_root = build_zcode_log_root(args)
    state_path = expand_path(args.state)
    state = load_state(state_path)
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
    if did_baseline:
        save_state(state_path, state)
        if args.once:
            return 0

    notifier = Notifier(args.dry_run, log)
    extra_types = parse_extra_event_types()
    log(f"watching {', '.join(str(root) for root in roots)} with channels={notifier.channels}", always_stdout=True)
    if zcode_enabled:
        log(f"watching ZCode {zcode_root} with channels={notifier.channels}", always_stdout=True)

    while True:
        for path in rollout_files(roots):
            if str(path) not in state.setdefault("files", {}):
                state["files"][str(path)] = {
                    "offset": 0,
                    "head_hash": file_head_hash(path),
                    "new_file_at": int(time.time()),
                }
                log(f"new rollout discovered: {path}")
            process_file(path, state, notifier, extra_types, log)
        if zcode_enabled:
            for path in zcode_log_files(zcode_root):
                if str(path) not in state.setdefault("files", {}):
                    state["files"][str(path)] = {"offset": 0, "new_file_at": int(time.time()), "kind": "zcode"}
                    log(f"new ZCode log discovered: {path}")
                process_zcode_file(path, state, notifier, log)
        save_state(state_path, state)
        if args.once:
            return 0
        time.sleep(max(args.poll_interval, 0.5))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        raise SystemExit(130)
