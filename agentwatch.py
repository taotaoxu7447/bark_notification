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
    load_or_create_machine,
    save_machine_account,
)


VERSION = "0.2.0"
MACOS_LABEL = "com.xutao.codex-watch-notifier"
LINUX_UNIT = "codex-watch-notifier.service"
WINDOWS_TASK = "CodexWatchNotifier"
RUNTIME_FILES = (
    "agentwatch.py",
    "agentwatch_core.py",
    "codex_watch_notifier.py",
    "env.example",
)


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

    def install(self, authenticated: bool) -> None:
        if self.system_name == "Darwin":
            self._install_macos(authenticated)
        elif self.system_name == "Linux":
            self._install_linux(authenticated)
        elif self.system_name == "Windows":
            self._install_windows(authenticated)
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

    def _install_macos(self, authenticated: bool) -> None:
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
        if authenticated:
            self.start()

    def _install_linux(self, authenticated: bool) -> None:
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
        if authenticated:
            self.start()

    def _install_windows(self, authenticated: bool) -> None:
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
        if authenticated:
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


def _status(paths: InstallPaths, service: ServiceManager) -> dict[str, Any]:
    machine = load_or_create_machine(paths.config)
    store = ComputerTokenStore(machine["computer_id"], paths.config)
    legacy_keys: list[str] = []
    env_path = paths.config / "env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in {"NTFY_URL", "NTFY_TOKEN"} and value.strip():
                legacy_keys.append(key.strip())
    except OSError:
        pass
    return {
        "version": VERSION,
        "installed": service.installed(),
        "authenticated": store.load() is not None,
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


def _login(username: str | None, paths: InstallPaths, service: ServiceManager) -> dict[str, Any]:
    machine = load_or_create_machine(paths.config)
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
    try:
        store.save(token)
        save_machine_account(machine, str(response.get("username") or entered_username), paths.config)
    except Exception:
        try:
            api.logout(token)
        except AgentWatchError:
            pass
        store.delete()
        raise
    service.start()
    return {
        "ok": True,
        "authenticated": True,
        "username": str(response.get("username") or entered_username),
        "computer_id": machine["computer_id"],
        "computer_name": machine["computer_name"],
        "platform": machine["platform"],
        "service": service.state(),
        "message": "private notification channel is ready",
    }


def _human_status(result: dict[str, Any]) -> None:
    print(f"AgentWatch {result['version']}")
    print(f"安装：{'已安装' if result['installed'] else '未安装'}")
    print(f"登录：{'已登录' if result['authenticated'] else '需要登录'}")
    if result.get("username"):
        print(f"账号：{result['username']}")
    print(f"电脑：{result['computer_name']} ({result['platform']})")
    print(f"后台服务：{result['service']}")
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
    install.add_argument("--username", help="Pre-fill the non-secret account name for interactive login.")
    install.add_argument("--password", action=RejectPasswordAction, nargs="?", help=argparse.SUPPRESS)
    login = command("login", "Log this computer into an AgentWatch account.")
    login.add_argument("--username", help="Pre-fill the non-secret account name.")
    login.add_argument("--password", action=RejectPasswordAction, nargs="?", help=argparse.SUPPRESS)
    command("status", "Show local authentication and background service state.")
    command("doctor", "Run local and server diagnostics without sending a notification.")
    command("update", "Install this package over the existing runtime without changing credentials.")
    command("logout", "Delete only this computer's local token and stop the watcher.")
    command("uninstall", "Remove the background service and installed runtime; keep account data.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = InstallPaths()
    service = ServiceManager(paths)
    json_output = bool(args.json)
    try:
        if args.command == "install":
            install_runtime(paths)
            machine = load_or_create_machine(paths.config)
            token = ComputerTokenStore(machine["computer_id"], paths.config).load()
            service.install(authenticated=token is not None)
            if token is None and not args.no_login and not json_output:
                print("AgentWatch 已安装。请登录以建立专属通知通道；密码输入不会显示。")
                result = _login(args.username, paths, service)
                result["installed"] = True
                result["launcher"] = str(paths.launcher)
                result["message"] += f"；后续命令入口：{paths.launcher}"
                _emit(result, False)
            else:
                result = _status(paths, service)
                result["ok"] = True
                result["login_required"] = not result["authenticated"]
                result["message"] = (
                    "AgentWatch 已安装；请亲自在终端运行 agentwatch login"
                    if result["login_required"]
                    else "AgentWatch 已完成幂等安装，现有账号绑定保持不变"
                )
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
            return 0 if result["installed"] and result["authenticated"] else 1

        if args.command == "doctor":
            result = _status(paths, service)
            checks = {
                "runtime_files": all((paths.runtime / filename).exists() for filename in RUNTIME_FILES[:3]),
                "authenticated": result["authenticated"],
                "service_installed": result["installed"],
                "service_running": result["service"] in {"running", "active", "ready"},
                "legacy_ntfy_ignored": result["legacy_ntfy_ignored"],
            }
            try:
                AgentWatchApi().health()
                checks["server_reachable"] = True
            except AgentWatchError:
                checks["server_reachable"] = False
            result["checks"] = checks
            result["ok"] = all(value for key, value in checks.items() if key != "legacy_ntfy_ignored")
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
            machine = load_or_create_machine(paths.config)
            token = ComputerTokenStore(machine["computer_id"], paths.config).load()
            install_runtime(paths)
            service.install(authenticated=token is not None)
            result = _status(paths, service)
            result.update({"ok": True, "message": "AgentWatch 已更新；账号凭据保持不变，未发送测试通知"})
            _emit(result, json_output)
            return 0

        if args.command == "logout":
            machine = load_or_create_machine(paths.config)
            store = ComputerTokenStore(machine["computer_id"], paths.config)
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
            service.stop()
            result = {
                "ok": True,
                "authenticated": False,
                "server_revoked": True,
                "message": "电脑 token 已在服务器撤销并从本机删除",
            }
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
