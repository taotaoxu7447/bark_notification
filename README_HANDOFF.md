# Codex Watch Notifier

This folder contains a Bark/AgentWatch notifier for local AI coding agents.

Goal: when a local Codex, ZCode, Kimi Code, Grok Build, or Claude Code task completes, stops, needs attention, or aborts, send a push only to the user's own devices. Bark is optional for iPhone and Apple Watch. Android uses the custom AgentWatch app over an account-isolated self-hosted WebSocket.

## Desktop Delivery Modes

Desktop installation must start by asking for, or reliably determining from existing context, the receiving device:

| Delivery | Receiver preparation | Desktop binding |
| --- | --- | --- |
| `bark` | Install Bark on iPhone; Apple Watch mirrors the iPhone notification. Do not install, register, or log in to AgentWatch. | Store the personal Bark home-screen push URL or key in private local configuration. This is not AgentWatch account pairing. |
| `agentwatch` | Install the custom AgentWatch Android app and register or log in there. | Log the computer in to the same AgentWatch account and retain only its write-only computer token. |
| `both` | Complete both receiver preparations independently. | Configure both channels. A missing Android/AgentWatch login must not stop an already configured Bark channel. |

The CLI contract is `agentwatch install --delivery bark|agentwatch|both`. There is no `configure-bark` command; do not invent one. `install`, `update`, and `doctor` may not send a test notification, and `doctor` never starts or restarts the watcher.

The Bark URL contains its key and is therefore a secret. The user must personally place it in private computer configuration; it must not enter AI chat, argv, logs, or Git. The same no-chat/no-argv rule applies to the AgentWatch password, which is entered only through the CLI's hidden interactive prompt.

For every `bark` or `both` install, the Bark URL/key must persist in `~/.codex-watch-notifier/env` (or the same private config path under the Windows user profile). A temporary shell export is not background configuration. After the user privately saves the secret, run `agentwatch update` to reconcile and start/restart the background watcher, then run `agentwatch doctor --json`. The AI may run those two commands only after the user confirms completion; it must never receive, read, or echo the secret.

## Platform Plan

The Python monitor is shared across platforms. Internal releases should be split into three packages:

- macOS: LaunchAgent package.
- Ubuntu: systemd user service package.
- Windows: Task Scheduler package.

See `PACKAGING.md` for the package layout. The macOS, Ubuntu, and Windows package entrypoints all forward `--delivery` to the same CLI semantics.

For coworkers, publish three release artifacts and ask them to download the one matching their OS:

- `codex-watch-notifier-macos-<version>.zip`
- `codex-watch-notifier-ubuntu-<version>.tar.gz`
- `codex-watch-notifier-windows-<version>.zip`

## Files

- `agentwatch.py`: cross-platform install/login/status/doctor/update/logout/uninstall CLI. `install --delivery` selects `bark`, `agentwatch`, or `both`.
- `agentwatch_core.py`: stable computer identity, OS credential storage, and strict private publish client.
- `claude_hook_config.py`: safe, idempotent Claude user-settings merge/status/removal logic.
- `codex_watch_notifier.py`: Python monitor. Uses only the Python standard library.
- `agentwatch.py` also exposes the internal Claude hook handler. It validates stdin JSON and appends only a safe subset to a private local spool; it never performs network delivery in the Claude process.
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
- Claude Code 2.1.196+ uses only its official `Stop` and `StopFailure` hooks. The effective minimum is 2.1.196 because exec-form `args` arrived in 2.1.139, `background_tasks` / `session_crons` in 2.1.145, and the primary `prompt_id` correlation key in 2.1.196. Install/update idempotently merge AgentWatch's managed entries into the active user settings without replacing existing hooks. `SubagentStop` is deliberately not enabled. A private registration file records the exact settings path so changing `CLAUDE_CONFIG_DIR` becomes a reconcile operation rather than a duplicate Hook.
- Claude's spool defaults to `~/.codex-watch-notifier/claude-hook-events.jsonl`. A custom `CLAUDE_WATCH_EVENTS_FILE` must be a dedicated, private path registered by AgentWatch; it must never reuse or take ownership of another data file. Fully consumed live data rotates at the earlier of the 4 MiB soft capacity limit (`CLAUDE_WATCH_SPOOL_MAX_BYTES=4194304`) or the 24-hour privacy TTL (`CLAUDE_WATCH_SPOOL_MAX_AGE_SECONDS=86400`); supported minimums are 64 KiB and one hour. Unread/retrying data is never truncated. Rotation renames the old inode into one watched drain and retires it only after it is consumed and stable through the 30-second append safety window. The packaged Claude icon is `assets/claude-icon-v1.png`; Android uses a dedicated Claude source icon, notification channel, and history category.
- Desktop `status`/`doctor` are deliberately static and cannot enumerate every project, local, plugin, skill, agent, session, or remote-managed runtime scope. Final verification requires Claude Code's own `/status` for `Setting sources` and `/hooks` for the effective Hook list.
- Claude's first `Stop` with `stop_hook_active=false` is provisional because all matching hooks run in parallel. The watcher leaves its durable offset unchanged for `CLAUDE_WATCH_STOP_SETTLE_SECONDS` (default 35 seconds, clamped to 5–600), performs no network work and consumes no delivery attempt, and read-only scans complete later spool lines. A fully validated true Stop with the same session/prompt/transcript, or a valid same-prompt `StopFailure`, suppresses the false record and is processed as the terminal candidate. Transcript growth is corroboration only: Claude writes transcripts asynchronously, so growth without a matching terminal record must not drop an ordinary final Stop. The 35-second default covers the official 30-second prompt-hook timeout plus merge/poll slack; command/HTTP/MCP hooks default to 600 seconds and custom project/plugin/session/managed hooks can outlast the configured window. A blocker that finishes after the window can still produce an early provisional alert; 600 seconds is the stricter but much slower option, not an absolute guarantee for longer custom timeouts.
- Bark settings: the personal `BARK_URL` or `BARK_KEY` comes from the Bark iPhone home screen and must be stored privately on the computer. It is a Bark delivery credential, not AgentWatch pairing.
- AgentWatch settings: Android users install the custom app and sign in there. `AGENTWATCH_API_BASE` is public metadata. Desktop `agentwatch login` uses a hidden password prompt once and stores only a per-computer token. `/publish` accepts no topic or user field. Legacy `NTFY_URL/NTFY_TOKEN` values are ignored to prevent duplicate shared-topic delivery.

