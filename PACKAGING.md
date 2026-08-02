# Packaging Plan

The project should ship three platform-specific internal packages. The Python monitor stays shared; each package only differs in launcher and background service setup.

The Android receiver is a fourth, separately signed artifact. Its long-lived APK
signing key is not part of the platform packages and must never enter Git.

## Shared Core

All packages include:

- `agentwatch.py`
- `agentwatch_core.py`
- `codex_watch_notifier.py`
- `env.example`
- `AI_INSTALL.md`
- `assets/`
- platform README

The Python script loads `~/.codex-watch-notifier/env` itself, so Linux and Windows do not need the zsh wrapper.

Build all packages from macOS with:

```bash
./build_packages.zsh v0.1.0-internal
```

## macOS Package

Package name:

```text
codex-watch-notifier-macos.zip
```

Includes:

- `codex_watch_notifier.py`
- `codex-watch-notifier.zsh`
- `install_launch_agent.zsh`
- `uninstall_launch_agent.zsh`
- `env.example`
- `assets/`

Background runner:

- user LaunchAgent

Install flow:

```bash
./install_launch_agent.zsh
~/.local/bin/agentwatch doctor
```

The installer prompts for the AgentWatch account and a hidden password. It does
not send a test notification. AI-driven installs use `--json --no-login`, then
pause so the user can personally run `agentwatch login`.

## Ubuntu Package

Package name:

```text
codex-watch-notifier-ubuntu.tar.gz
```

Includes:

- `codex_watch_notifier.py`
- `install_systemd_user.sh`
- `uninstall_systemd_user.sh`
- `env.example`
- `assets/`

Background runner:

- `systemd --user` service

Install flow:

```bash
./install_systemd_user.sh
~/.local/bin/agentwatch doctor
```

Notes:

- If the machine must run without an active desktop login, enable lingering with `loginctl enable-linger "$USER"`.
- macOS local notifications are not available; AgentWatch private publish and optional personal Bark remain the cross-device channels.

## Windows Package

Package name:

```text
codex-watch-notifier-windows.zip
```

Includes:

- `codex_watch_notifier.py`
- `install_task_scheduler.ps1`
- `uninstall_task_scheduler.ps1`
- `env.example`
- `assets/`

Background runner:

- Windows Task Scheduler, running at logon

Install flow:

```powershell
.\install_task_scheduler.ps1
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" doctor
```

Notes:

- Paths in `env` may use Windows paths, for example `C:\Users\<name>\.codex\sessions`.
- The scheduled task launches a hidden PowerShell wrapper and appends stdout/stderr to private runtime logs.

## Internal Release Checklist

Before each internal release:

1. Run `python3 -m py_compile codex_watch_notifier.py`.
2. Run `agentwatch doctor --json` on each platform fixture.
3. Confirm install/update/login do not automatically send any test notification.
4. Confirm first-run baseline does not replay old Codex, ZCode, Kimi Code, or Grok Build history.
5. Build all three package files from the same git commit.
6. Tag the commit, for example `v0.1.0-internal`.
7. Run `android/build_release.zsh`, verify the APK signature/application ID, and
   publish `AgentWatch-android-<version>.apk` from the same commit.
