#!/usr/bin/env python3
"""Shared AgentWatch computer identity, credential, and API primitives.

Only the short-lived account password crosses this module during an interactive
login request.  It is never persisted.  The resulting computer token is stored
in the operating system credential facility where practical, with a 0600 file
fallback on Linux hosts without Secret Service.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import platform as platform_module
import shutil
import stat as stat_module
import subprocess
import tempfile
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid


PRODUCT_NAME = "AgentWatch"
CLIENT_USER_AGENT = "agentwatch-computer/0.3.0"
CRYPTPROTECT_UI_FORBIDDEN = 0x1
API_VERSION = 2
DEFAULT_API_BASE = "https://64.90.8.184:9444/agentwatch/api/v1"
DEFAULT_CONFIG_DIR = "~/.codex-watch-notifier"
MACHINE_FILE_NAME = "machine.json"
SETTINGS_FILE_NAME = "settings.json"
DELIVERY_SETTINGS_VERSION = 1
ALLOWED_DELIVERY_MODES = frozenset({"bark", "agentwatch", "both"})
LINUX_TOKEN_FILE_NAME = "computer-token"
LINUX_TOKEN_BACKEND_FILE_NAME = "computer-token-backend"
LINUX_BACKEND_SECRET_SERVICE = "secret-service"
LINUX_BACKEND_PRIVATE_FILE = "private-file"
LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW = "private-file-secret-shadow"
LINUX_BACKENDS = frozenset(
    {
        LINUX_BACKEND_SECRET_SERVICE,
        LINUX_BACKEND_PRIVATE_FILE,
        LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW,
    }
)
WINDOWS_TOKEN_FILE_NAME = "computer-token.dpapi"
KEYCHAIN_SERVICE = "io.github.taotaoxu7447.agentwatch.computer"
SECRET_TOOL_LABEL = "AgentWatch computer token"
ALLOWED_SOURCES = {"codex", "zcode", "kimi", "grok", "claude", "other"}


class AgentWatchError(RuntimeError):
    """Safe, user-facing error without credentials or response internals."""


class ApiError(AgentWatchError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def config_dir() -> Path:
    configured = os.getenv("AGENTWATCH_CONFIG_DIR", DEFAULT_CONFIG_DIR)
    # Keep the lexical path so installer safety checks can still detect a
    # symlink or Windows junction in AGENTWATCH_CONFIG_DIR. Resolving here would
    # erase that evidence before any mutating operation validates the path.
    expanded = os.path.expandvars(os.path.expanduser(configured))
    return Path(os.path.abspath(expanded))


def api_base() -> str:
    return os.getenv("AGENTWATCH_API_BASE", DEFAULT_API_BASE).strip().rstrip("/")


def path_has_link_component(path: Path) -> bool:
    """Return whether an untrusted descendant contains a symlink/junction.

    The user's home and the platform temporary directory are treated as trusted
    roots so macOS aliases such as `/var -> /private/var` do not reject normal
    temporary files. Every component below the longest matching trusted root is
    checked without resolving the final target. Paths elsewhere are checked
    from their filesystem anchor.
    """
    target = Path(os.path.abspath(path))
    trusted_roots: list[Path] = []
    for raw_root in (Path.home(), Path(tempfile.gettempdir())):
        root = Path(os.path.abspath(raw_root))
        try:
            target.relative_to(root)
        except ValueError:
            continue
        trusted_roots.append(root)
    if trusted_roots:
        boundary = max(trusted_roots, key=lambda candidate: len(candidate.parts))
    else:
        boundary = Path(target.anchor)

    try:
        relative = target.relative_to(boundary)
    except ValueError:
        return True
    current = boundary
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for part in relative.parts:
        current = current / part
        if not os.path.lexists(current):
            continue
        try:
            metadata = current.lstat()
        except OSError:
            return True
        is_junction = bool(
            hasattr(os.path, "isjunction") and os.path.isjunction(current)
        )
        is_reparse = bool(
            getattr(metadata, "st_file_attributes", 0) & reparse_flag
        )
        if stat_module.S_ISLNK(metadata.st_mode) or is_junction or is_reparse:
            return True
    return False


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, mode)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _platform_slug(system_name: str | None = None) -> str:
    normalized = (system_name or platform_module.system()).lower()
    return {
        "darwin": "macos",
        "windows": "windows",
        "linux": "linux",
    }.get(normalized, normalized or "unknown")


def _normalized_machine(raw: Any, *, existing_identity: bool) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise AgentWatchError(
            "AgentWatch machine.json must be a JSON object; refusing to replace computer identity"
        )

    computer_id_value = raw.get("computer_id")
    computer_id = computer_id_value.strip() if isinstance(computer_id_value, str) else ""
    if existing_identity:
        if not computer_id:
            raise AgentWatchError(
                "AgentWatch machine.json is missing a valid computer_id; refusing to rotate computer identity"
            )
        try:
            parsed_id = str(uuid.UUID(computer_id))
        except ValueError as exc:
            raise AgentWatchError(
                "AgentWatch machine.json has an invalid computer_id; refusing to rotate computer identity"
            ) from exc
    else:
        parsed_id = str(uuid.uuid4())

    computer_name_value = raw.get("computer_name")
    if computer_name_value is not None and not isinstance(computer_name_value, str):
        raise AgentWatchError("AgentWatch machine.json computer_name must be a string")
    computer_name = (computer_name_value or "").strip()
    if not computer_name:
        computer_name = platform_module.node().strip() or f"{PRODUCT_NAME}-{parsed_id[:8]}"
    platform_name = _platform_slug()
    username_value = raw.get("username")
    if username_value is not None and not isinstance(username_value, str):
        raise AgentWatchError("AgentWatch machine.json username must be a string")
    username = (username_value or "").strip()
    machine = {
        "computer_id": parsed_id,
        "computer_name": computer_name[:120],
        "platform": platform_name[:32],
    }
    if username:
        machine["username"] = username[:120]
    return machine


def load_machine(config_root: Path | None = None) -> dict[str, str] | None:
    """Read and validate an existing computer identity without creating or rewriting it."""
    root = config_root or config_dir()
    path = root / MACHINE_FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise AgentWatchError(
            "AgentWatch machine.json is unreadable or invalid; refusing to replace computer identity"
        ) from exc
    return _normalized_machine(raw, existing_identity=True)


def load_or_create_machine(config_root: Path | None = None) -> dict[str, str]:
    root = config_root or config_dir()
    path = root / MACHINE_FILE_NAME
    machine = load_machine(root)
    if machine is None:
        machine = _normalized_machine({}, existing_identity=False)

    serialized = (json.dumps(machine, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    existing = None
    try:
        existing = path.read_bytes()
    except OSError:
        pass
    if existing != serialized:
        atomic_write(path, serialized)
    return machine


def save_machine_account(machine: dict[str, str], username: str, config_root: Path | None = None) -> None:
    root = config_root or config_dir()
    updated = {
        "computer_id": machine["computer_id"],
        "computer_name": machine["computer_name"],
        "platform": machine["platform"],
        "username": username.strip()[:120],
    }
    atomic_write(
        root / MACHINE_FILE_NAME,
        (json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def load_delivery_mode(config_root: Path | None = None) -> str | None:
    """Load the non-secret receiver selection without guessing a default."""
    root = config_root or config_dir()
    path = root / SETTINGS_FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise AgentWatchError("AgentWatch settings.json is unreadable or invalid") from exc
    if not isinstance(raw, dict) or raw.get("version") != DELIVERY_SETTINGS_VERSION:
        raise AgentWatchError("AgentWatch settings.json has an unsupported format")
    mode = raw.get("delivery_mode")
    if not isinstance(mode, str) or mode not in ALLOWED_DELIVERY_MODES:
        raise AgentWatchError("delivery_mode must be bark, agentwatch, or both")
    return mode


def save_delivery_mode(delivery_mode: str, config_root: Path | None = None) -> None:
    """Persist only the non-secret receiver choice using the shared safe writer."""
    if delivery_mode not in ALLOWED_DELIVERY_MODES:
        raise AgentWatchError("delivery_mode must be bark, agentwatch, or both")
    root = config_root or config_dir()
    payload = {
        "version": DELIVERY_SETTINGS_VERSION,
        "delivery_mode": delivery_mode,
    }
    atomic_write(
        root / SETTINGS_FILE_NAME,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
    )


def infer_delivery_mode(bark_configured: bool, agentwatch_authenticated: bool) -> str | None:
    """Infer an existing installation, but never guess when neither receiver exists."""
    if bark_configured and agentwatch_authenticated:
        return "both"
    if bark_configured:
        return "bark"
    if agentwatch_authenticated:
        return "agentwatch"
    return None


def resolve_delivery(
    delivery_mode: str | None,
    bark_configured: bool,
    agentwatch_authenticated: bool,
) -> dict[str, Any]:
    """Resolve desired receiver mode into stable status and service semantics."""
    if delivery_mode is not None and delivery_mode not in ALLOWED_DELIVERY_MODES:
        raise AgentWatchError("delivery_mode must be bark, agentwatch, or both")
    desired = {
        "bark": ["bark"],
        "agentwatch": ["agentwatch"],
        "both": ["bark", "agentwatch"],
    }.get(delivery_mode, [])
    readiness = {
        "bark": bool(bark_configured),
        "agentwatch": bool(agentwatch_authenticated),
    }
    effective = [channel for channel in desired if readiness[channel]]
    missing = [channel for channel in desired if not readiness[channel]]
    operational = bool(effective)
    fully_configured = bool(desired) and not missing
    return {
        "delivery_mode": delivery_mode,
        "bark_configured": bool(bark_configured),
        "agentwatch_authenticated": bool(agentwatch_authenticated),
        "effective_channels": effective,
        "missing_channels": missing,
        "operational": operational,
        "fully_configured": fully_configured,
        "degraded": operational and not fully_configured,
        "delivery_mode_required": delivery_mode is None,
    }


ERR_SEC_SUCCESS = 0
ERR_SEC_USER_CANCELED = -128
ERR_SEC_AUTH_FAILED = -25293
ERR_SEC_DUPLICATE_ITEM = -25299
ERR_SEC_ITEM_NOT_FOUND = -25300
ERR_SEC_INTERACTION_NOT_ALLOWED = -25308
ERR_SEC_MISSING_ENTITLEMENT = -34018


class _MacOSStatusError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(str(status))


class _MacOSSecurityNative:
    """Small ctypes binding for generic-password Keychain operations."""

    def __init__(self) -> None:
        try:
            self.security = ctypes.CDLL(
                "/System/Library/Frameworks/Security.framework/Security"
            )
            self.core_foundation = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        except OSError as exc:
            raise AgentWatchError("macOS Security.framework is unavailable") from exc

        os_status = ctypes.c_int32
        uint32 = ctypes.c_uint32
        void_pointer = ctypes.c_void_p

        self.security.SecKeychainAddGenericPassword.argtypes = [
            void_pointer,
            uint32,
            ctypes.c_char_p,
            uint32,
            ctypes.c_char_p,
            uint32,
            void_pointer,
            ctypes.POINTER(void_pointer),
        ]
        self.security.SecKeychainAddGenericPassword.restype = os_status
        self.security.SecKeychainFindGenericPassword.argtypes = [
            void_pointer,
            uint32,
            ctypes.c_char_p,
            uint32,
            ctypes.c_char_p,
            ctypes.POINTER(uint32),
            ctypes.POINTER(void_pointer),
            ctypes.POINTER(void_pointer),
        ]
        self.security.SecKeychainFindGenericPassword.restype = os_status
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [
            void_pointer,
            void_pointer,
            uint32,
            void_pointer,
        ]
        self.security.SecKeychainItemModifyAttributesAndData.restype = os_status
        self.security.SecKeychainItemDelete.argtypes = [void_pointer]
        self.security.SecKeychainItemDelete.restype = os_status
        self.security.SecKeychainItemFreeContent.argtypes = [void_pointer, void_pointer]
        self.security.SecKeychainItemFreeContent.restype = os_status
        self.security.SecKeychainGetUserInteractionAllowed.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte)
        ]
        self.security.SecKeychainGetUserInteractionAllowed.restype = os_status
        self.security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
        self.security.SecKeychainSetUserInteractionAllowed.restype = os_status
        self.core_foundation.CFRelease.argtypes = [void_pointer]
        self.core_foundation.CFRelease.restype = None

    @contextmanager
    def _without_user_interaction(self) -> Any:
        previous = ctypes.c_ubyte(0)
        status = int(self.security.SecKeychainGetUserInteractionAllowed(ctypes.byref(previous)))
        if status != ERR_SEC_SUCCESS:
            raise _MacOSStatusError(status)
        status = int(self.security.SecKeychainSetUserInteractionAllowed(0))
        if status != ERR_SEC_SUCCESS:
            raise _MacOSStatusError(status)
        try:
            yield
        finally:
            restore_status = int(
                self.security.SecKeychainSetUserInteractionAllowed(previous.value)
            )
            if restore_status != ERR_SEC_SUCCESS:
                raise _MacOSStatusError(restore_status)

    @staticmethod
    def _encoded(value: str) -> bytes:
        return value.encode("utf-8")

    @staticmethod
    def _secret_pointer(secret: bytearray) -> tuple[Any, ctypes.c_void_p]:
        buffer = (ctypes.c_ubyte * len(secret)).from_buffer(secret)
        return buffer, ctypes.cast(buffer, ctypes.c_void_p)

    def add(self, service: str, account: str, secret: bytearray) -> int:
        service_bytes = self._encoded(service)
        account_bytes = self._encoded(account)
        buffer, secret_pointer = self._secret_pointer(secret)
        try:
            with self._without_user_interaction():
                status = self.security.SecKeychainAddGenericPassword(
                    None,
                    len(service_bytes),
                    service_bytes,
                    len(account_bytes),
                    account_bytes,
                    len(secret),
                    secret_pointer,
                    None,
                )
        except _MacOSStatusError as exc:
            status = exc.status
        del buffer
        return int(status)

    def read(self, service: str, account: str) -> tuple[int, bytearray | None]:
        service_bytes = self._encoded(service)
        account_bytes = self._encoded(account)
        password_length = ctypes.c_uint32(0)
        password_data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        try:
            with self._without_user_interaction():
                status = int(
                    self.security.SecKeychainFindGenericPassword(
                        None,
                        len(service_bytes),
                        service_bytes,
                        len(account_bytes),
                        account_bytes,
                        ctypes.byref(password_length),
                        ctypes.byref(password_data),
                        ctypes.byref(item),
                    )
                )
        except _MacOSStatusError as exc:
            status = exc.status
        if status != ERR_SEC_SUCCESS:
            # SecKeychainFindGenericPassword may have succeeded even when
            # restoring the process interaction setting failed. Release any
            # objects returned by that successful call before surfacing the
            # restore error.
            if password_data.value:
                self.security.SecKeychainItemFreeContent(None, password_data)
            if item.value:
                self.core_foundation.CFRelease(item)
            return status, None
        try:
            if not password_data.value and password_length.value:
                return ERR_SEC_AUTH_FAILED, None
            value = bytearray(ctypes.string_at(password_data, password_length.value))
            return status, value
        finally:
            if password_data.value:
                self.security.SecKeychainItemFreeContent(None, password_data)
            if item.value:
                self.core_foundation.CFRelease(item)

    def _find_item(self, service: str, account: str) -> tuple[int, ctypes.c_void_p]:
        service_bytes = self._encoded(service)
        account_bytes = self._encoded(account)
        item = ctypes.c_void_p()
        try:
            with self._without_user_interaction():
                status = int(
                    self.security.SecKeychainFindGenericPassword(
                        None,
                        len(service_bytes),
                        service_bytes,
                        len(account_bytes),
                        account_bytes,
                        None,
                        None,
                        ctypes.byref(item),
                    )
                )
        except _MacOSStatusError as exc:
            status = exc.status
        if status != ERR_SEC_SUCCESS and item.value:
            # As above, a failed interaction-setting restore can follow a
            # successful lookup. The caller only owns an item on success.
            self.core_foundation.CFRelease(item)
            item = ctypes.c_void_p()
        return status, item

    def update(self, service: str, account: str, secret: bytearray) -> int:
        status, item = self._find_item(service, account)
        if status != ERR_SEC_SUCCESS:
            return status
        try:
            buffer, secret_pointer = self._secret_pointer(secret)
            try:
                with self._without_user_interaction():
                    status = int(
                        self.security.SecKeychainItemModifyAttributesAndData(
                            item,
                            None,
                            len(secret),
                            secret_pointer,
                        )
                    )
            except _MacOSStatusError as exc:
                status = exc.status
            del buffer
            return status
        finally:
            if item.value:
                self.core_foundation.CFRelease(item)

    def delete(self, service: str, account: str) -> int:
        status, item = self._find_item(service, account)
        if status != ERR_SEC_SUCCESS:
            return status
        try:
            try:
                with self._without_user_interaction():
                    return int(self.security.SecKeychainItemDelete(item))
            except _MacOSStatusError as exc:
                return exc.status
        finally:
            if item.value:
                self.core_foundation.CFRelease(item)


class MacOSKeychain:
    """Prompt-free Keychain facade with safe OSStatus mapping."""

    def __init__(self, native: Any | None = None) -> None:
        self.native = native or _MacOSSecurityNative()

    @staticmethod
    def _raise(operation: str, status: int) -> None:
        details = {
            ERR_SEC_USER_CANCELED: "operation was cancelled",
            ERR_SEC_AUTH_FAILED: "authentication failed",
            ERR_SEC_INTERACTION_NOT_ALLOWED: "the login Keychain is locked or unavailable",
            ERR_SEC_MISSING_ENTITLEMENT: "the process is not permitted to use Keychain",
        }
        detail = details.get(status, f"OSStatus {status}")
        raise AgentWatchError(f"macOS Keychain {operation} failed: {detail}")

    @staticmethod
    def _zero(value: bytearray | None) -> None:
        if value is None:
            return
        for index in range(len(value)):
            value[index] = 0

    def save(self, account: str, token: str) -> None:
        secret = bytearray(token.encode("utf-8"))
        try:
            status = self.native.add(KEYCHAIN_SERVICE, account, secret)
            if status == ERR_SEC_DUPLICATE_ITEM:
                status = self.native.update(KEYCHAIN_SERVICE, account, secret)
            if status != ERR_SEC_SUCCESS:
                self._raise("save", status)
        finally:
            self._zero(secret)

    def load(self, account: str) -> str | None:
        status, secret = self.native.read(KEYCHAIN_SERVICE, account)
        if status == ERR_SEC_ITEM_NOT_FOUND:
            return None
        if status != ERR_SEC_SUCCESS:
            self._zero(secret)
            self._raise("read", status)
        if secret is None:
            raise AgentWatchError("macOS Keychain computer token is missing")
        try:
            value = secret.decode("utf-8").strip()
        except UnicodeError:
            raise AgentWatchError("macOS Keychain computer token is invalid") from None
        finally:
            self._zero(secret)
        return value or None

    def delete(self, account: str) -> None:
        status = self.native.delete(KEYCHAIN_SERVICE, account)
        if status not in {ERR_SEC_SUCCESS, ERR_SEC_ITEM_NOT_FOUND}:
            self._raise("delete", status)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _bytes_to_blob(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _dpapi_protect(value: bytes) -> bytes:
    input_blob, input_buffer = _bytes_to_blob(value)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        ctypes.c_wchar_p(PRODUCT_NAME),
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise AgentWatchError("Windows DPAPI could not protect the computer token")
    # A ctypes pointer does not own its target. Keep input_buffer referenced
    # until CryptProtectData returns so the input blob cannot dangle.
    del input_buffer
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _dpapi_unprotect(value: bytes) -> bytes:
    input_blob, input_buffer = _bytes_to_blob(value)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise AgentWatchError("Windows DPAPI could not read the computer token")
    del input_buffer
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


class ComputerTokenStore:
    """OS credential wrapper. No method prints or logs token material."""

    def __init__(
        self,
        computer_id: str,
        config_root: Path | None = None,
        system_name: str | None = None,
        which: Callable[[str], str | None] = shutil.which,
        macos_keychain: MacOSKeychain | None = None,
    ) -> None:
        self.computer_id = computer_id
        self.config_root = config_root or config_dir()
        self.system_name = system_name or platform_module.system()
        self.which = which
        self.macos_keychain = macos_keychain

    def _macos_backend(self) -> MacOSKeychain:
        if self.macos_keychain is None:
            self.macos_keychain = MacOSKeychain()
        return self.macos_keychain

    def load(self) -> str | None:
        try:
            return self.load_strict()
        except (OSError, subprocess.SubprocessError, AgentWatchError, ValueError):
            return None

    def load_strict(self, *, migrate: bool = True) -> str | None:
        """Load a token, optionally persisting legacy backend discovery."""
        if self.system_name == "Darwin":
            return self._macos_load()
        if self.system_name == "Windows":
            return self._windows_load_strict()
        if self.system_name == "Linux":
            backend = self._linux_backend_load_strict()
            secret_tool = self.which("secret-tool")
            if backend in {
                LINUX_BACKEND_SECRET_SERVICE,
                LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW,
            } and not secret_tool:
                raise AgentWatchError(
                    "recorded Linux Secret Service credential backend is unavailable"
                )
            if backend == LINUX_BACKEND_SECRET_SERVICE:
                return self._linux_secret_load(strict=True)
            if backend in {
                LINUX_BACKEND_PRIVATE_FILE,
                LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW,
            }:
                return self._file_load_strict()

            # Legacy installs predate the backend marker.  A fallback file is
            # authoritative; when Secret Service is available it may still
            # contain an older token and is recorded as a shadow that must be
            # cleared before a destructive logout can finish.
            if os.path.lexists(self._fallback_path()):
                value = self._file_load_strict()
                if migrate:
                    self._linux_backend_save(
                        LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW
                        if secret_tool
                        else LINUX_BACKEND_PRIVATE_FILE
                    )
                return value
            if secret_tool:
                value = self._linux_secret_load(strict=True)
                if value is not None and migrate:
                    self._linux_backend_save(LINUX_BACKEND_SECRET_SERVICE)
                return value
        return self._file_load_strict()

    def load_read_only(self) -> str | None:
        """Strictly inspect credentials without creating a backend marker."""
        return self.load_strict(migrate=False)

    def save(self, token: str) -> None:
        normalized = token.strip()
        if not normalized:
            raise AgentWatchError("server returned an empty computer token")
        if self.system_name == "Darwin":
            self._macos_save(normalized)
        elif self.system_name == "Windows":
            self._windows_save(normalized)
        elif self.system_name == "Linux":
            secret_tool = self.which("secret-tool")
            # Validate any existing marker before changing credential state.
            previous_backend = self._linux_backend_load_strict()
            if secret_tool:
                try:
                    self._linux_secret_save(normalized)
                except (OSError, subprocess.SubprocessError, AgentWatchError):
                    self._file_save(normalized)
                    self._linux_backend_save(
                        LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW
                    )
                else:
                    try:
                        self._linux_backend_save(LINUX_BACKEND_SECRET_SERVICE)
                    except (OSError, AgentWatchError):
                        # Preserve a retrievable copy if the non-secret marker
                        # cannot be committed after Secret Service accepted the
                        # new token.  The original exception still prevents a
                        # caller from reporting a completed login.
                        self._file_save(normalized)
                        raise
                    self._unlink_linux_path(self._fallback_path())
            else:
                if previous_backend in {
                    LINUX_BACKEND_SECRET_SERVICE,
                    LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW,
                }:
                    raise AgentWatchError(
                        "recorded Linux Secret Service credential backend is unavailable"
                    )
                self._file_save(normalized)
                self._linux_backend_save(LINUX_BACKEND_PRIVATE_FILE)
        else:
            self._file_save(normalized)

    def delete(self) -> None:
        local_paths: tuple[Path, ...]
        if self.system_name == "Darwin":
            self._macos_backend().delete(self.computer_id)
            local_paths = ()
        elif self.system_name == "Linux":
            backend = self._linux_backend_load_strict()
            secret_tool = self.which("secret-tool")
            if backend in {
                LINUX_BACKEND_SECRET_SERVICE,
                LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW,
            } and not secret_tool:
                raise AgentWatchError(
                    "recorded Linux Secret Service credential backend is unavailable"
                )
            # With no marker, preserve the legacy behavior: an available
            # Secret Service is cleared before the fallback file.  A marked
            # private-file backend is known not to have a Secret Service copy.
            if backend in {
                LINUX_BACKEND_SECRET_SERVICE,
                LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW,
            } or (backend is None and secret_tool):
                self._linux_secret_delete()
            local_paths = (
                self._fallback_path(),
                self._linux_backend_path(),
            )
        elif self.system_name == "Windows":
            local_paths = (self._windows_path(),)
        else:
            local_paths = (self._fallback_path(),)
        for path in local_paths:
            if path_has_link_component(path):
                raise AgentWatchError("computer credential path must not contain a symlink or junction")
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def backend_name(self) -> str:
        if self.system_name == "Darwin":
            return "macOS Keychain"
        if self.system_name == "Windows":
            return "Windows DPAPI"
        if self.system_name == "Linux":
            try:
                backend = self._linux_backend_load_strict()
            except (OSError, AgentWatchError):
                return "unavailable Linux credential backend"
            if backend == LINUX_BACKEND_SECRET_SERVICE:
                return (
                    "Linux Secret Service"
                    if self.which("secret-tool")
                    else "unavailable Linux Secret Service"
                )
            if backend == LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW:
                return (
                    "0600 private file"
                    if self.which("secret-tool")
                    else "unavailable Linux Secret Service shadow"
                )
            if backend == LINUX_BACKEND_PRIVATE_FILE or self._fallback_path().exists():
                return "0600 private file"
            if self.which("secret-tool"):
                return "Linux Secret Service"
        return "0600 private file"

    def _macos_load(self) -> str | None:
        return self._macos_backend().load(self.computer_id)

    def _macos_save(self, token: str) -> None:
        self._macos_backend().save(self.computer_id, token)

    def _linux_backend_path(self) -> Path:
        return self.config_root / LINUX_TOKEN_BACKEND_FILE_NAME

    def _linux_backend_load_strict(self) -> str | None:
        path = self._linux_backend_path()
        if path_has_link_component(path):
            raise AgentWatchError(
                "Linux credential backend marker path must not contain a symlink or junction"
            )
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if value not in LINUX_BACKENDS:
            raise AgentWatchError("Linux credential backend marker is invalid")
        return value

    def _linux_backend_save(self, backend: str) -> None:
        if backend not in LINUX_BACKENDS:
            raise AgentWatchError("refusing invalid Linux credential backend marker")
        path = self._linux_backend_path()
        if path_has_link_component(path):
            raise AgentWatchError(
                "Linux credential backend marker path must not contain a symlink or junction"
            )
        atomic_write(path, (backend + "\n").encode("ascii"), mode=0o600)

    def _linux_secret_delete(self) -> None:
        completed = subprocess.run(
            [
                "secret-tool",
                "clear",
                "service",
                KEYCHAIN_SERVICE,
                "computer_id",
                self.computer_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode not in {0, 1} or (
            completed.returncode == 1 and completed.stderr.strip()
        ):
            raise AgentWatchError("Linux Secret Service could not clear the computer token")

    @staticmethod
    def _unlink_linux_path(path: Path) -> None:
        if path_has_link_component(path):
            raise AgentWatchError(
                "computer credential path must not contain a symlink or junction"
            )
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _linux_secret_load(self, *, strict: bool = False) -> str | None:
        completed = subprocess.run(
            ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE, "computer_id", self.computer_id],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            if strict and (completed.returncode != 1 or completed.stderr.strip()):
                raise AgentWatchError("Linux Secret Service was unavailable")
            return None
        value = completed.stdout.strip()
        return value or None

    def _linux_secret_save(self, token: str) -> None:
        completed = subprocess.run(
            [
                "secret-tool",
                "store",
                f"--label={SECRET_TOOL_LABEL}",
                "service",
                KEYCHAIN_SERVICE,
                "computer_id",
                self.computer_id,
            ],
            input=token,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise AgentWatchError("Linux Secret Service was unavailable")

    def _fallback_path(self) -> Path:
        return self.config_root / LINUX_TOKEN_FILE_NAME

    def _file_load(self) -> str | None:
        try:
            return self._file_load_strict()
        except (OSError, UnicodeError, AgentWatchError):
            return None

    def _file_load_strict(self) -> str | None:
        path = self._fallback_path()
        if path_has_link_component(path):
            raise AgentWatchError("computer credential path must not contain a symlink or junction")
        try:
            if path.stat().st_mode & 0o077:
                raise AgentWatchError("computer credential file permissions are unsafe")
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _file_save(self, token: str) -> None:
        path = self._fallback_path()
        if path_has_link_component(path):
            raise AgentWatchError(
                "computer credential path must not contain a symlink or junction"
            )
        atomic_write(path, (token + "\n").encode("utf-8"))

    def _windows_path(self) -> Path:
        return self.config_root / WINDOWS_TOKEN_FILE_NAME

    def _windows_load(self) -> str | None:
        try:
            return self._windows_load_strict()
        except (OSError, ValueError, UnicodeError, AgentWatchError):
            return None

    def _windows_load_strict(self) -> str | None:
        path = self._windows_path()
        if path_has_link_component(path):
            raise AgentWatchError("computer credential path must not contain a symlink or junction")
        try:
            encoded = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            protected = base64.b64decode(encoded, validate=True)
            value = _dpapi_unprotect(protected).decode("utf-8").strip()
        except (ValueError, UnicodeError, AgentWatchError) as exc:
            raise AgentWatchError("Windows DPAPI computer token is unreadable") from exc
        return value or None

    def _windows_save(self, token: str) -> None:
        protected = _dpapi_protect(token.encode("utf-8"))
        atomic_write(self._windows_path(), base64.b64encode(protected) + b"\n")


class AgentWatchApi:
    def __init__(self, base_url: str | None = None, timeout: float = 12.0) -> None:
        self.base_url = (base_url or api_base()).strip().rstrip("/")
        self.timeout = timeout
        if not self.base_url.startswith("https://"):
            raise AgentWatchError("AgentWatch API URL must use HTTPS")

    def _post(self, path: str, payload: dict[str, Any], token: str | None = None) -> tuple[int, dict[str, Any]]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": CLIENT_USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 200))
                body = response.read(65536)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read(65536)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            detail = str(reason or "network request failed")
            raise AgentWatchError(f"cannot reach AgentWatch server: {detail}") from None

        try:
            decoded = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeError, json.JSONDecodeError):
            decoded = {}
        if not isinstance(decoded, dict):
            decoded = {}
        if not 200 <= status < 300:
            code = str(decoded.get("error") or f"http_{status}")[:80]
            message = str(decoded.get("message") or "request was rejected")[:300]
            raise ApiError(status, code, message)
        return status, decoded

    def login(self, username: str, password: str, machine: dict[str, str]) -> dict[str, Any]:
        payload = {
            "username": username,
            "password": password,
            "computer_id": machine["computer_id"],
            "computer_name": machine["computer_name"],
            "platform": machine["platform"],
        }
        _, response = self._post("/computers/login", payload)
        token = str(response.get("computer_token") or "").strip()
        if not token:
            raise AgentWatchError("AgentWatch server did not return a computer token")
        return response

    def publish(
        self,
        token: str,
        *,
        event_id: str,
        source: str,
        title: str,
        body: str,
        priority: str | None = None,
    ) -> dict[str, Any]:
        normalized_source = source.lower().strip()
        if normalized_source not in ALLOWED_SOURCES:
            normalized_source = "other"
        payload: dict[str, Any] = {
            "event_id": event_id,
            "source": normalized_source,
            "title": title,
            "body": body,
        }
        if priority:
            payload["priority"] = priority
        status, response = self._post("/publish", payload, token=token)
        if status != 202:
            raise AgentWatchError(f"unexpected publish response status {status}")
        return response

    def logout(self, token: str) -> dict[str, Any]:
        _, response = self._post("/computers/logout", {}, token=token)
        return response

    def health(self) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + "/health",
            headers={"Accept": "application/json", "User-Agent": CLIENT_USER_AGENT},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 200))
                body = response.read(65536)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            raise AgentWatchError("cannot reach AgentWatch server health endpoint") from exc
        if status != 200:
            raise AgentWatchError(f"AgentWatch server health returned HTTP {status}")
        try:
            decoded = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeError, json.JSONDecodeError):
            decoded = {}
        return decoded if isinstance(decoded, dict) else {}


def stable_event_id(event: dict[str, Any], computer_id: str) -> str:
    # ntfy 2.26.3 applies its topic-name contract to X-Sequence-ID as well:
    # ASCII letters, digits, dash, or underscore, with a 64-character maximum.
    # Preserve existing short protocol-safe stable IDs so retries keep the same
    # identity. Hash only missing or incompatible values to avoid lossy
    # punctuation stripping and accidental collisions.
    machine = "".join(
        character.lower()
        for character in computer_id
        if character.isascii() and character.isalnum()
    )[:32]
    if not machine:
        machine = hashlib_sha256(computer_id.encode("utf-8"))[:12]

    prefix = f"aw{API_VERSION}_{machine}_"
    available = 64 - len(prefix)
    if available < 1:
        # UUID-backed machine IDs never take this path, but keep the helper
        # bounded even if a future caller supplies a different identifier.
        machine = hashlib_sha256(computer_id.encode("utf-8"))[:12]
        prefix = f"aw{API_VERSION}_{machine}_"
        available = 64 - len(prefix)

    raw_stable = str(event.get("stable_id") or "").strip()
    compatible = bool(raw_stable) and all(
        character.isascii() and (character.isalnum() or character in {"_", "-"})
        for character in raw_stable
    )
    if compatible and len(raw_stable) <= available:
        stable = raw_stable.lower()
    else:
        if raw_stable:
            stable_source = raw_stable.encode("utf-8")
        else:
            canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stable_source = canonical.encode("utf-8")
        stable = hashlib_sha256(stable_source)[: min(24, available)]
    return prefix + stable


def hashlib_sha256(value: bytes) -> str:
    # Kept as a tiny seam for deterministic unit tests without exposing inputs.
    import hashlib

    return hashlib.sha256(value).hexdigest()
