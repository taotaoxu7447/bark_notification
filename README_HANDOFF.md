# Codex Watch Notifier

This folder contains a Bark/ntfy notifier for local AI coding agents.

Goal: when a local Codex, ZCode, Kimi Code, or Grok Build task completes, stops, needs attention, or aborts, send a push to the user's devices. Bark is recommended for iPhone and Apple Watch. Android uses the custom AgentWatch app over the project self-hosted ntfy WebSocket.

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
- `env.example`: template config. It includes the public self-hosted ntfy endpoint; copy it to `~/.codex-watch-notifier/env` and fill in a private Bark URL/key, ntfy publisher token, or webhook.
- `android/`: custom Android receiver, persistent event dedupe/ACK outbox, source channels/icons, and release build script.
- `deploy/agentwatch-registration/`: loopback registration/login/logout/test/ACK API plus Caddy and systemd examples.
- Optional Bark icon/group: set `CODEX_BARK_ICON` and `CODEX_BARK_GROUP`. This repo includes `assets/codex-icon-large-v1.png`, available at `https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/codex-icon-large-v1.png`.
- ZCode Bark settings: set `ZCODE_BARK_ICON` and `ZCODE_BARK_GROUP`. This repo includes `assets/zcode-icon-v1.png`, available at `https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/zcode-icon-v1.png`. ZCode notifications watch `~/.zcode/cli/log/zcode-*.jsonl`.
- Kimi Code and Grok Build use `assets/kimi-icon-v1.png` and `assets/grok-icon-v1.png` as their default Bark icons. Override them with `KIMI_BARK_ICON` or `GROK_BARK_ICON`.
- Kimi Code watches `~/.kimi-code/sessions/**/agents/main/wire.jsonl`. Its child agents are silent unless `KIMI_WATCH_NOTIFY_SUBAGENTS=1`.
- Grok Build watches `~/.grok/sessions/**/events.jsonl`. Sessions with `parent_session_id` are silent unless `GROK_WATCH_NOTIFY_SUBAGENTS=1`.
- ntfy settings: the self-hosted endpoint is `NTFY_URL=https://64.90.8.184:9444/agent-watch`. Optional per-tool overrides are `CODEX_NTFY_URL`, `ZCODE_NTFY_URL`, `KIMI_NTFY_URL`, and `GROK_NTFY_URL`. The endpoint/topic is public metadata, while `NTFY_TOKEN` remains private. The server defaults to deny-all and separates write-only publishers from read-only subscribers.

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
- Kimi Code `context.append_loop_event.event.type == "step.end"` with `finishReason == "end_turn"`
- Grok Build `type == "turn_ended"` with `outcome` equal to `completed`, `error`, or `cancelled`

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
./codex-watch-notifier.zsh --test-zcode
./codex-watch-notifier.zsh --test-kimi
./codex-watch-notifier.zsh --test-grok
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

The doctor command checks the config file, Bark/ntfy setup, all supported tool log roots, state file, notifier log, LaunchAgent state on macOS, and current privacy settings.

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

Delivery retries are deliberately bounded. One event is sent at most twice, with a persisted delay before the second attempt. Bark receives the same stable `id` on both attempts so a retry updates or collapses the same notification where supported. Exhausted deliveries are recorded for `--doctor`; they are never retried in a loop.

ntfy receives a stable `X-Sequence-ID`, protocol/source tags, and a public icon URL. AgentWatch persists the event key before notifying, uses timestamp-based WebSocket resume, and maintains a metadata-only ACK outbox. A server test carries a hashed target tag so only the initiating installation displays it. Do not remove these controls: preventing duplicate audible notifications is a product requirement.

The long-running watcher and `--once`/`--replay-file` processing hold an OS-backed lock next to the state file. A second process exits without sending, so an installer restart or manual command cannot multiply the two-attempt allowance.

## Uninstall

```bash
./uninstall_launch_agent.zsh
```

This removes only the LaunchAgent plist. Config and logs remain in `~/.codex-watch-notifier`.

## Important Notes

- Do not print, commit, or share the Bark URL, Bark key, ntfy token, account password, or authentication database. The official self-hosted ntfy URL/topic is intentionally public.
- Do not remove first-run EOF baselining; otherwise the target Mac may receive many old Codex completion pushes.
- Do not raise the hard two-attempt delivery cap or restore immediate retry loops; avoiding repeated phone alerts is a product requirement.
- The current Android release is a trusted shared-broadcast design: every invited account can read the same `agent-watch` topic. Do not invite mutually untrusted users without first implementing per-user topics and ACLs.
- Keep `CODEX_WATCH_MAX_EVENT_AGE_SECONDS` enabled unless you explicitly want old rewritten rollout history to be replayed.
- If this Mac stores Codex rollout files somewhere other than `~/.codex/sessions`, find the actual `rollout-*.jsonl` location and set `--sessions-root` by adapting the LaunchAgent/wrapper.
