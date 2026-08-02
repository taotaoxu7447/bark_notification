#!/usr/bin/env python3
"""Small, dependency-free registration and delivery-ack API for AgentWatch.

TLS termination is intentionally left to Caddy.  This process only listens on
the loopback interface and never logs request bodies, passwords, or tokens.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping


API_PREFIX = "/agentwatch/api/v1"
LISTEN_ADDRESS = "127.0.0.1"
LISTEN_PORT = 2587
ACK_RETENTION_SECONDS = 7 * 24 * 60 * 60
TOKEN_PATTERN = re.compile(r"\btk_[A-Za-z0-9]{20,125}\b")
USERNAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9_.-]{1,30}[a-z0-9])?\Z")
DEVICE_ID_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.:-]{6,126}[A-Za-z0-9])?\Z")
SEQUENCE_ID_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,126}[A-Za-z0-9])?\Z")
APP_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
TEST_SOURCES = frozenset({"codex", "zcode", "kimi", "grok", "other"})


def device_target_tag(device_id: str) -> str:
    """Return the non-reversible notification target shared with Android."""
    digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:24]
    return f"target_{digest}"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ProvisioningError(Exception):
    """A sanitized ntfy token-provisioning failure."""


class PublishError(Exception):
    """A sanitized ntfy publishing failure."""


@dataclasses.dataclass(frozen=True)
class Config:
    database_path: Path
    invite_code: str
    publisher_token: str
    ntfy_binary: str = "/usr/bin/ntfy"
    ntfy_config_file: str = "/etc/ntfy/server.yml"
    ntfy_subscriber_user: str = "agent-watch-subscriber"
    ntfy_internal_url: str = "http://127.0.0.1:2586/agent-watch"
    ntfy_public_url: str = "https://64.90.8.184:9444/agent-watch"
    topic: str = "agent-watch"
    max_request_body: int = 16 * 1024
    max_users: int = 32
    max_devices_per_user: int = 32
    # ntfy currently caps one user's tokens at 60. Keep headroom for the
    # create-before-revoke rotation and for a bounded number of stale tokens.
    max_devices_total: int = 50
    scrypt_n: int = 2**14
    scrypt_r: int = 8
    scrypt_p: int = 1

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "Config":
        values = os.environ if env is None else env
        invite_code = values.get("AGENTWATCH_INVITE_CODE", "")
        publisher_token = values.get("AGENTWATCH_NTFY_PUBLISHER_TOKEN", "")
        if len(invite_code.encode("utf-8")) < 16:
            raise ValueError("AGENTWATCH_INVITE_CODE must contain at least 16 UTF-8 bytes")
        if not TOKEN_PATTERN.fullmatch(publisher_token):
            raise ValueError("AGENTWATCH_NTFY_PUBLISHER_TOKEN is missing or malformed")

        config = cls(
            database_path=Path(
                values.get("AGENTWATCH_DATABASE_PATH", "/var/lib/agentwatch-registration/registration.db")
            ),
            invite_code=invite_code,
            publisher_token=publisher_token,
            ntfy_binary=values.get("AGENTWATCH_NTFY_BINARY", "/usr/bin/ntfy"),
            ntfy_config_file=values.get("AGENTWATCH_NTFY_CONFIG_FILE", "/etc/ntfy/server.yml"),
            ntfy_subscriber_user=values.get(
                "AGENTWATCH_NTFY_SUBSCRIBER_USER", "agent-watch-subscriber"
            ),
            ntfy_internal_url=values.get(
                "AGENTWATCH_NTFY_INTERNAL_URL", "http://127.0.0.1:2586/agent-watch"
            ),
            ntfy_public_url=values.get(
                "AGENTWATCH_NTFY_PUBLIC_URL", "https://64.90.8.184:9444/agent-watch"
            ),
            topic=values.get("AGENTWATCH_NTFY_TOPIC", "agent-watch"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not Path(self.ntfy_binary).is_absolute():
            raise ValueError("AGENTWATCH_NTFY_BINARY must be an absolute path")
        if not Path(self.ntfy_config_file).is_absolute():
            raise ValueError("AGENTWATCH_NTFY_CONFIG_FILE must be an absolute path")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.ntfy_subscriber_user):
            raise ValueError("invalid ntfy subscriber username")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.topic):
            raise ValueError("invalid ntfy topic")
        internal = urllib.parse.urlsplit(self.ntfy_internal_url)
        if (
            internal.scheme != "http"
            or internal.hostname not in {"127.0.0.1", "::1", "localhost"}
            or internal.username
            or internal.password
            or internal.query
            or internal.fragment
        ):
            raise ValueError("AGENTWATCH_NTFY_INTERNAL_URL must be a plain loopback HTTP URL")
        if internal.path.rstrip("/") != f"/{self.topic}":
            raise ValueError("AGENTWATCH_NTFY_INTERNAL_URL must point to the configured topic")
        public = urllib.parse.urlsplit(self.ntfy_public_url)
        if public.scheme != "https" or not public.hostname or public.username or public.password:
            raise ValueError("AGENTWATCH_NTFY_PUBLIC_URL must be an HTTPS URL without credentials")
        if public.query or public.fragment or public.path.rstrip("/") != f"/{self.topic}":
            raise ValueError("AGENTWATCH_NTFY_PUBLIC_URL must point to the configured topic")


class PasswordHasher:
    def __init__(self, n: int = 2**14, r: int = 8, p: int = 1) -> None:
        self.n = n
        self.r = r
        self.p = p
        self._work_slots = threading.BoundedSemaphore(4)
        self._dummy_salt = hashlib.sha256(b"agentwatch-login-dummy-salt-v1").digest()[:16]
        self._dummy_hash = self._derive("not-a-real-password", self._dummy_salt)

    def _derive(self, password: str, salt: bytes) -> bytes:
        try:
            return hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=self.n,
                r=self.r,
                p=self.p,
                maxmem=64 * 1024 * 1024,
                dklen=32,
            )
        except (ValueError, MemoryError) as exc:
            raise RuntimeError("password hashing is temporarily unavailable") from exc

    def hash_password(self, password: str) -> tuple[bytes, bytes]:
        if not self._work_slots.acquire(timeout=2.0):
            raise RuntimeError("password hashing is busy")
        try:
            salt = secrets.token_bytes(16)
            return salt, self._derive(password, salt)
        finally:
            self._work_slots.release()

    def verify(self, password: str, salt: bytes | None, expected: bytes | None) -> bool:
        actual_salt = salt if salt is not None else self._dummy_salt
        actual_expected = expected if expected is not None else self._dummy_hash
        if not self._work_slots.acquire(timeout=2.0):
            raise RuntimeError("password hashing is busy")
        try:
            candidate = self._derive(password, actual_salt)
            valid = hmac.compare_digest(candidate, actual_expected)
            return bool(valid and salt is not None and expected is not None)
        finally:
            self._work_slots.release()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = FULL;

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE BINARY,
                    password_salt BLOB NOT NULL CHECK(length(password_salt) = 16),
                    password_hash BLOB NOT NULL CHECK(length(password_hash) = 32),
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL UNIQUE COLLATE BINARY,
                    device_name TEXT NOT NULL,
                    app_token_hash BLOB NOT NULL UNIQUE CHECK(length(app_token_hash) = 32),
                    created_at INTEGER NOT NULL,
                    last_login_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS delivery_acks (
                    id INTEGER PRIMARY KEY,
                    device_row_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL,
                    acknowledged_at INTEGER NOT NULL,
                    UNIQUE(device_row_id, event_id)
                );

                CREATE INDEX IF NOT EXISTS delivery_acks_time_idx
                    ON delivery_acks(acknowledged_at);
                """
            )
            connection.execute(
                "DELETE FROM delivery_acks WHERE acknowledged_at < ?",
                (int(time.time()) - ACK_RETENTION_SECONDS,),
            )
            connection.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # A read-only or unusual test filesystem may reject chmod even though
            # SQLite itself is usable. The production systemd unit also sets 0077.
            pass

    def healthy(self) -> bool:
        try:
            with closing(self.connect()) as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False


