# Codex Watch Notifier

This folder contains a Bark/AgentWatch notifier for local AI coding agents.

Goal: when a local Codex, ZCode, Kimi Code, or Grok Build task completes, stops, needs attention, or aborts, send a push only to the user's own devices. Bark is optional for iPhone and Apple Watch. Android uses the custom AgentWatch app over an account-isolated self-hosted WebSocket.

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

- `agentwatch.py`: cross-platform install/login/status/doctor/update/logout/uninstall CLI.
- `agentwatch_core.py`: stable computer identity, OS credential storage, and strict private publish client.
- `codex_watch_notifier.py`: Python monitor. Uses only the Python standard library.
- `codex-watch-notifier.zsh`: wrapper that loads `~/.codex-watch-notifier/env`.
- `install_launch_agent.zsh`: installs runtime copies into `~/.codex-watch-notifier/bin` and starts a user LaunchAgent.
- `uninstall_launch_agent.zsh`: stops/removes the LaunchAgent.
- `env.example`: template config. It includes the public API endpoint; account passwords and computer tokens never belong in this file.
- `android/`: custom Android receiver, persistent event dedupe/ACK outbox, source channels/icons, and release build script.
- `deploy/agentwatch-registration/`: loopback registration/login/logout/test/ACK API plus Caddy and systemd examples.
- Optional Bark icon/group: set `CODEX_BARK_ICON` and `CODEX_BARK_GROUP`. This repo includes `assets/codex-icon-large-v1.png`, available at `https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/codex-icon-large-v1.png`.
- ZCode Bark settings: set `ZCODE_BARK_ICON` and `ZCODE_BARK_GROUP`. This repo includes `assets/zcode-icon-v1.png`, available at `https://raw.githubusercontent.com/taotaoxu7447/bark_notification/main/assets/zcode-icon-v1.png`. ZCode notifications watch `~/.zcode/cli/log/zcode-*.jsonl`.
- Kimi Code and Grok Build use `assets/kimi-icon-v1.png` and `assets/grok-icon-v1.png` as their default Bark icons. Override them with `KIMI_BARK_ICON` or `GROK_BARK_ICON`.
- Kimi Code watches `~/.kimi-code/sessions/**/agents/main/wire.jsonl`. Its child agents are silent unless `KIMI_WATCH_NOTIFY_SUBAGENTS=1`.
- Grok Build watches `~/.grok/sessions/**/events.jsonl`. Sessions with `parent_session_id` are silent unless `GROK_WATCH_NOTIFY_SUBAGENTS=1`.
- AgentWatch settings: `AGENTWATCH_API_BASE` is public metadata. `agentwatch login` uses a hidden password prompt once and stores only a per-computer token. `/publish` accepts no topic or user field. Legacy `NTFY_URL/NTFY_TOKEN` values are ignored to prevent duplicate shared-topic delivery.

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
./install_launch_agent.zsh
~/.local/bin/agentwatch doctor
```

If a previous `com.xutao.codex-watch-notifier` LaunchAgent exists, the installer stops it before prompting for the account and hidden password. Login success starts exactly one private watcher and sends no test notification.

## Ubuntu Install

Run these commands from the Ubuntu package folder:

```bash
chmod +x install_systemd_user.sh uninstall_systemd_user.sh
./install_systemd_user.sh
~/.local/bin/agentwatch doctor
```

If the service must run while the user is not logged in:

```bash
loginctl enable-linger "$USER"
```

## Windows Install

Run PowerShell from the Windows package folder:

```powershell
.\install_task_scheduler.ps1
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" doctor
```

The Windows package installs a scheduled task named `CodexWatchNotifier`. It remains disabled until login and runs through a hidden PowerShell wrapper with stdout/stderr logs.

For AI-driven setup, follow `AI_INSTALL.md`: run the platform installer with `--json --no-login`, pause for the user to run the hidden-prompt `agentwatch login`, then verify with `agentwatch doctor --json`.

## Test

Send one test notification through every configured channel:

```bash
./codex-watch-notifier.zsh --test
./codex-watch-notifier.zsh --test-zcode
./codex-watch-notifier.zsh --test-kimi
./codex-watch-notifier.zsh --test-grok
```

Expected: the configured personal Bark client or logged-in AgentWatch account receives `Codex 测试提醒`. Install, update, and login never invoke these test commands automatically.

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
watching /Users/<user>/.codex/sessions with channels=['bark', 'agentwatch']
```

Run diagnostics:

```bash
~/.local/bin/agentwatch doctor
```

The unified doctor checks runtime files, account binding, the single background service, server health, and ignored legacy ntfy values. The watcher-level `--doctor` retains log-root and retry diagnostics.

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

Successful channels are persisted per event and skipped during the second round. A local macOS banner cannot mask a failed AgentWatch/Bark delivery, while an already successful remote channel is not sent again.

Private `/publish` receives a stable event ID, source, title, body, and optional priority only. The server derives user/topic from the Bearer computer token. AgentWatch persists the event before notifying, resumes its WebSocket safely, and maintains an ACK outbox. Do not remove these controls: preventing duplicate audible notifications is a product requirement.

The long-running watcher and `--once`/`--replay-file` processing hold an OS-backed lock next to the state file. A second process exits without sending, so an installer restart or manual command cannot multiply the two-attempt allowance.

## Uninstall

```bash
./uninstall_launch_agent.zsh
```

This removes the background service and installed runtime. Account credentials, config, logs, and watcher state remain for an idempotent reinstall; `agentwatch logout` revokes the current computer token first.

## Important Notes

- Do not print, commit, or share the Bark URL, Bark key, computer token, account password, or authentication database. The self-hosted API base is intentionally public.
- Do not remove first-run EOF baselining; otherwise the target Mac may receive many old Codex completion pushes.
- Do not raise the hard two-attempt delivery cap or restore immediate retry loops; avoiding repeated phone alerts is a product requirement.
- Every account must have its own random topic and least-privilege ACL. A computer token is write-only for its bound account and must never specify topic/user in a publish request.
- Keep `CODEX_WATCH_MAX_EVENT_AGE_SECONDS` enabled unless you explicitly want old rewritten rollout history to be replayed.
- If this Mac stores Codex rollout files somewhere other than `~/.codex/sessions`, find the actual `rollout-*.jsonl` location and set `--sessions-root` by adapting the LaunchAgent/wrapper.
