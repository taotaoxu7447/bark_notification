# Packaging Plan

The project should ship three platform-specific internal packages. The Python monitor stays shared; each package only differs in launcher and background service setup.

The Android receiver is a fourth, separately signed artifact. Its long-lived APK
signing key is not part of the platform packages and must never enter Git.

Every desktop package supports the same delivery selection:

- `bark`: iPhone / Apple Watch users install only Bark and privately configure that Bark installation's home-screen push URL or key on the computer. They do not register or log in to AgentWatch.
- `agentwatch`: Android users install the custom AgentWatch app and use the same account on the computer.
- `both`: both channels coexist independently. A missing AgentWatch login must not prevent a configured Bark channel from running.

The shared CLI contract is `agentwatch install --delivery bark|agentwatch|both`; each platform installer forwards `--delivery`. There is no `configure-bark` command. Bark URL/key and AgentWatch password must never enter AI chat or argv. Install, `update`, and `doctor` never send a test notification, and `doctor` never starts/restarts the background watcher.

For every `bark` or `both` install, the user must save `BARK_URL` or `BARK_KEY` in the persistent private `~/.codex-watch-notifier/env` (or its Windows-profile equivalent). A temporary shell export is not background configuration. After the user confirms the secret was saved without revealing it, run `agentwatch update` to reconcile and start/restart the service, then run the read-only `agentwatch doctor --json`.

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
./build_packages.zsh v0.2.0
```

## macOS Package

Package name:

```text
codex-watch-notifier-macos-v0.2.0.zip
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
./install_launch_agent.zsh --delivery bark
# or --delivery agentwatch / --delivery both
# bark/both: after the user privately saves BARK_URL/BARK_KEY
~/.local/bin/agentwatch update
~/.local/bin/agentwatch doctor --json
```

The installer asks for AgentWatch account input only in modes that use AgentWatch. AI-driven installs use the selected mode plus `--json --no-login`, then pause for the user-owned secret step: private Bark configuration, hidden-prompt `agentwatch login`, or both independently. It does not send a test notification.

## Ubuntu Package

Package name:

```text
codex-watch-notifier-ubuntu-v0.2.0.tar.gz
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
./install_systemd_user.sh --delivery bark
# or --delivery agentwatch / --delivery both
# bark/both: after the user privately saves BARK_URL/BARK_KEY
~/.local/bin/agentwatch update
~/.local/bin/agentwatch doctor --json
```

Notes:

- If the machine must run without an active desktop login, enable lingering with `loginctl enable-linger "$USER"`.
- macOS local notifications are not available; AgentWatch private publish and optional personal Bark remain the cross-device channels.

## Windows Package

Package name:

```text
codex-watch-notifier-windows-v0.2.0.zip
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
.\install_task_scheduler.ps1 --delivery bark
# or --delivery agentwatch / --delivery both
# bark/both: after the user privately saves BARK_URL/BARK_KEY
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" update
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" doctor --json
```

Notes:

- Paths in `env` may use Windows paths, for example `C:\Users\<name>\.codex\sessions`.
- The scheduled task launches a hidden PowerShell wrapper and appends stdout/stderr to private runtime logs.
- Task Scheduler state `Ready` only means the task is registered and waiting; it does not prove the watcher process is running. Use `doctor --json` (`checks.service_running`) and the runtime log. Doctor does not start the task.

## Internal Release Checklist

Before each internal release:

1. Run `python3 -m py_compile codex_watch_notifier.py`.
2. Exercise `install --delivery bark`, `agentwatch`, and `both` on each platform fixture. For Bark-enabled modes, persist a fixture secret, run `agentwatch update`, and only then run `agentwatch doctor --json`.
3. Confirm install/update/login do not automatically send any test notification.
4. Confirm `doctor` is read-only, does not start/restart the service, and does not send a test notification.
5. Confirm Bark-only never requests AgentWatch credentials, Android mode uses the custom app/account path, and `both` keeps Bark running when AgentWatch is logged out.
6. Confirm first-run baseline does not replay old Codex, ZCode, Kimi Code, or Grok Build history.
7. Build all three package files from the same git commit.
8. Tag the commit `v0.2.0`.
9. Run `android/build_release.zsh`, verify the APK signature/application ID, and
   publish `AgentWatch-android-v0.2.0.apk` from the same commit.
