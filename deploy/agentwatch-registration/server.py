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
COMPUTER_TOKEN_PATTERN = re.compile(r"awc_[A-Za-z0-9_-]{32,128}\Z")
TEST_SOURCES = frozenset({"codex", "zcode", "kimi", "grok", "other"})
NTFY_PRIORITIES = frozenset({"min", "low", "default", "high", "max"})
PRIVATE_TOPIC_PATTERN = re.compile(r"aw-[0-9a-f]{32}\Z")
PRIVATE_PRINCIPAL_PATTERN = re.compile(r"awu[0-9a-f]{24}\Z")
COMPUTER_TOKEN_TTL_SECONDS = 0  # Revocation-based by default; no silent expiry.
MAX_NTFY_MESSAGE_BYTES = 3900


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
    # Kept only while v0.1 Android installations migrate one by one.
    ntfy_subscriber_user: str = "agent-watch-subscriber"
    ntfy_publisher_user: str = "agent-watch-publisher"
    ntfy_internal_url: str = "http://127.0.0.1:2586/agent-watch"
    ntfy_public_url: str = "https://64.90.8.184:9444/agent-watch"
    topic: str = "agent-watch"
    max_request_body: int = 16 * 1024
    max_users: int = 32
    max_devices_per_user: int = 32
    # ntfy caps each private subscriber principal at 60 tokens. The per-user
    # device cap leaves headroom for create-before-revoke rotation.
    max_devices_total: int = 512
    max_computers_per_user: int = 64
    computer_token_ttl_seconds: int = COMPUTER_TOKEN_TTL_SECONDS
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
            ntfy_publisher_user=values.get(
                "AGENTWATCH_NTFY_PUBLISHER_USER", "agent-watch-publisher"
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
        for username in (self.ntfy_subscriber_user, self.ntfy_publisher_user):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username):
                raise ValueError("invalid ntfy service username")
        if self.ntfy_subscriber_user == self.ntfy_publisher_user:
            raise ValueError("ntfy subscriber and publisher users must be distinct")
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
        if self.computer_token_ttl_seconds != 0 and not (
            3600 <= self.computer_token_ttl_seconds <= 2 * 365 * 24 * 60 * 60
        ):
            raise ValueError("computer token lifetime is outside the supported range")

    @staticmethod
    def _topic_url(base_topic_url: str, topic: str) -> str:
        if not PRIVATE_TOPIC_PATTERN.fullmatch(topic):
            raise ValueError("invalid private topic")
        parsed = urllib.parse.urlsplit(base_topic_url)
        prefix, _, _legacy_topic = parsed.path.rstrip("/").rpartition("/")
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, f"{prefix}/{topic}", "", "")
        )

    def private_internal_url(self, topic: str) -> str:
        return self._topic_url(self.ntfy_internal_url, topic)

    def private_public_url(self, topic: str) -> str:
        return self._topic_url(self.ntfy_public_url, topic)


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
                    last_login_at INTEGER NOT NULL,
                    private_ready_at INTEGER
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
            # v0.2 is an in-place, nullable-first migration. Existing v0.1
            # sessions stay usable until that installation calls login or
            # /session/upgrade and receives its private subscription token.
            user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            for name, declaration in (
                ("private_topic", "TEXT COLLATE BINARY"),
                ("ntfy_subscriber_user", "TEXT COLLATE BINARY"),
                ("channel_created_at", "INTEGER"),
            ):
                if name not in user_columns:
                    connection.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")
            device_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(devices)").fetchall()
            }
            if "private_ready_at" not in device_columns:
                connection.execute("ALTER TABLE devices ADD COLUMN private_ready_at INTEGER")
            connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS users_private_topic_idx
                    ON users(private_topic) WHERE private_topic IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS users_ntfy_subscriber_user_idx
                    ON users(ntfy_subscriber_user) WHERE ntfy_subscriber_user IS NOT NULL;

                CREATE TABLE IF NOT EXISTS computers (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    computer_id TEXT NOT NULL UNIQUE COLLATE BINARY,
                    computer_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    token_hash BLOB NOT NULL UNIQUE CHECK(length(token_hash) = 32),
                    created_at INTEGER NOT NULL,
                    last_login_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS computers_user_active_idx
                    ON computers(user_id, revoked_at, last_seen_at);
                """
            )
            connection.execute(
                "DELETE FROM delivery_acks WHERE acknowledged_at < ?",
                (int(time.time()) - ACK_RETENTION_SECONDS,),
            )
            connection.execute("PRAGMA user_version = 2")
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


@dataclasses.dataclass(frozen=True)
class ProvisionedNtfyChannel:
    subscriber_user: str
    topic: str


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
    """Provision isolated ntfy readers and per-installation read tokens.

    The random password used to create an ntfy principal exists only in the
    child process environment. It is neither an argv item nor application data:
    Android authenticates exclusively with a per-installation ntfy token.
    """

    def __init__(self, config: Config, runner: Runner = _default_runner) -> None:
        self.config = config
        self.runner = runner
        self._lock = threading.Lock()

    def _environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        # Deliberately do not pass the service environment to ntfy: it contains
        # the invite code and private publisher token.
        environment = {
            "HOME": "/var/lib/ntfy",
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "NTFY_CONFIG_FILE": self.config.ntfy_config_file,
        }
        if extra:
            environment.update(extra)
        return environment

    def _run(self, arguments: list[str], extra_environment: Mapping[str, str] | None = None) -> str:
        try:
            result = self.runner(
                [self.config.ntfy_binary, *arguments],
                self._environment(extra_environment),
                10.0,
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

    def provision_channel(self, subscriber_user: str, topic: str) -> ProvisionedNtfyChannel:
        if not PRIVATE_PRINCIPAL_PATTERN.fullmatch(subscriber_user):
            raise ProvisioningError("private subscriber principal is invalid")
        if not PRIVATE_TOPIC_PATTERN.fullmatch(topic):
            raise ProvisioningError("private topic is invalid")
        password = secrets.token_urlsafe(36)
        user_created = False
        with self._lock:
            try:
                self._run(
                    ["user", "add", "--role=user", subscriber_user],
                    {"NTFY_PASSWORD": password},
                )
                user_created = True
                self._run(["access", subscriber_user, topic, "ro"])
                self._run(["access", self.config.ntfy_publisher_user, topic, "wo"])
            except ProvisioningError:
                # Reset even if `access` returned an error: the CLI may have
                # committed its SQLite transaction before its process failed.
                try:
                    self._run(
                        ["access", "--reset", self.config.ntfy_publisher_user, topic]
                    )
                except ProvisioningError:
                    logging.getLogger("agentwatch-registration").warning(
                        "ntfy_channel_publisher_acl_rollback_failed"
                    )
                if user_created:
                    try:
                        self._run(["user", "del", subscriber_user])
                    except ProvisioningError:
                        logging.getLogger("agentwatch-registration").warning(
                            "ntfy_channel_user_rollback_failed"
                        )
                raise
            finally:
                # Avoid accidentally reusing the random password in this process.
                password = ""
        return ProvisionedNtfyChannel(subscriber_user, topic)

    def rollback_channel(self, channel: ProvisionedNtfyChannel) -> None:
        with self._lock:
            try:
                self._run(
                    ["access", "--reset", self.config.ntfy_publisher_user, channel.topic]
                )
            except ProvisioningError:
                logging.getLogger("agentwatch-registration").warning(
                    "ntfy_channel_publisher_acl_rollback_failed"
                )
            try:
                self._run(["user", "del", channel.subscriber_user])
            except ProvisioningError:
                logging.getLogger("agentwatch-registration").warning(
                    "ntfy_channel_user_rollback_failed"
                )

    def issue(self, subscriber_user: str, device_id: str) -> IssuedNtfyToken:
        if not PRIVATE_PRINCIPAL_PATTERN.fullmatch(subscriber_user):
            raise ProvisioningError("private subscriber principal is invalid")
        label = self._label(device_id)
        with self._lock:
            listed = self._run(["token", "list", subscriber_user])
            previous = self._tokens_with_label(listed, label)
            created = self._run(
                [
                    "token",
                    "add",
                    f"--label={label}",
                    subscriber_user,
                ]
            )
            tokens = TOKEN_PATTERN.findall(created)
            if len(tokens) != 1:
                raise ProvisioningError("ntfy returned an unexpected token response")
            return IssuedNtfyToken(tokens[0], label, previous)

    def _remove(self, subscriber_user: str, token: str) -> None:
        self._run(["token", "remove", subscriber_user, token])

    def rollback(self, subscriber_user: str, issued: IssuedNtfyToken) -> None:
        try:
            self._remove(subscriber_user, issued.value)
        except ProvisioningError:
            logging.getLogger("agentwatch-registration").warning(
                "ntfy_token_rollback_failed"
            )

    def finalize(self, subscriber_user: str, issued: IssuedNtfyToken) -> None:
        for previous in issued.previous_values:
            if hmac.compare_digest(previous, issued.value):
                continue
            try:
                self._remove(subscriber_user, previous)
            except ProvisioningError:
                logging.getLogger("agentwatch-registration").warning(
                    "ntfy_old_token_cleanup_failed"
                )

    def revoke_device(self, subscriber_user: str, device_id: str) -> int:
        """Remove every ntfy token bearing one installation's derived label."""
        label = self._label(device_id)
        with self._lock:
            listed = self._run(["token", "list", subscriber_user])
            tokens = self._tokens_with_label(listed, label)
            for token in tokens:
                self._remove(subscriber_user, token)
            return len(tokens)

    def revoke_legacy_device(self, device_id: str) -> int:
        return self.revoke_device(self.config.ntfy_subscriber_user, device_id)

    def legacy_token_count(self, device_id: str) -> int:
        label = self._label(device_id)
        with self._lock:
            listed = self._run(["token", "list", self.config.ntfy_subscriber_user])
            return len(self._tokens_with_label(listed, label))

    def audit_channel_acl(self, subscriber_user: str, topic: str) -> tuple[bool, bool]:
        if not PRIVATE_PRINCIPAL_PATTERN.fullmatch(subscriber_user):
            return False, False
        if not PRIVATE_TOPIC_PATTERN.fullmatch(topic):
            return False, False
        with self._lock:
            subscriber_access = self._run(["access", subscriber_user])
            publisher_access = self._run(["access", self.config.ntfy_publisher_user])

        def entries(output: str) -> list[tuple[str, str]]:
            parsed: list[tuple[str, str]] = []
            for line in output.splitlines():
                match = re.fullmatch(
                    r"-\s+(read-only|write-only|read-write|no) access to topic ([A-Za-z0-9_*.-]+)",
                    line.strip().lower(),
                )
                if match:
                    parsed.append((match.group(1), match.group(2)))
            return parsed

        subscriber_entries = entries(subscriber_access)
        publisher_entries = entries(publisher_access)
        subscriber_is_user = "(admin)" not in subscriber_access.lower()
        publisher_is_user = "(admin)" not in publisher_access.lower()
        publisher_entries_are_bounded = all(
            mode == "write-only"
            and (candidate == self.config.topic or PRIVATE_TOPIC_PATTERN.fullmatch(candidate))
            for mode, candidate in publisher_entries
        )
        return (
            subscriber_is_user and subscriber_entries == [("read-only", topic)],
            publisher_is_user
            and publisher_entries_are_bounded
            and ("write-only", topic) in publisher_entries,
        )

    def reset_legacy_acls(self) -> None:
        with self._lock:
            self._run(
                ["access", "--reset", self.config.ntfy_subscriber_user, self.config.topic]
            )
            self._run(
                ["access", "--reset", self.config.ntfy_publisher_user, self.config.topic]
            )


class NtfyPublisher:
    def __init__(
        self,
        legacy_url: str,
        publisher_token: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.legacy_url = legacy_url
        self.publisher_token = publisher_token
        self.opener = opener

    def _topic_url(self, topic: str) -> str:
        parsed = urllib.parse.urlsplit(self.legacy_url)
        prefix, _, _legacy_topic = parsed.path.rstrip("/").rpartition("/")
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, f"{prefix}/{topic}", "", "")
        )

    def _publish(
        self,
        topic: str,
        message: bytes,
        title: str,
        tags: tuple[str, ...],
        priority: str,
        sequence_id: str,
    ) -> None:
        if not PRIVATE_TOPIC_PATTERN.fullmatch(topic):
            raise ValueError("invalid private topic")
        query = urllib.parse.urlencode(
            {"title": title, "tags": ",".join(tags), "priority": priority}
        )
        request = urllib.request.Request(
            f"{self._topic_url(topic)}?{query}",
            data=message,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.publisher_token}",
                "Content-Type": "text/plain; charset=utf-8",
                "X-Sequence-ID": sequence_id,
            },
        )
        try:
            with self.opener(request, timeout=8.0) as response:
                status = int(getattr(response, "status", 200))
                response.read(4096)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise PublishError("ntfy publish failed") from exc
        if not 200 <= status < 300:
            raise PublishError("ntfy publish returned a non-success status")

    def publish_test(self, topic: str, source: str, target: str) -> str:
        if source not in TEST_SOURCES:
            raise ValueError("unsupported test notification source")
        if not re.fullmatch(r"target_[0-9a-f]{24}", target):
            raise ValueError("invalid test notification target")
        sequence_id = f"aw2_server_test_{secrets.token_hex(12)}"
        message = json.dumps(
            {
                "schema": "agentwatch_event_v2",
                "event_id": sequence_id,
                "source": source,
                "title": "AgentWatch test",
                "body": "AgentWatch connection test",
                "computer_id": "server-test",
                "computer_name": "AgentWatch",
                "sent_at": int(time.time()),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._publish(
            topic,
            message,
            "AgentWatch test",
            ("white_check_mark", "agentwatch_v2", f"source_{source}", target),
            "default",
            sequence_id,
        )
        return sequence_id

    def publish_event(
        self,
        topic: str,
        event_id: str,
        source: str,
        title: str,
        message: bytes,
        priority: str,
    ) -> None:
        if source not in TEST_SOURCES:
            raise ValueError("unsupported notification source")
        if priority not in NTFY_PRIORITIES:
            raise ValueError("unsupported notification priority")
        self._publish(
            topic,
            message,
            title,
            ("agentwatch_v2", f"source_{source}"),
            priority,
            event_id,
        )


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

    @classmethod
    def _computer_id(cls, value: Any) -> str:
        computer_id = cls._string(value, "computer_id")
        if not 8 <= len(computer_id) <= 128 or not DEVICE_ID_PATTERN.fullmatch(computer_id):
            raise ApiError(
                400, "invalid_computer_id", "Computer ID must contain 8-128 safe ASCII characters"
            )
        return computer_id

    @classmethod
    def _computer_name(cls, value: Any) -> str:
        try:
            return cls._device_name(value)
        except ApiError as exc:
            raise ApiError(400, "invalid_computer_name", exc.message.replace("Device", "Computer")) from exc

    @classmethod
    def _platform(cls, value: Any) -> str:
        platform = cls._string(value, "platform").lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", platform):
            raise ApiError(400, "invalid_platform", "Platform is invalid")
        return platform

    @classmethod
    def _notification_text(cls, value: Any, field: str, max_characters: int) -> str:
        text = unicodedata.normalize("NFC", cls._string(value, field)).strip()
        if not 1 <= len(text) <= max_characters:
            raise ApiError(400, f"invalid_{field}", f"{field} is empty or too long")
        if "\x00" in text:
            raise ApiError(400, f"invalid_{field}", f"{field} contains a null character")
        return text

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
            f"{API_PREFIX}/session/upgrade": (30, 300.0),
            f"{API_PREFIX}/computers/login": (30, 300.0),
            f"{API_PREFIX}/computers": (120, 60.0),
            f"{API_PREFIX}/computers/revoke": (30, 300.0),
            f"{API_PREFIX}/computers/logout": (30, 300.0),
            f"{API_PREFIX}/publish": (600, 60.0),
            f"{API_PREFIX}/health": (120, 60.0),
        }
        route_key = path if path in rules else "unknown"
        limit, window = rules.get(path, (30, 60.0))
        # This fixed bucket remains effective even when distributed clients
        # churn the bounded dynamic-key table.
        self._check_rate("global:requests", 3000, 60.0)
        self._check_rate(f"ip:{client_ip}:{route_key}", limit, window)

    @staticmethod
    def _bearer(
        headers: Mapping[str, str], pattern: re.Pattern[str] = APP_TOKEN_PATTERN
    ) -> str:
        authorization = headers.get("authorization", "")
        if len(authorization) > 256:
            raise ApiError(401, "unauthorized", "Valid app token required")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not pattern.fullmatch(parts[1]):
            raise ApiError(401, "unauthorized", "Valid bearer token required")
        return parts[1]

    def _authenticate_app(self, headers: Mapping[str, str]) -> sqlite3.Row:
        token = self._bearer(headers)
        candidate = hashlib.sha256(token.encode("ascii")).digest()
        selected: sqlite3.Row | None = None
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT devices.id AS device_row_id, devices.device_id, devices.user_id, users.username,
                       devices.app_token_hash, devices.private_ready_at,
                       users.private_topic, users.ntfy_subscriber_user
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

    def _authenticate_computer(self, headers: Mapping[str, str]) -> sqlite3.Row:
        token = self._bearer(headers, COMPUTER_TOKEN_PATTERN)
        candidate = hashlib.sha256(token.encode("ascii")).digest()
        selected: sqlite3.Row | None = None
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT computers.id AS computer_row_id, computers.user_id,
                       computers.computer_id, computers.computer_name, computers.platform,
                       computers.token_hash, computers.expires_at, computers.revoked_at,
                       users.username, users.private_topic
                FROM computers JOIN users ON users.id = computers.user_id
                """
            ).fetchall()
        if not rows:
            hmac.compare_digest(candidate, bytes(32))
        for row in rows:
            if hmac.compare_digest(candidate, bytes(row["token_hash"])):
                selected = row
        now = int(time.time())
        if (
            selected is None
            or selected["revoked_at"] is not None
            or (0 < int(selected["expires_at"]) <= now)
            or not selected["private_topic"]
        ):
            raise ApiError(401, "unauthorized", "Valid computer token required")
        return selected

    @staticmethod
    def _new_private_channel() -> ProvisionedNtfyChannel:
        return ProvisionedNtfyChannel(
            subscriber_user=f"awu{secrets.token_hex(12)}",
            topic=f"aw-{secrets.token_hex(16)}",
        )

    def _prepare_private_channel(
        self, user_id: int
    ) -> tuple[ProvisionedNtfyChannel, bool]:
        with closing(self.database.connect()) as connection:
            user = connection.execute(
                """
                SELECT id, username, private_topic, ntfy_subscriber_user
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        if user is None:
            raise ApiError(401, "unauthorized", "Account no longer exists")
        if user["private_topic"] and user["ntfy_subscriber_user"]:
            return (
                ProvisionedNtfyChannel(
                    str(user["ntfy_subscriber_user"]), str(user["private_topic"])
                ),
                False,
            )
        if bool(user["private_topic"]) != bool(user["ntfy_subscriber_user"]):
            raise ApiError(503, "migration_incomplete", "Private channel migration needs repair")

        channel = self._new_private_channel()
        try:
            self.token_manager.provision_channel(channel.subscriber_user, channel.topic)
        except ProvisioningError as exc:
            raise ApiError(
                503, "provisioning_failed", "Private channel could not be provisioned"
            ) from exc
        return channel, True

    @staticmethod
    def _commit_prepared_channel(
        connection: sqlite3.Connection,
        user_id: int,
        channel: ProvisionedNtfyChannel,
        is_new: bool,
        now: int,
    ) -> None:
        if not is_new:
            return
        cursor = connection.execute(
            """
            UPDATE users
            SET private_topic = ?, ntfy_subscriber_user = ?, channel_created_at = ?
            WHERE id = ? AND private_topic IS NULL AND ntfy_subscriber_user IS NULL
            """,
            (channel.topic, channel.subscriber_user, now, user_id),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("channel migration raced or became inconsistent")

    def _client_credentials(
        self,
        username: str,
        device_id: str,
        private_topic: str,
        ntfy_token: str,
        app_token: str,
    ) -> Response:
        public_url = self.config.private_public_url(private_topic)
        parsed = urllib.parse.urlsplit(public_url)
        websocket_scheme = "wss" if parsed.scheme == "https" else "ws"
        websocket_url = urllib.parse.urlunsplit(
            (websocket_scheme, parsed.netloc, parsed.path.rstrip("/") + "/ws", "", "")
        )
        return Response(
            200,
            {
                "api_version": 2,
                "username": username,
                "device_id": device_id,
                "ntfy_url": public_url,
                "ntfy_ws_url": websocket_url,
                "ntfy_topic": private_topic,
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
            channel = self._new_private_channel()
            channel_provisioned = False
            try:
                self.token_manager.provision_channel(channel.subscriber_user, channel.topic)
                channel_provisioned = True
                issued = self.token_manager.issue(channel.subscriber_user, device_id)
            except ProvisioningError as exc:
                # provision_channel performs its own partial rollback. If the
                # subsequent token issue failed, remove the completed channel.
                if channel_provisioned:
                    self.token_manager.rollback_channel(channel)
                raise ApiError(503, "provisioning_failed", "Device credentials could not be provisioned") from exc
            now = int(time.time())
            try:
                with closing(self.database.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    cursor = connection.execute(
                        """
                        INSERT INTO users(
                            username, password_salt, password_hash, created_at,
                            private_topic, ntfy_subscriber_user, channel_created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            username,
                            salt,
                            password_hash,
                            now,
                            channel.topic,
                            channel.subscriber_user,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO devices(
                            user_id, device_id, device_name, app_token_hash,
                            created_at, last_login_at, private_ready_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (cursor.lastrowid, device_id, device_name, app_token_hash, now, now, now),
                    )
                    connection.commit()
            except sqlite3.Error as exc:
                self.token_manager.rollback(channel.subscriber_user, issued)
                self.token_manager.rollback_channel(channel)
                raise ApiError(503, "database_error", "Registration could not be saved") from exc
            self.token_manager.finalize(channel.subscriber_user, issued)
            response = self._client_credentials(
                username, device_id, channel.topic, issued.value, app_token
            )
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

            channel, channel_is_new = self._prepare_private_channel(int(user["id"]))
            app_token = secrets.token_urlsafe(32)
            app_token_hash = hashlib.sha256(app_token.encode("ascii")).digest()
            try:
                issued = self.token_manager.issue(channel.subscriber_user, device_id)
            except ProvisioningError as exc:
                if channel_is_new:
                    self.token_manager.rollback_channel(channel)
                raise ApiError(503, "provisioning_failed", "Device credentials could not be provisioned") from exc
            now = int(time.time())
            try:
                with closing(self.database.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._commit_prepared_channel(
                        connection, int(user["id"]), channel, channel_is_new, now
                    )
                    connection.execute(
                        """
                        INSERT INTO devices(
                            user_id, device_id, device_name, app_token_hash,
                            created_at, last_login_at, private_ready_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(device_id) DO UPDATE SET
                            device_name = excluded.device_name,
                            app_token_hash = excluded.app_token_hash,
                            last_login_at = excluded.last_login_at,
                            private_ready_at = excluded.private_ready_at
                        WHERE devices.user_id = excluded.user_id
                        """,
                        (user["id"], device_id, device_name, app_token_hash, now, now, now),
                    )
                    connection.commit()
            except sqlite3.Error as exc:
                self.token_manager.rollback(channel.subscriber_user, issued)
                if channel_is_new:
                    self.token_manager.rollback_channel(channel)
                raise ApiError(503, "database_error", "Login could not be saved") from exc
            self.token_manager.finalize(channel.subscriber_user, issued)
            # The new private token is durable at this point. Failure to remove
            # the v0.1 shared token must not invalidate the successful login.
            try:
                self.token_manager.revoke_legacy_device(device_id)
            except ProvisioningError:
                self.logger.warning("ntfy_legacy_token_cleanup_failed")
            return self._client_credentials(
                username,
                device_id,
                channel.topic,
                issued.value,
                app_token,
            )

    def _session_upgrade(self, raw: bytes, headers: Mapping[str, str]) -> Response:
        self._decode_object(raw, set())
        app_token = self._bearer(headers)
        device = self._authenticate_app(headers)
        self._check_rate(f"device:{device['device_row_id']}:upgrade", 5, 300.0)
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
            channel, channel_is_new = self._prepare_private_channel(int(device["user_id"]))
            try:
                issued = self.token_manager.issue(
                    channel.subscriber_user, str(device["device_id"])
                )
            except ProvisioningError as exc:
                if channel_is_new:
                    self.token_manager.rollback_channel(channel)
                raise ApiError(
                    503, "provisioning_failed", "Private credentials could not be provisioned"
                ) from exc
            try:
                with closing(self.database.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._commit_prepared_channel(
                        connection,
                        int(device["user_id"]),
                        channel,
                        channel_is_new,
                        int(time.time()),
                    )
                    ready = connection.execute(
                        """
                        UPDATE devices SET private_ready_at = ?
                        WHERE id = ? AND app_token_hash = ?
                        """,
                        (
                            int(time.time()),
                            device["device_row_id"],
                            bytes(device["app_token_hash"]),
                        ),
                    )
                    if ready.rowcount != 1:
                        raise sqlite3.IntegrityError("device session changed during upgrade")
                    connection.commit()
            except sqlite3.Error as exc:
                self.token_manager.rollback(channel.subscriber_user, issued)
                if channel_is_new:
                    self.token_manager.rollback_channel(channel)
                raise ApiError(
                    503, "database_error", "Private channel migration could not be saved"
                ) from exc
            self.token_manager.finalize(channel.subscriber_user, issued)
            try:
                self.token_manager.revoke_legacy_device(str(device["device_id"]))
            except ProvisioningError:
                self.logger.warning("ntfy_legacy_token_cleanup_failed")
            return self._client_credentials(
                str(device["username"]),
                str(device["device_id"]),
                channel.topic,
                issued.value,
                app_token,
            )

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
                # Remove the legacy token first. If that cleanup fails, keep
                # the working private subscription and app session intact.
                self.token_manager.revoke_legacy_device(str(device["device_id"]))
                subscriber_user = device["ntfy_subscriber_user"]
                if subscriber_user:
                    self.token_manager.revoke_device(
                        str(subscriber_user), str(device["device_id"])
                    )
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
        if not device["private_topic"] or device["private_ready_at"] is None:
            raise ApiError(409, "upgrade_required", "Device must upgrade to a private channel")
        self._check_rate("global:test", 10, 60.0)
        self._check_rate(f"device:{device['device_row_id']}:test", 3, 60.0)
        target = device_target_tag(device["device_id"])
        try:
            sequence_id = self.publisher.publish_test(
                str(device["private_topic"]), source, target
            )
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

    def _computer_login(self, raw: bytes) -> Response:
        payload = self._decode_object(
            raw, {"username", "password", "computer_id", "computer_name", "platform"}
        )
        username = self._username(payload["username"])
        password = self._password(payload["password"])
        computer_id = self._computer_id(payload["computer_id"])
        computer_name = self._computer_name(payload["computer_name"])
        platform = self._platform(payload["platform"])
        username_key = hashlib.sha256(username.encode("ascii")).hexdigest()[:20]
        self._check_rate(f"account:{username_key}:computer-login", 10, 300.0)

        with closing(self.database.connect()) as connection:
            user = connection.execute(
                """
                SELECT id, password_salt, password_hash, private_topic, ntfy_subscriber_user
                FROM users WHERE username = ?
                """,
                (username,),
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
        if not user["private_topic"] and not user["ntfy_subscriber_user"]:
            raise ApiError(
                409,
                "app_upgrade_required",
                "Open the updated AgentWatch app before adding a computer",
            )
        if not user["private_topic"] or not user["ntfy_subscriber_user"]:
            raise ApiError(503, "migration_incomplete", "Private channel migration needs repair")

        with self._provisioning_lock:
            with closing(self.database.connect()) as connection:
                unmigrated_devices = connection.execute(
                    "SELECT count(*) FROM devices WHERE user_id = ? AND private_ready_at IS NULL",
                    (user["id"],),
                ).fetchone()[0]
                if unmigrated_devices:
                    raise ApiError(
                        409,
                        "app_upgrade_required",
                        "Open AgentWatch on every registered mobile device before adding a computer",
                    )
                existing = connection.execute(
                    "SELECT user_id, created_at, revoked_at FROM computers WHERE computer_id = ?",
                    (computer_id,),
                ).fetchone()
                if existing and int(existing["user_id"]) != int(user["id"]):
                    raise ApiError(409, "computer_conflict", "Computer belongs to another account")
                active_count = connection.execute(
                    "SELECT count(*) FROM computers WHERE user_id = ? AND revoked_at IS NULL",
                    (user["id"],),
                ).fetchone()[0]
                creates_active_slot = existing is None or existing["revoked_at"] is not None
                if creates_active_slot and active_count >= self.config.max_computers_per_user:
                    raise ApiError(
                        409,
                        "computer_capacity_reached",
                        "Account computer capacity has been reached",
                    )

            computer_token = "awc_" + secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(computer_token.encode("ascii")).digest()
            now = int(time.time())
            expires_at = (
                now + self.config.computer_token_ttl_seconds
                if self.config.computer_token_ttl_seconds
                else 0
            )
            created_at = int(existing["created_at"]) if existing else now
            try:
                with closing(self.database.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        INSERT INTO computers(
                            user_id, computer_id, computer_name, platform, token_hash,
                            created_at, last_login_at, last_seen_at, expires_at, revoked_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        ON CONFLICT(computer_id) DO UPDATE SET
                            computer_name = excluded.computer_name,
                            platform = excluded.platform,
                            token_hash = excluded.token_hash,
                            last_login_at = excluded.last_login_at,
                            last_seen_at = excluded.last_seen_at,
                            expires_at = excluded.expires_at,
                            revoked_at = NULL
                        WHERE computers.user_id = excluded.user_id
                        """,
                        (
                            user["id"],
                            computer_id,
                            computer_name,
                            platform,
                            token_hash,
                            created_at,
                            now,
                            now,
                            expires_at,
                        ),
                    )
                    connection.commit()
            except sqlite3.Error as exc:
                raise ApiError(503, "database_error", "Computer login could not be saved") from exc
        return Response(
            200,
            {
                "api_version": 2,
                "username": username,
                "computer_id": computer_id,
                "computer_name": computer_name,
                "platform": platform,
                "computer_token": computer_token,
                "created_at": created_at,
                "last_seen_at": now,
                "expires_at": expires_at,
            },
        )

    def _computers(self, headers: Mapping[str, str]) -> Response:
        device = self._authenticate_app(headers)
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT computer_id, computer_name, platform, created_at, last_seen_at
                FROM computers
                WHERE user_id = ? AND revoked_at IS NULL
                ORDER BY last_seen_at DESC, id DESC
                """,
                (device["user_id"],),
            ).fetchall()
        return Response(
            200,
            {
                "api_version": 2,
                "computers": [
                    {
                        "computer_id": row["computer_id"],
                        "computer_name": row["computer_name"],
                        "platform": row["platform"],
                        "created_at": row["created_at"],
                        "last_seen_at": row["last_seen_at"],
                    }
                    for row in rows
                ],
            },
        )

    def _computer_revoke(self, raw: bytes, headers: Mapping[str, str]) -> Response:
        payload = self._decode_object(raw, {"computer_id"})
        computer_id = self._computer_id(payload["computer_id"])
        device = self._authenticate_app(headers)
        self._check_rate(f"device:{device['device_row_id']}:computer-revoke", 10, 300.0)
        now = int(time.time())
        replacement_hash = hashlib.sha256(secrets.token_bytes(32)).digest()
        try:
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE computers
                    SET revoked_at = ?, token_hash = ?
                    WHERE user_id = ? AND computer_id = ? AND revoked_at IS NULL
                    """,
                    (now, replacement_hash, device["user_id"], computer_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise ApiError(404, "computer_not_found", "Computer was not found")
                connection.commit()
        except sqlite3.Error as exc:
            raise ApiError(503, "database_error", "Computer could not be revoked") from exc
        return Response(200, {"ok": True})

    def _computer_logout(self, raw: bytes, headers: Mapping[str, str]) -> Response:
        self._decode_object(raw, set())
        computer = self._authenticate_computer(headers)
        self._check_rate(f"computer:{computer['computer_row_id']}:logout", 5, 300.0)
        now = int(time.time())
        replacement_hash = hashlib.sha256(secrets.token_bytes(32)).digest()
        try:
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE computers SET revoked_at = ?, token_hash = ?
                    WHERE id = ? AND token_hash = ? AND revoked_at IS NULL
                    """,
                    (
                        now,
                        replacement_hash,
                        computer["computer_row_id"],
                        bytes(computer["token_hash"]),
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise ApiError(401, "session_replaced", "Computer session is no longer current")
                connection.commit()
        except sqlite3.Error as exc:
            raise ApiError(503, "database_error", "Computer logout could not be saved") from exc
        return Response(200, {"ok": True})

    def _publish(self, raw: bytes, headers: Mapping[str, str]) -> Response:
        payload = self._decode_object(
            raw, {"event_id", "source", "title", "body"}, {"priority"}
        )
        event_id = self._string(payload["event_id"], "event_id")
        if not SEQUENCE_ID_PATTERN.fullmatch(event_id):
            raise ApiError(400, "invalid_event_id", "Event ID is invalid")
        source = self._string(payload["source"], "source").lower()
        if source not in TEST_SOURCES:
            raise ApiError(400, "invalid_source", "source is invalid")
        title = self._notification_text(payload["title"], "title", 160)
        body = self._notification_text(payload["body"], "body", 3200)
        priority = self._string(payload.get("priority", "default"), "priority").lower()
        if priority not in NTFY_PRIORITIES:
            raise ApiError(400, "invalid_priority", "priority is invalid")
        computer = self._authenticate_computer(headers)
        self._check_rate("global:publish", 1000, 60.0)
        self._check_rate(f"user:{computer['user_id']}:publish", 120, 60.0)
        self._check_rate(f"computer:{computer['computer_row_id']}:publish", 120, 60.0)
        now = int(time.time())
        envelope = {
            "schema": "agentwatch_event_v2",
            "event_id": event_id,
            "source": source,
            "title": title,
            "body": body,
            "computer_id": computer["computer_id"],
            "computer_name": computer["computer_name"],
            "sent_at": now,
        }
        encoded = json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_NTFY_MESSAGE_BYTES:
            raise ApiError(413, "message_too_large", "Notification is too large")
        try:
            self.publisher.publish_event(
                str(computer["private_topic"]),
                event_id,
                source,
                title,
                encoded,
                priority,
            )
        except PublishError as exc:
            raise ApiError(502, "publish_failed", "Notification could not be published") from exc
        try:
            with closing(self.database.connect()) as connection:
                connection.execute(
                    "UPDATE computers SET last_seen_at = ? WHERE id = ?",
                    (now, computer["computer_row_id"]),
                )
                connection.commit()
        except sqlite3.Error:
            # The message was already accepted by ntfy. Returning an error here
            # would encourage a duplicate retry for mere inventory metadata.
            self.logger.warning("computer_last_seen_update_failed")
        return Response(202, {"ok": True, "event_id": event_id})

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
        if method == "GET" and path == f"{API_PREFIX}/computers":
            return self._computers(headers)
        if method != "POST":
            raise ApiError(405, "method_not_allowed", "Method is not allowed")
        if path == f"{API_PREFIX}/register":
            return self._register(raw)
        if path == f"{API_PREFIX}/login":
            return self._login(raw)
        if path == f"{API_PREFIX}/session/upgrade":
            return self._session_upgrade(raw, headers)
        if path == f"{API_PREFIX}/logout":
            return self._logout(raw, headers)
        if path == f"{API_PREFIX}/test":
            return self._test(raw, headers)
        if path == f"{API_PREFIX}/ack":
            return self._ack(raw, headers)
        if path == f"{API_PREFIX}/computers/login":
            return self._computer_login(raw)
        if path == f"{API_PREFIX}/computers/revoke":
            return self._computer_revoke(raw, headers)
        if path == f"{API_PREFIX}/computers/logout":
            return self._computer_logout(raw, headers)
        if path == f"{API_PREFIX}/publish":
            return self._publish(raw, headers)
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