## What It Monitors

Default rollout root:

```text
~/.codex/sessions/**/rollout-*.jsonl
```

On first background start, existing rollout files are baselined at EOF so old Codex history is not pushed. The Claude hook spool is also baselined at its current EOF on first enable, so records created before this watcher takes ownership are not replayed. New records are then polled every 2 seconds.

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
- Claude Code official `Stop` and `StopFailure` hook payloads accepted by the local hook ingestor; `SubagentStop` is rejected and never configured

Claude's hook path is intentionally split: the hook only validates and appends to the private local spool, exits successfully, and emits no network traffic. The long-running watcher reads the spool and uses the same bounded delivery/de-duplication path as all other sources.

The official Hook contract and parallel execution semantics are documented at <https://code.claude.com/docs/en/hooks>; version-specific field additions are tracked in <https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md>. Never infer a block from transcript size alone: the official docs state that transcript persistence can lag the in-memory conversation and recommend `last_assistant_message` for notification hooks.

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

Choose the delivery mode from the receiver device first. The package installer forwards the same `--delivery` choice to the unified CLI:

```bash
./install_launch_agent.zsh --delivery bark
# or --delivery agentwatch / --delivery both
# bark/both: after the user privately saves BARK_URL/BARK_KEY
~/.local/bin/agentwatch update
~/.local/bin/agentwatch doctor --json
```

If a previous `com.xutao.codex-watch-notifier` LaunchAgent exists, the installer stops it before replacing the single watcher. Bark-only setup must not request AgentWatch credentials. AgentWatch login uses the hidden prompt; `both` keeps Bark available even while that login is incomplete. Installation and update also reconcile only AgentWatch's managed Claude `Stop`/`StopFailure` hooks while preserving every pre-existing Claude setting and hook. Installation and `doctor` send no test notification.

## Ubuntu Install

Choose `bark`, `agentwatch`, or `both` from the receiver device before running the Ubuntu package bootstrap:

```bash
chmod +x install_systemd_user.sh uninstall_systemd_user.sh
./install_systemd_user.sh --delivery bark
# or --delivery agentwatch / --delivery both
# bark/both: after the user privately saves BARK_URL/BARK_KEY
~/.local/bin/agentwatch update
~/.local/bin/agentwatch doctor --json
```

If the service must run while the user is not logged in:

```bash
loginctl enable-linger "$USER"
```

## Windows Install

Choose `bark`, `agentwatch`, or `both` from the receiver device before running PowerShell from the Windows package folder:

```powershell
.\install_task_scheduler.ps1 --delivery bark
# or --delivery agentwatch / --delivery both
# bark/both: after the user privately saves BARK_URL/BARK_KEY
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" update
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" doctor --json
```

The Windows package installs a scheduled task named `CodexWatchNotifier` and runs through a hidden PowerShell wrapper with stdout/stderr logs. AgentWatch authentication is required only for the `agentwatch` channel; Bark-only operation must not be gated on it. Task Scheduler state `Ready` means the task is registered and waiting, not that the watcher process is currently running; use `doctor --json` (`checks.service_running`) and the runtime log to diagnose actual execution.