@dataclasses.dataclass(frozen=True)
class IssuedNtfyToken:
    value: str
    label: str
    previous_values: tuple[str, ...]


Runner = Callable[[list[str], Mapping[str, str], float], subprocess.CompletedProcess[str]]


def _default_runner(
    command: list[str], environment: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
        env=dict(environment),
    )


class NtfyTokenManager:
    """Creates one ntfy read token per installation and rotates it on login."""

    def __init__(self, config: Config, runner: Runner = _default_runner) -> None:
        self.config = config
        self.runner = runner
        self._lock = threading.Lock()

    def _environment(self) -> dict[str, str]:
        # Deliberately do not pass the service environment to ntfy: it contains
        # the invite code and private publisher token.
        return {
            "HOME": "/var/lib/ntfy",
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "NTFY_CONFIG_FILE": self.config.ntfy_config_file,
        }

    def _run(self, arguments: list[str]) -> str:
        try:
            result = self.runner(
                [self.config.ntfy_binary, *arguments], self._environment(), 10.0
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProvisioningError("ntfy command could not be executed") from exc
        if result.returncode != 0:
            # stdout/stderr can contain access tokens and are never propagated.
            raise ProvisioningError("ntfy command rejected the token operation")
        return f"{result.stdout}\n{result.stderr}"

    @staticmethod
    def _label(device_id: str) -> str:
        digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:24]
        return f"agentwatch-{digest}"

    @staticmethod
    def _tokens_with_label(output: str, label: str) -> tuple[str, ...]:
        matches: list[str] = []
        for line in output.splitlines():
            match = re.match(r"^-\s+(tk_[A-Za-z0-9]{20,125})(?:\s+\(([^)]*)\))?,", line.strip())
            if match and match.group(2) == label:
                matches.append(match.group(1))
        return tuple(matches)

    def issue(self, device_id: str) -> IssuedNtfyToken:
        label = self._label(device_id)
        with self._lock:
            listed = self._run(["token", "list", self.config.ntfy_subscriber_user])
            previous = self._tokens_with_label(listed, label)
            created = self._run(
                [
                    "token",
                    "add",
                    f"--label={label}",
                    self.config.ntfy_subscriber_user,
                ]
            )
            tokens = TOKEN_PATTERN.findall(created)
            if len(tokens) != 1:
                raise ProvisioningError("ntfy returned an unexpected token response")
            return IssuedNtfyToken(tokens[0], label, previous)

    def _remove(self, token: str) -> None:
        self._run(["token", "remove", self.config.ntfy_subscriber_user, token])

    def rollback(self, issued: IssuedNtfyToken) -> None:
        try:
            self._remove(issued.value)
        except ProvisioningError:
            logging.getLogger("agentwatch-registration").warning(
                "ntfy_token_rollback_failed"
            )

    def finalize(self, issued: IssuedNtfyToken) -> None:
        for previous in issued.previous_values:
            if hmac.compare_digest(previous, issued.value):
                continue
            try:
                self._remove(previous)
            except ProvisioningError:
                logging.getLogger("agentwatch-registration").warning(
                    "ntfy_old_token_cleanup_failed"
                )

    def revoke_device(self, device_id: str) -> int:
        """Remove every ntfy token bearing one installation's derived label."""
        label = self._label(device_id)
        with self._lock:
            listed = self._run(["token", "list", self.config.ntfy_subscriber_user])
            tokens = self._tokens_with_label(listed, label)
            for token in tokens:
                self._remove(token)
            return len(tokens)


