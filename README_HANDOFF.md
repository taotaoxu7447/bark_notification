# Codex Watch Notifier Handoff

This folder contains the exact notifier implementation from the source Mac.

Goal: configure this Mac so any local Codex session completion, stop, attention-needed state, or abort sends a Bark push to the user's existing iPhone and Apple Watch.

## Files

- `codex_watch_notifier.py`: Python monitor. Uses only the Python standard library.
- `codex-watch-notifier.zsh`: wrapper that loads `~/.codex-watch-notifier/env`.
- `install_launch_agent.zsh`: installs runtime copies into `~/.codex-watch-notifier/bin` and starts a user LaunchAgent.
- `uninstall_launch_agent.zsh`: stops/removes the LaunchAgent.
- `env.example`: template config. Copy it to `~/.codex-watch-notifier/env` and fill in a private push token or webhook.

## What It Monitors

Default rollout root:

```text
~/.codex/sessions/**/rollout-*.jsonl
```

On first background start, existing rollout files are baselined at EOF so old Codex history is not pushed. New rollout files and appended lines are then polled every 2 seconds.

Triggers:

- `event_msg.payload.type == "task_complete"`
- `event_msg.payload.type == "turn_aborted"`

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

## Install On The Target Mac

Run these commands from this folder:

```bash
mkdir -p ~/.codex-watch-notifier
cp env.example ~/.codex-watch-notifier/env
chmod 600 ~/.codex-watch-notifier/env
$EDITOR ~/.codex-watch-notifier/env
./install_launch_agent.zsh
```

If a previous `com.xutao.codex-watch-notifier` LaunchAgent exists, the installer will `bootout` it and install the current copy.

## Test

Send one Bark test notification:

```bash
./codex-watch-notifier.zsh --test
```

Expected: iPhone receives `Codex 测试提醒`. If the iPhone is locked and the Apple Watch is worn/unlocked, the watch should vibrate.

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
watching /Users/<user>/.codex/sessions with channels=['bark']
```

## Verify Real Codex Completion

After the LaunchAgent is running, finish any Codex turn. Within a few seconds, Bark should send a notification with a title like:

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

## Uninstall

```bash
./uninstall_launch_agent.zsh
```

This removes only the LaunchAgent plist. Config and logs remain in `~/.codex-watch-notifier`.

## Important Notes

- Do not print, commit, or share the Bark URL; it is a push token.
- Do not remove first-run EOF baselining; otherwise the target Mac may receive many old Codex completion pushes.
- If this Mac stores Codex rollout files somewhere other than `~/.codex/sessions`, find the actual `rollout-*.jsonl` location and set `--sessions-root` by adapting the LaunchAgent/wrapper.