Across macOS, Ubuntu, and Windows, repeated install/update must be safe: merge the managed Claude hooks once, preserve unrelated `~/.claude/settings.json` content, never add `SubagentStop`, and never emit a notification as a side effect.

For AI-driven setup, follow `AI_INSTALL.md`: determine the receiver and delivery mode first, run the platform installer without secrets, then pause for only the relevant user-owned steps. The user privately configures the persistent Bark URL/key for `bark`; the user runs the hidden-prompt `agentwatch login` for `agentwatch`; `both` requires each independently. After Bark configuration, the AI runs `agentwatch update` and only then `agentwatch doctor --json`. Doctor is read-only: it does not start the watcher or test delivery.

## Test

Only when the user explicitly requests a source-specific end-to-end test, run exactly one relevant command once (each command uses the configured delivery channel or channels). Do not run the whole list as an installation check:

```bash
./codex-watch-notifier.zsh --test
./codex-watch-notifier.zsh --test-zcode
./codex-watch-notifier.zsh --test-kimi
./codex-watch-notifier.zsh --test-grok
./codex-watch-notifier.zsh --test-claude
```

Expected: the explicitly configured personal Bark client or logged-in Android AgentWatch account receives the selected source's test reminder. `--test-claude` is an explicit one-shot external test; it does not simulate a hook retry loop. These commands are manual and intentional and may run only when the user asks for a notification test; install, update, login, `doctor`, and packaging never invoke them automatically.

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

Example when both channels are fully configured:

```text
watching /Users/<user>/.codex/sessions with channels=['bark', 'agentwatch']
```

Run diagnostics:

```bash
~/.local/bin/agentwatch doctor
```

The unified doctor checks runtime files, account binding, the single background service, server health, and ignored legacy ntfy values. The watcher-level `--doctor` retains log-root and retry diagnostics.

Doctor results are mode-dependent: Bark-only does not require AgentWatch authentication, and `both` may report AgentWatch login as incomplete without treating a working Bark configuration as disabled. Doctor never starts/restarts the watcher or sends a notification; use `update` first whenever persistent Bark configuration has just changed.

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

This removes the background service and installed runtime. It must also remove only AgentWatch's own managed entries from Claude's `Stop` and `StopFailure` arrays; all other Claude hooks and settings remain untouched. Account credentials, config, logs, and watcher state remain for an idempotent reinstall; `agentwatch logout` revokes the current computer token first.

## Important Notes

- Do not print, commit, or share the Bark URL, Bark key, computer token, account password, or authentication database. The self-hosted API base is intentionally public.
- Do not put a Bark URL/key or AgentWatch password in AI chat or argv. iPhone/Apple Watch setup is Bark-only and is not AgentWatch account pairing.
- Ask for or infer the receiver device before installation, then preserve the selected `bark`, `agentwatch`, or `both` semantics. In `both`, an unavailable AgentWatch login must not disable Bark.
- Do not remove first-run EOF baselining for either discovered session files or the Claude hook spool; otherwise an update may replay old completion records.
- Do not turn the Claude spool's 4 MiB/24-hour soft retention policy into a hard truncate. A live file may exceed either boundary while unread records, an active retry, or a drain exists; preserving those records takes precedence, and cleanup must continue to use rename plus drain.
- Do not advance the Claude spool offset, create a delivery attempt, or call a notification channel while a false Stop is inside its settle window. Preserve the tail lookahead and full candidate validation; transcript growth by itself is never sufficient suppression evidence. Keep the 5–600 second clamp and document that blockers slower than the selected window remain an early-alert edge case.
- Do not raise the hard two-attempt delivery cap or restore immediate retry loops; avoiding repeated phone alerts is a product requirement.
- Do not put network work into a Claude hook, enable `SubagentStop`, replace existing `~/.claude/settings.json` hooks, or make uninstall remove entries it does not own.
- Do not treat desktop `status`/`doctor` as proof that every Claude Hook scope is active. Verify the effective runtime configuration with both `/status` and `/hooks`, and keep custom spool paths dedicated, private, and explicitly registered by AgentWatch.
- Every account must have its own random topic and least-privilege ACL. A computer token is write-only for its bound account and must never specify topic/user in a publish request.
- Keep `CODEX_WATCH_MAX_EVENT_AGE_SECONDS` enabled unless you explicitly want old rewritten rollout history to be replayed.
- If this Mac stores Codex rollout files somewhere other than `~/.codex/sessions`, find the actual `rollout-*.jsonl` location and set `--sessions-root` by adapting the LaunchAgent/wrapper.
