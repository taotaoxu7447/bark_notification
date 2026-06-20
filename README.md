# Codex Watch Notifier

Bark-first desktop notifier for local AI coding agents.

Current stable watchers:

- Codex App / Codex CLI rollout logs under `~/.codex/sessions`
- ZCode logs under `~/.zcode/cli/log`

The notifier sends Bark pushes when a watched agent completes, stops, needs attention, or aborts. Bark is the primary supported notification channel for the internal release.

## Pick Your Package

Download the package for your OS from the release page:

- macOS: `codex-watch-notifier-macos-<version>.zip`
- Ubuntu: `codex-watch-notifier-ubuntu-<version>.tar.gz`
- Windows: `codex-watch-notifier-windows-<version>.zip`

## Bark Setup

Install Bark on your iPhone, copy your Bark push URL or key, then set one of these in the generated env file:

```bash
BARK_URL=https://api.day.app/<your-key>
# or
BARK_KEY=<your-key>
```

Do not commit or share the real Bark URL/key.

## macOS

```bash
./install_launch_agent.zsh
$EDITOR ~/.codex-watch-notifier/env
./install_launch_agent.zsh
./codex-watch-notifier.zsh --doctor
./codex-watch-notifier.zsh --test
```

## Ubuntu

```bash
chmod +x install_systemd_user.sh uninstall_systemd_user.sh
./install_systemd_user.sh
$EDITOR ~/.codex-watch-notifier/env
./install_systemd_user.sh
python3 ~/.codex-watch-notifier/bin/codex_watch_notifier.py --doctor
python3 ~/.codex-watch-notifier/bin/codex_watch_notifier.py --test
```

## Windows

Run PowerShell from the extracted package folder:

```powershell
.\install_task_scheduler.ps1
notepad $env:USERPROFILE\.codex-watch-notifier\env
.\install_task_scheduler.ps1
py -3 $env:USERPROFILE\.codex-watch-notifier\bin\codex_watch_notifier.py --doctor
py -3 $env:USERPROFILE\.codex-watch-notifier\bin\codex_watch_notifier.py --test
```

## Diagnostics

Run:

```bash
python3 codex_watch_notifier.py --doctor
```

On macOS, you can also use:

```bash
./codex-watch-notifier.zsh --doctor
```

The doctor command checks notification channels, log roots, state/log files, platform background service status, and privacy settings.

## Privacy

Notification bodies can include workspace paths and final message excerpts. To minimize sensitive content:

```bash
NOTIFY_INCLUDE_WORKSPACE=0
NOTIFY_INCLUDE_MESSAGE=0
NOTIFY_BODY_MAX_CHARS=0
```

## Build Packages

From macOS:

```bash
./build_packages.zsh v0.1.0-internal
```

Artifacts are written to `dist/`.