class NtfyPublisher:
    def __init__(
        self,
        url: str,
        publisher_token: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.url = url
        self.publisher_token = publisher_token
        self.opener = opener

    def publish_test(self, source: str, target: str) -> str:
        if source not in TEST_SOURCES:
            raise ValueError("unsupported test notification source")
        if not re.fullmatch(r"target_[0-9a-f]{24}", target):
            raise ValueError("invalid test notification target")
        sequence_id = f"aw1_server_test_{secrets.token_hex(12)}"
        request = urllib.request.Request(
            self.url,
            data=b"AgentWatch connection test",
            method="POST",
            headers={
                "Authorization": f"Bearer {self.publisher_token}",
                "Content-Type": "text/plain; charset=utf-8",
                "X-Title": "AgentWatch test",
                "X-Tags": f"white_check_mark,agentwatch_v1,source_{source},{target}",
                "X-Priority": "default",
                "X-Sequence-ID": sequence_id,
            },
        )
        try:
            with self.opener(request, timeout=8.0) as response:
                status = int(getattr(response, "status", 200))
                response.read(4096)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise PublishError("ntfy test publish failed") from exc
        if not 200 <= status < 300:
            raise PublishError("ntfy test publish returned a non-success status")
        return sequence_id


class SlidingWindowLimiter:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 1024,
        key_idle_seconds: float = 600.0,
    ) -> None:
        self.clock = clock
        self.max_keys = max_keys
        self.key_idle_seconds = key_idle_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        now = self.clock()
        threshold = now - window_seconds
        with self._lock:
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= self.max_keys:
                    stale_before = now - self.key_idle_seconds
                    stale = [
                        name
                        for name, values in self._events.items()
                        if not values or values[-1] <= stale_before
                    ]
                    for name in stale:
                        self._events.pop(name, None)
                if len(self._events) >= self.max_keys:
                    evictable = [
                        (values[-1] if values else float("-inf"), name)
                        for name, values in self._events.items()
                        if not name.startswith("global:")
                    ]
                    if not evictable:
                        return False
                    _, oldest = min(evictable)
                    self._events.pop(oldest, None)
                events = deque()
                self._events[key] = events
            while events and events[0] <= threshold:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True


