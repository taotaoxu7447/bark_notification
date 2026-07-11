# Codex Watch Notifier

This folder contains a Bark/ntfy notifier for local AI coding agents.

Goal: when a local Codex or ZCode task completes, stops, needs attention, or aborts, send a push to the user's devices. Bark is recommended for iPhone and Apple Watch. Public `ntfy.sh` is the current Android-friendly channel for Android phones and wearable notification forwarding.

## Platform Plan

The Python monitor is shared across platforms. Internal releases should be split into three packages:

- macOS: LaunchAgent package.
- Ubuntu: systemd user service package.
- Windows: Task Scheduler package.

See `PACKAGING.md` for the package layout. The current installer in this repository is the macOS LaunchAgent installer.

For coworkers, publish three release artifacts and ask them to download the one matching their OS:

- `codex-watch-notifier-macos-<version>.zip`
- `codex-watch-notifier-ubuntu-<version>.tar.gz`
- `codex-watch-notifier-windows-<version>.zip`

## Files

- `codex_watch_notifier.py`: Python monitor. Uses only the Python standard library.
- `codex-watch-notifier.zsh`: wrapper that loads `~/.codex-watch-notifier/env`.
- `install_launch_agent.zsh`: installs runtime copies into `~/.codex-watch-notifier/bin` and starts a user LaunchAgent.
- `uninstall_launch_agent.zsh`: stops/removes the LaunchAgent.
- `env.example`: template config. Copy it to `~/.codex-watch-notifier/env` and fill in a private Bark URL/key, ntfy topic URL, or webhook.
- Optional Bark icon/group: set `CODEX_BARK_ICON` and `CODEX_BARK_GROUP`. This repo includes `assets/codex-icon-large-v1.png`, available at `https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/codex-icon-large-v1.png`.
- ZCode Bark settings: set `ZCODE_BARK_ICON` and `ZCODE_BARK_GROUP`. This repo includes `assets/zcode-icon-v1.png`, available at `https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/zcode-icon-v1.png`. ZCode notifications watch `~/.zcode/cli/log/zcode-*.jsonl`.
- ntfy settings: set `NTFY_URL=https://ntfy.sh/<long-random-topic>`. Optional per-tool overrides are `CODEX_NTFY_URL` and `ZCODE_NTFY_URL`. Public ntfy.sh topics are shared secrets; never use short or guessable topic names.

## What It Monitors

Default rollout root:

```text
~/.codex/sessions/**/rollout-*.jsonl
```

On first background start, existing rollout files are baselined at EOF so old Codex history is not pushed. New rollout files and appended lines are then polled every 2 seconds.

To avoid false pushes from Codex account/session tools such as Cockpit Tools, the monitor also:

- ignores rollout files whose first `session_meta.payload.thread_source` is `subagent` unless `CODEX_WATCH_NOTIFY_SUBAGENTS=1`; unknown or legacy metadata continues to notify;
- skips Codex completion events older than `CODEX_WATCH_MAX_EVENT_AGE_SECONDS` seconds, default `3600`;
- uses semantic de-duplication based on thread id, event type, and turn id instead of JSONL byte offset;
- detects known rollout files whose header changes, then baselines them at EOF instead of replaying history.

Triggers:

- `event_msg.payload.type == "task_complete"`
- `event_msg.payload.type == "turn_aborted"`
- ZCode `message == "ZCode Protocol background turn completed"` from `~/.zcode/cli/log/zcode-*.jsonl`

Thread title:

- Reads `session_meta.payload.id` from the rollout file.
- Looks up the title in `~/.codex/session_index.jsonl`.
- Falls back to the first 8 chars of the thread id.

Status labeling:

- `turn_aborted` => `Codex 会话已中止`
- `task_complete` with attention markers such as `需要你`, `等你`, `确认`, `是否`, `你看`, `下一步`, `失败`, `报错`, `error`, `confirm` => `Codex 需要处理`
- `task_complete` with completion markers such as `已完成`, `完成了`, `改完了`, `验证通过`, `已处理`, `done`, `completed` => `Codex 已完成`
- otherwise => `Codex 已停下`

