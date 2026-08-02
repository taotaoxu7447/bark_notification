#!/usr/bin/env python3
"""Cross-platform AgentWatch computer command line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import subprocess
import sys
from typing import Any

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
    load_or_create_machine,
    resolve_delivery,
    save_delivery_mode,
    save_machine_account,
)


VERSION = "0.2.1"
MACOS_LABEL = "com.xutao.codex-watch-notifier"
LINUX_UNIT = "codex-watch-notifier.service"
WINDOWS_TASK = "CodexWatchNotifier"
RUNTIME_FILES = (
    "agentwatch.py",
    "agentwatch_core.py",
    "codex_watch_notifier.py",
    "env.example",
)
RUNNING_SERVICE_STATES = {"running", "active"}
BARK_UPDATE_INSTRUCTION = (
    "请私下将 BARK_URL 或 BARK_KEY 写入持久 env，然后运行 agentwatch update 以启用 Bark"
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


def _run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
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
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AgentWatchError(f"refusing symlink in installation path: {current}")


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
            _run(["launchctl", "bootout", target])
            _run(["launchctl", "enable", target])
            result = _run(["launchctl", "bootstrap", domain, str(self.paths.macos_plist)])
            if result.returncode != 0 and "already loaded" not in result.stderr.lower():
                raise AgentWatchError("could not start the AgentWatch LaunchAgent")
            result = _run(["launchctl", "kickstart", "-k", target])
            if result.returncode != 0:
                raise AgentWatchError("could not start the AgentWatch LaunchAgent")
        elif self.system_name == "Linux":
            result = _run(["systemctl", "--user", "enable", "--now", LINUX_UNIT])
            if result.returncode != 0:
                raise AgentWatchError("could not start the AgentWatch systemd user service")
        elif self.system_name == "Windows":
            _run(["schtasks.exe", "/Change", "/TN", WINDOWS_TASK, "/Enable"])
            result = _run(["schtasks.exe", "/Run", "/TN", WINDOWS_TASK])
            if result.returncode != 0:
                raise AgentWatchError("could not start the AgentWatch scheduled task")

    def stop(self) -> None:
        if self.system_name == "Darwin":
            target = f"gui/{os.getuid()}/{MACOS_LABEL}"
            _run(["launchctl", "bootout", target])
            _run(["launchctl", "disable", target])
        elif self.system_name == "Linux":
            _run(["systemctl", "--user", "stop", LINUX_UNIT])
            _run(["systemctl", "--user", "disable", LINUX_UNIT])
        elif self.system_name == "Windows":
            _run(["schtasks.exe", "/End", "/TN", WINDOWS_TASK])
            _run(["schtasks.exe", "/Change", "/TN", WINDOWS_TASK, "/Disable"])

    def uninstall(self) -> None:
        if self.system_name == "Darwin":
            reject_symlink_path(self.paths.macos_plist, self.paths.home)
        elif self.system_name == "Linux":
            reject_symlink_path(self.paths.linux_unit, self.paths.home)
        self.stop()
        if self.system_name == "Darwin":
            try:
                self.paths.macos_plist.unlink()
            except FileNotFoundError:
                pass
        elif self.system_name == "Linux":
            _run(["systemctl", "--user", "disable", LINUX_UNIT])
            try:
                self.paths.linux_unit.unlink()
            except FileNotFoundError:
                pass
            _run(["systemctl", "--user", "daemon-reload"])
        elif self.system_name == "Windows":
            _run(["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK, "/F"])

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
        _run(["systemctl", "--user", "daemon-reload"])
        if should_start:
            self.start()

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
            _run(["schtasks.exe", "/Change", "/TN", WINDOWS_TASK, "/Disable"])


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
    except OSError:
        pass
    return values


def _delivery_snapshot(
    paths: InstallPaths,
) -> tuple[dict[str, str], ComputerTokenStore, str | None, dict[str, Any]]:
    machine = load_or_create_machine(paths.config)
    store = ComputerTokenStore(machine["computer_id"], paths.config)
    values = _config_values(paths)
    bark_configured = bool(values.get("BARK_URL", "").strip() or values.get("BARK_KEY", "").strip())
    mode = load_delivery_mode(paths.config)
    token: str | None = None
    # Explicit Bark-only mode must not depend on Keychain/Secret Service or an
    # AgentWatch account. Missing legacy settings still inspect a token once so
    # an existing dual-receiver installation can be migrated accurately.
    if mode != "bark":
        token = store.load()
    if mode is None:
        mode = infer_delivery_mode(bark_configured, token is not None)
        if mode is not None:
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
        "api_base": api_base(),
        "legacy_ntfy_ignored": bool(legacy_keys),
        "legacy_ntfy_keys": legacy_keys,
        "launcher": str(paths.launcher),
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
    api = AgentWatchApi()
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
    effective = ", ".join(result.get("effective_channels") or []) or "无"
    print(f"当前可用通道：{effective}")
    if result.get("degraded"):
        missing = ", ".join(result.get("missing_channels") or [])
        print(f"状态：可用但未完全配置（缺少 {missing}）")
    print(f"凭据存储：{result['credential_backend']}")
    if result.get("legacy_ntfy_ignored"):
        print("旧版 NTFY_URL/NTFY_TOKEN：已检测到，但 v0.2 私有发布会忽略它们，不会双发")


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
    paths = InstallPaths()
    service = ServiceManager(paths)
    json_output = bool(args.json)
    try:
        if args.command == "install":
            install_runtime(paths)
            if args.delivery:
                save_delivery_mode(args.delivery, paths.config)
            result = _status(paths, service)
            if result["delivery_mode"] is None:
                if json_output or not sys.stdin.isatty():
                    raise DeliveryModeRequired(
                        "无法从现有配置判断接收方式；请使用 --delivery bark|agentwatch|both"
                    )
                save_delivery_mode(_prompt_delivery_mode(), paths.config)
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
            checks = {
                "runtime_files": all((paths.runtime / filename).exists() for filename in RUNTIME_FILES[:3]),
                "delivery_mode_selected": result["delivery_mode"] is not None,
                "service_installed": result["installed"],
                "service_running": result["service"] in RUNNING_SERVICE_STATES,
                "legacy_ntfy_ignored": result["legacy_ntfy_ignored"],
            }
            mode = result["delivery_mode"]
            if mode in {"bark", "both"}:
                checks["bark_configured"] = result["bark_configured"]
            agentwatch_healthy = False
            if mode in {"agentwatch", "both"}:
                checks["agentwatch_authenticated"] = result["agentwatch_authenticated"]
                if result["agentwatch_authenticated"]:
                    try:
                        AgentWatchApi().health()
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
            install_runtime(paths)
            before = _status(paths, service)
            service.install(should_start=before["operational"])
            result = _status(paths, service)
            result.update({"ok": True, "message": "AgentWatch 已更新；账号凭据保持不变，未发送测试通知"})
            _emit(result, json_output)
            return 0

        if args.command == "logout":
            # Snapshot first so a legacy Bark+token install is migrated to
            # `both` before the token disappears.
            _machine, store, token, _delivery = _delivery_snapshot(paths)
            if _delivery["delivery_mode"] == "bark":
                token = store.load()
            server_revoked = token is None
            if token:
                try:
                    AgentWatchApi().logout(token)
                    server_revoked = True
                except ApiError as exc:
                    if exc.status == 401:
                        server_revoked = True
                    else:
                        raise
            if not server_revoked:
                raise AgentWatchError("server did not revoke this computer token")
            store.delete()
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
                "server_revoked": True,
                "message": "电脑 token 已在服务器撤销并从本机删除；其他已配置接收通道保持运行",
            })
            _emit(result, json_output)
            return 0

        if args.command == "uninstall":
            reject_symlink_path(paths.launcher, paths.home)
            reject_symlink_path(paths.runtime, paths.config.parent)
            service.uninstall()
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
                "message": "AgentWatch 后台服务和程序已卸载；本机账号 token 与历史状态已保留",
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