@dataclasses.dataclass(frozen=True)
class Response:
    status: int
    payload: Mapping[str, Any] | None = None
    headers: Mapping[str, str] = dataclasses.field(default_factory=dict)


class AgentWatchApplication:
    def __init__(
        self,
        config: Config,
        database: Database,
        token_manager: Any,
        publisher: Any,
        hasher: PasswordHasher | None = None,
        limiter: SlidingWindowLimiter | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.token_manager = token_manager
        self.publisher = publisher
        self.hasher = hasher or PasswordHasher(config.scrypt_n, config.scrypt_r, config.scrypt_p)
        self.limiter = limiter or SlidingWindowLimiter()
        self._provisioning_lock = threading.Lock()
        self._invite_digest = hashlib.sha256(config.invite_code.encode("utf-8")).digest()
        self.logger = logging.getLogger("agentwatch-registration")

    @staticmethod
    def _decode_object(raw: bytes, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
        optional = optional or set()

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite number")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ApiError(400, "invalid_json", "Request body must be a valid JSON object") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json", "Request body must be a JSON object")
        keys = set(value)
        if not required.issubset(keys) or not keys.issubset(required | optional):
            raise ApiError(400, "invalid_fields", "Request fields do not match this endpoint")
        return value

    @staticmethod
    def _string(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise ApiError(400, "invalid_field", f"{field} must be a string")
        return value

    @classmethod
    def _username(cls, value: Any) -> str:
        username = cls._string(value, "username").lower()
        if not 3 <= len(username) <= 32 or not USERNAME_PATTERN.fullmatch(username):
            raise ApiError(400, "invalid_username", "Username must contain 3-32 safe ASCII characters")
        return username

    @classmethod
    def _password(cls, value: Any) -> str:
        password = cls._string(value, "password")
        encoded = password.encode("utf-8")
        if not 12 <= len(password) <= 128 or len(encoded) > 512 or "\x00" in password:
            raise ApiError(400, "invalid_password", "Password must contain 12-128 characters")
        return password

    @classmethod
    def _device_id(cls, value: Any) -> str:
        device_id = cls._string(value, "device_id")
        if not 8 <= len(device_id) <= 128 or not DEVICE_ID_PATTERN.fullmatch(device_id):
            raise ApiError(400, "invalid_device_id", "Device ID must contain 8-128 safe ASCII characters")
        return device_id

    @classmethod
    def _device_name(cls, value: Any) -> str:
        name = unicodedata.normalize("NFKC", cls._string(value, "device_name").strip())
        if not 1 <= len(name) <= 80 or len(name.encode("utf-8")) > 240:
            raise ApiError(400, "invalid_device_name", "Device name must contain 1-80 characters")
        if any(unicodedata.category(character).startswith("C") for character in name):
            raise ApiError(400, "invalid_device_name", "Device name contains control characters")
        return name

    def _check_rate(self, key: str, limit: int, window: float) -> None:
        if not self.limiter.allow(key, limit, window):
            raise ApiError(429, "rate_limited", "Too many requests; try again later")

    def _check_route_rate(self, path: str, client_ip: str) -> None:
        rules = {
            f"{API_PREFIX}/register": (10, 300.0),
            f"{API_PREFIX}/login": (30, 300.0),
            f"{API_PREFIX}/logout": (10, 300.0),
            f"{API_PREFIX}/test": (30, 60.0),
            f"{API_PREFIX}/ack": (600, 60.0),
            f"{API_PREFIX}/health": (120, 60.0),
        }
        route_key = path if path in rules else "unknown"
        limit, window = rules.get(path, (30, 60.0))
        # This fixed bucket remains effective even when distributed clients
        # churn the bounded dynamic-key table.
        self._check_rate("global:requests", 3000, 60.0)
        self._check_rate(f"ip:{client_ip}:{route_key}", limit, window)

    @staticmethod
    def _bearer(headers: Mapping[str, str]) -> str:
        authorization = headers.get("authorization", "")
        if len(authorization) > 256:
            raise ApiError(401, "unauthorized", "Valid app token required")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not APP_TOKEN_PATTERN.fullmatch(parts[1]):
            raise ApiError(401, "unauthorized", "Valid app token required")
        return parts[1]

    def _authenticate_app(self, headers: Mapping[str, str]) -> sqlite3.Row:
        token = self._bearer(headers)
        candidate = hashlib.sha256(token.encode("ascii")).digest()
        selected: sqlite3.Row | None = None
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT devices.id AS device_row_id, devices.device_id, devices.user_id, users.username,
                       devices.app_token_hash
                FROM devices JOIN users ON users.id = devices.user_id
                """
            ).fetchall()
        if not rows:
            hmac.compare_digest(candidate, bytes(32))
        for row in rows:
            matches = hmac.compare_digest(candidate, bytes(row["app_token_hash"]))
            if matches:
                selected = row
        if selected is None:
            raise ApiError(401, "unauthorized", "Valid app token required")
        try:
            with closing(self.database.connect()) as connection:
                connection.execute(
                    "UPDATE devices SET last_login_at = ? WHERE id = ?",
                    (int(time.time()), selected["device_row_id"]),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise ApiError(503, "database_error", "Device session could not be updated") from exc
        return selected

    def _client_credentials(self, username: str, device_id: str, ntfy_token: str, app_token: str) -> Response:
        parsed = urllib.parse.urlsplit(self.config.ntfy_public_url)
        websocket_scheme = "wss" if parsed.scheme == "https" else "ws"
        websocket_url = urllib.parse.urlunsplit(
            (websocket_scheme, parsed.netloc, parsed.path.rstrip("/") + "/ws", "", "")
        )
        return Response(
            200,
            {
                "api_version": 1,
                "username": username,
                "device_id": device_id,
                "ntfy_url": self.config.ntfy_public_url,
                "ntfy_ws_url": websocket_url,
                "ntfy_topic": self.config.topic,
                "target_tag": device_target_tag(device_id),
                "ntfy_token": ntfy_token,
                "app_token": app_token,
            },
        )

    def _register(self, raw: bytes) -> Response:
        payload = self._decode_object(
            raw, {"username", "password", "invite_code", "device_id", "device_name"}
        )
        username = self._username(payload["username"])
        password = self._password(payload["password"])
        device_id = self._device_id(payload["device_id"])
        device_name = self._device_name(payload["device_name"])
        invite = self._string(payload["invite_code"], "invite_code")
        invite_digest = hashlib.sha256(invite.encode("utf-8")).digest()
        if not hmac.compare_digest(invite_digest, self._invite_digest):
            raise ApiError(403, "invalid_invite", "Invitation code is invalid")
        try:
            salt, password_hash = self.hasher.hash_password(password)
        except RuntimeError as exc:
            raise ApiError(503, "auth_busy", "Authentication is temporarily unavailable") from exc

        with self._provisioning_lock:
            with closing(self.database.connect()) as connection:
                if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                    raise ApiError(409, "account_exists", "Account already exists")
                if connection.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone():
                    raise ApiError(409, "device_exists", "Device is already registered")
                if connection.execute("SELECT count(*) FROM users").fetchone()[0] >= self.config.max_users:
                    raise ApiError(409, "capacity_reached", "Registration capacity has been reached")
                if connection.execute("SELECT count(*) FROM devices").fetchone()[0] >= self.config.max_devices_total:
                    raise ApiError(409, "capacity_reached", "Registration capacity has been reached")

            app_token = secrets.token_urlsafe(32)
            app_token_hash = hashlib.sha256(app_token.encode("ascii")).digest()
            try:
                issued = self.token_manager.issue(device_id)
            except ProvisioningError as exc:
                raise ApiError(503, "provisioning_failed", "Device credentials could not be provisioned") from exc
            now = int(time.time())
            try:
                with closing(self.database.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    cursor = connection.execute(
                        """
                        INSERT INTO users(username, password_salt, password_hash, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (username, salt, password_hash, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO devices(user_id, device_id, device_name, app_token_hash, created_at, last_login_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (cursor.lastrowid, device_id, device_name, app_token_hash, now, now),
                    )
                    connection.commit()
            except sqlite3.Error as exc:
                self.token_manager.rollback(issued)
                raise ApiError(503, "database_error", "Registration could not be saved") from exc
            self.token_manager.finalize(issued)
            response = self._client_credentials(username, device_id, issued.value, app_token)
            return dataclasses.replace(response, status=201)

    def _login(self, raw: bytes) -> Response:
        payload = self._decode_object(raw, {"username", "password", "device_id", "device_name"})
        username = self._username(payload["username"])
        password = self._password(payload["password"])
        device_id = self._device_id(payload["device_id"])
        device_name = self._device_name(payload["device_name"])
        username_key = hashlib.sha256(username.encode("ascii")).hexdigest()[:20]
        self._check_rate(f"account:{username_key}:login", 20, 300.0)

        with closing(self.database.connect()) as connection:
            user = connection.execute(
                "SELECT id, password_salt, password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        try:
            valid = self.hasher.verify(
                password,
                bytes(user["password_salt"]) if user else None,
                bytes(user["password_hash"]) if user else None,
            )
        except RuntimeError as exc:
            raise ApiError(503, "auth_busy", "Authentication is temporarily unavailable") from exc
        if not valid or user is None:
            raise ApiError(401, "invalid_credentials", "Username or password is incorrect")

        with self._provisioning_lock:
            with closing(self.database.connect()) as connection:
                existing = connection.execute(
                    "SELECT user_id FROM devices WHERE device_id = ?", (device_id,)
                ).fetchone()
                if existing and existing["user_id"] != user["id"]:
                    raise ApiError(409, "device_conflict", "Device belongs to another account")
                device_count = connection.execute(
                    "SELECT count(*) FROM devices WHERE user_id = ?", (user["id"],)
                ).fetchone()[0]
                if not existing and device_count >= self.config.max_devices_per_user:
                    raise ApiError(409, "device_capacity_reached", "Account device capacity has been reached")
                total_devices = connection.execute("SELECT count(*) FROM devices").fetchone()[0]
                if not existing and total_devices >= self.config.max_devices_total:
                    raise ApiError(409, "capacity_reached", "Registration capacity has been reached")

            app_token = secrets.token_urlsafe(32)
            app_token_hash = hashlib.sha256(app_token.encode("ascii")).digest()
            try:
                issued = self.token_manager.issue(device_id)
            except ProvisioningError as exc:
                raise ApiError(503, "provisioning_failed", "Device credentials could not be provisioned") from exc
            now = int(time.time())
            try:
                with closing(self.database.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        INSERT INTO devices(user_id, device_id, device_name, app_token_hash, created_at, last_login_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(device_id) DO UPDATE SET
                            device_name = excluded.device_name,
                            app_token_hash = excluded.app_token_hash,
                            last_login_at = excluded.last_login_at
                        WHERE devices.user_id = excluded.user_id
                        """,
                        (user["id"], device_id, device_name, app_token_hash, now, now),
                    )
                    connection.commit()
            except sqlite3.Error as exc:
                self.token_manager.rollback(issued)
                raise ApiError(503, "database_error", "Login could not be saved") from exc
            self.token_manager.finalize(issued)
            return self._client_credentials(username, device_id, issued.value, app_token)

    def _logout(self, raw: bytes, headers: Mapping[str, str]) -> Response:
        self._decode_object(raw, set())
        device = self._authenticate_app(headers)
        self._check_rate(f"device:{device['device_row_id']}:logout", 3, 300.0)
        with self._provisioning_lock:
            with closing(self.database.connect()) as connection:
                current = connection.execute(
                    "SELECT app_token_hash FROM devices WHERE id = ?",
                    (device["device_row_id"],),
                ).fetchone()
            if current is None or not hmac.compare_digest(
                bytes(current["app_token_hash"]), bytes(device["app_token_hash"])
            ):
                raise ApiError(401, "session_replaced", "Device session is no longer current")
            try:
                self.token_manager.revoke_device(device["device_id"])
            except ProvisioningError as exc:
                raise ApiError(503, "logout_failed", "Device credentials could not be revoked") from exc
            try:
                with closing(self.database.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    cursor = connection.execute(
                        "DELETE FROM devices WHERE id = ? AND app_token_hash = ?",
                        (device["device_row_id"], bytes(device["app_token_hash"])),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        raise ApiError(503, "logout_failed", "Device session could not be removed")
                    connection.commit()
            except sqlite3.Error as exc:
                raise ApiError(503, "logout_failed", "Device session could not be removed") from exc
        return Response(200, {"ok": True})

    def _test(self, raw: bytes, headers: Mapping[str, str]) -> Response:
        payload = self._decode_object(raw, set(), {"source"})
        source = self._string(payload.get("source", "other"), "source")
        if source not in TEST_SOURCES:
            raise ApiError(400, "invalid_source", "source is invalid")
        device = self._authenticate_app(headers)
        self._check_rate("global:test", 10, 60.0)
        self._check_rate(f"device:{device['device_row_id']}:test", 3, 60.0)
        target = device_target_tag(device["device_id"])
        try:
            sequence_id = self.publisher.publish_test(source, target)
        except PublishError as exc:
            raise ApiError(502, "publish_failed", "Test notification could not be published") from exc
        # `sequence_id` is retained for older clients; Android treats the same
        # stable identifier as its protocol-level event_id.
        return Response(
            200,
            {
                "ok": True,
                "event_id": sequence_id,
                "sequence_id": sequence_id,
                "target_tag": target,
            },
        )

    def _ack(self, raw: bytes, headers: Mapping[str, str]) -> Response:
        payload = self._decode_object(
            raw,
            set(),
            {"event_id", "sequence_id", "message_id", "source", "received_at", "app_version"},
        )
        event_value = payload.get("event_id")
        sequence_value = payload.get("sequence_id")
        if event_value is None and sequence_value is None:
            raise ApiError(400, "missing_event_id", "event_id is required")
        event_id = self._string(
            event_value if event_value is not None else sequence_value, "event_id"
        )
        if sequence_value is not None:
            compatible_sequence = self._string(sequence_value, "sequence_id")
            if event_value is not None and not hmac.compare_digest(event_id, compatible_sequence):
                raise ApiError(400, "conflicting_event_id", "event_id and sequence_id must match")
        if not SEQUENCE_ID_PATTERN.fullmatch(event_id):
            raise ApiError(400, "invalid_event_id", "Event ID is invalid")
        self._validate_optional_ack_metadata(payload)
        device = self._authenticate_app(headers)
        self._check_rate(f"device:{device['device_row_id']}:ack", 120, 60.0)
        try:
            acknowledged_at = int(time.time())
            with closing(self.database.connect()) as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO delivery_acks(device_row_id, event_id, acknowledged_at)
                    VALUES (?, ?, ?)
                    """,
                    (device["device_row_id"], event_id, acknowledged_at),
                )
                connection.execute(
                    "DELETE FROM delivery_acks WHERE acknowledged_at < ?",
                    (acknowledged_at - ACK_RETENTION_SECONDS,),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise ApiError(503, "database_error", "Acknowledgement could not be saved") from exc
        return Response(202, {"ok": True})

    def _validate_optional_ack_metadata(self, payload: Mapping[str, Any]) -> None:
        """Validate Android diagnostics without retaining them as message history."""
        message_id = payload.get("message_id")
        if message_id is not None:
            value = self._string(message_id, "message_id")
            if not 1 <= len(value) <= 128 or not SEQUENCE_ID_PATTERN.fullmatch(value):
                raise ApiError(400, "invalid_message_id", "message_id is invalid")
        source = payload.get("source")
        if source is not None:
            value = self._string(source, "source")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
                raise ApiError(400, "invalid_source", "source is invalid")
        received_at = payload.get("received_at")
        if received_at is not None:
            if isinstance(received_at, bool) or not isinstance(received_at, int) or received_at < 0:
                raise ApiError(400, "invalid_received_at", "received_at must be a positive integer")
        app_version = payload.get("app_version")
        if app_version is not None:
            value = self._string(app_version, "app_version")
            if not 1 <= len(value) <= 32 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
                raise ApiError(400, "invalid_app_version", "app_version is invalid")

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        raw: bytes,
        client_ip: str,
    ) -> Response:
        self._check_route_rate(path, client_ip)
        if method == "GET" and path == f"{API_PREFIX}/health":
            if not self.database.healthy():
                return Response(503, {"ok": False})
            return Response(200, {"ok": True})
        if method != "POST":
            raise ApiError(405, "method_not_allowed", "Method is not allowed")
        if path == f"{API_PREFIX}/register":
            return self._register(raw)
        if path == f"{API_PREFIX}/login":
            return self._login(raw)
        if path == f"{API_PREFIX}/logout":
            return self._logout(raw, headers)
        if path == f"{API_PREFIX}/test":
            return self._test(raw, headers)
        if path == f"{API_PREFIX}/ack":
            return self._ack(raw, headers)
        raise ApiError(404, "not_found", "Endpoint was not found")


class AgentWatchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, server_address: tuple[str, int], application: AgentWatchApplication) -> None:
        self.application = application
        super().__init__(server_address, AgentWatchRequestHandler)

    def get_request(self) -> tuple[socket.socket, Any]:
        connection, address = super().get_request()
        connection.settimeout(10.0)
        return connection, address


class AgentWatchRequestHandler(BaseHTTPRequestHandler):
    server: AgentWatchHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "AgentWatchRegistration"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        path = urllib.parse.urlsplit(self.path).path
        self.server.application.logger.info("http_request method=%s path=%s", self.command, path)

    def _client_ip(self) -> str:
        peer = self.client_address[0]
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return "invalid"
        forwarded = self.headers.get("X-Forwarded-For", "")
        if peer_address.is_loopback and forwarded and len(forwarded) <= 256:
            candidate = forwarded.split(",", 1)[0].strip()
            try:
                return ipaddress.ip_address(candidate).compressed
            except ValueError:
                return "invalid"
        return peer_address.compressed

    def _headers(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers.items()}

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "invalid_transfer", "Chunked request bodies are not accepted")
        length_value = self.headers.get("Content-Length")
        if length_value is None or not length_value.isdigit():
            raise ApiError(411, "length_required", "A valid Content-Length header is required")
        length = int(length_value)
        if length <= 0:
            raise ApiError(400, "empty_body", "A JSON request body is required")
        if length > self.server.application.config.max_request_body:
            raise ApiError(413, "body_too_large", "Request body is too large")
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise ApiError(415, "unsupported_media_type", "Content-Type must be application/json")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ApiError(400, "incomplete_body", "Request body is incomplete")
        return data

    def _send(self, response: Response) -> None:
        body = b"" if response.payload is None else json.dumps(
            response.payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def _dispatch(self) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.query or parsed.fragment:
                raise ApiError(400, "invalid_url", "Query strings are not accepted")
            raw = self._body() if self.command == "POST" else b""
            response = self.server.application.handle(
                self.command, parsed.path, self._headers(), raw, self._client_ip()
            )
        except ApiError as exc:
            headers = {"Retry-After": "60"} if exc.status == 429 else {}
            response = Response(exc.status, {"error": exc.code, "message": exc.message}, headers)
        except socket.timeout:
            response = Response(408, {"error": "request_timeout", "message": "Request timed out"})
        except Exception:
            self.server.application.logger.exception("unhandled_request_error")
            response = Response(500, {"error": "internal_error", "message": "Internal server error"})
        self._send(response)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch()


def main() -> int:
    os.umask(0o077)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("agentwatch-registration")
    try:
        config = Config.from_environment()
        database = Database(config.database_path)
        database.initialize()
        application = AgentWatchApplication(
            config,
            database,
            NtfyTokenManager(config),
            NtfyPublisher(config.ntfy_internal_url, config.publisher_token),
        )
        server = AgentWatchHTTPServer((LISTEN_ADDRESS, LISTEN_PORT), application)
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        logger.error("startup_failed reason=%s", str(exc))
        return 1

    def request_shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    logger.info("listening address=%s port=%d", LISTEN_ADDRESS, LISTEN_PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
