# Packaging Plan

The project should ship three platform-specific internal packages. The Python monitor stays shared; each package only differs in launcher and background service setup.

## Shared Core

All packages include:

- `codex_watch_notifier.py`
- `env.example`
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
./codex-watch-notifier.zsh --doctor
./codex-watch-notifier.zsh --test
```

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
python3 codex_watch_notifier.py --doctor
python3 codex_watch_notifier.py --test
```

Notes:

- If the machine must run without an active desktop login, enable lingering with `loginctl enable-linger "$USER"`.
- macOS local notifications are not available; Bark remains the primary notification channel.

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
py .\codex_watch_notifier.py --doctor
py .\codex_watch_notifier.py --test
```

Notes:

- Paths in `env` may use Windows paths, for example `C:\Users\<name>\.codex\sessions`.
- macOS local notifications are not available; Bark remains the primary notification channel.

## Internal Release Checklist

Before each internal release:

1. Run `python3 -m py_compile codex_watch_notifier.py`.
2. Run `--doctor` on macOS.
3. Smoke test Bark with `--test`.
4. Confirm first-run baseline does not replay old Codex or ZCode history.
5. Build all three package files from the same git commit.
6. Tag the commit, for example `v0.1.0-internal`.