There is no official structured complete-vs-attention-needed field in the observed Codex `task_complete` payload, so the status split is intentionally conservative and based on the final assistant message.

## macOS Install

Run these commands from this folder:

```bash
mkdir -p ~/.codex-watch-notifier
cp env.example ~/.codex-watch-notifier/env
chmod 600 ~/.codex-watch-notifier/env
$EDITOR ~/.codex-watch-notifier/env
./install_launch_agent.zsh
```

If a previous `com.xutao.codex-watch-notifier` LaunchAgent exists, the installer will `bootout` it and install the current copy.

## Ubuntu Install

Run these commands from the Ubuntu package folder:

```bash
chmod +x install_systemd_user.sh uninstall_systemd_user.sh
./install_systemd_user.sh
$EDITOR ~/.codex-watch-notifier/env
./install_systemd_user.sh
python3 ~/.codex-watch-notifier/bin/codex_watch_notifier.py --doctor
python3 ~/.codex-watch-notifier/bin/codex_watch_notifier.py --test
```

If the service must run while the user is not logged in:

```bash
loginctl enable-linger "$USER"
```

## Windows Install

Run PowerShell from the Windows package folder:

```powershell
.\install_task_scheduler.ps1
notepad $env:USERPROFILE\.codex-watch-notifier\env
.\install_task_scheduler.ps1
py -3 $env:USERPROFILE\.codex-watch-notifier\bin\codex_watch_notifier.py --doctor
py -3 $env:USERPROFILE\.codex-watch-notifier\bin\codex_watch_notifier.py --test
```

The Windows package installs a scheduled task named `CodexWatchNotifier` that starts at logon.

## Test

Send one test notification through every configured channel:

```bash
./codex-watch-notifier.zsh --test
```

Expected: the configured Bark or ntfy client receives `Codex 测试提醒`. For Apple Watch, if the iPhone is locked and the Apple Watch is worn/unlocked, the watch should vibrate. For Android wearables, behavior depends on the phone's notification forwarding settings.

Check service state:

```bash
launchctl print gui/$(id -u)/com.xutao.codex-watch-notifier | sed -n '1,60p'
```

Expected:

```text
state = running
```

Check notifier log:

```bash
tail -40 ~/.codex-watch-notifier/notifier.log
```

Expected line:

```text
watching /Users/<user>/.codex/sessions with channels=['bark', 'ntfy']
```

Run diagnostics:

```bash
./codex-watch-notifier.zsh --doctor
```

The doctor command checks the config file, Bark/ntfy setup, Codex/ZCode log roots, state file, notifier log, LaunchAgent state on macOS, and current privacy settings.

## Verify Real Codex Completion

After the LaunchAgent is running, finish any Codex turn. Within a few seconds, the configured channel should send a notification with a title like:

```text
Codex 需要处理: <会话标题>
Codex 已完成: <会话标题>
Codex 已停下: <会话标题>
```

The body includes:

- status
- status reasoning
- session title
- short thread id
- time
- working directory
- final message excerpt

## Privacy

Notification bodies can include workspace paths and final assistant message excerpts. For a safer internal default, edit `~/.codex-watch-notifier/env`:

```bash
NOTIFY_INCLUDE_WORKSPACE=0
NOTIFY_INCLUDE_MESSAGE=0
NOTIFY_BODY_MAX_CHARS=0
```

Keep these enabled only when the extra context is useful and acceptable for your team.

## Uninstall

```bash
./uninstall_launch_agent.zsh
```

This removes only the LaunchAgent plist. Config and logs remain in `~/.codex-watch-notifier`.

## Important Notes

- Do not print, commit, or share the Bark URL, Bark key, or ntfy topic URL; they are push tokens.
- Do not remove first-run EOF baselining; otherwise the target Mac may receive many old Codex completion pushes.
- Keep `CODEX_WATCH_MAX_EVENT_AGE_SECONDS` enabled unless you explicitly want old rewritten rollout history to be replayed.
- If this Mac stores Codex rollout files somewhere other than `~/.codex/sessions`, find the actual `rollout-*.jsonl` location and set `--sessions-root` by adapting the LaunchAgent/wrapper.
