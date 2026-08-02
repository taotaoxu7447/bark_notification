# Install Steps For The Other Mac

Before installation, Codex must ask for or determine the receiving device and select exactly one delivery mode:

- `bark`: iPhone / Apple Watch. Install Bark only; do not register or log in to AgentWatch.
- `agentwatch`: Android. Install the custom AgentWatch app and log in there.
- `both`: configure both independently. Bark must continue working if the Android login is not yet complete.

Give this folder to the other Mac. Then ask Codex on that Mac to run the command matching the selected mode. For example:

```bash
cd /path/to/codex-watch-notifier-handoff-2026-06-16
./install_launch_agent.zsh --delivery both --json --no-login
```

For `bark`, Codex must pause while the user personally copies the Bark home-screen personal push URL or key into the persistent private `~/.codex-watch-notifier/env`. The URL contains the key: it must not enter AI chat, argv, command output, logs, or Git. A temporary shell `export` does not configure the background watcher, which reads only the persistent private env. This is Bark configuration on the computer, not AgentWatch account pairing. There is no `configure-bark` command.

For `agentwatch`, Codex must pause. The user personally runs this in Terminal and enters the password in the hidden prompt:

```bash
~/.local/bin/agentwatch login
```

For `both`, the user completes each step independently. If the AgentWatch login is deferred, the configured Bark channel must still run.

After the user confirms that the Bark secret has been saved—without sending its value back—Codex must run `update` for `bark` or `both` so the CLI reconciles and starts/restarts the LaunchAgent:

```bash
~/.local/bin/agentwatch update
```

Only after that coordination step may Codex run:

```bash
~/.local/bin/agentwatch doctor --json
```

Expected result:

- Bark is configured for `bark`; AgentWatch authentication is not required.
- The computer is authenticated to the user's private AgentWatch channel for `agentwatch`.
- For `both`, each channel is reported independently; missing AgentWatch authentication does not invalidate Bark.
- LaunchAgent state is `running`.
- No test notification was sent during install, login, `update`, or `doctor`.

`doctor` is read-only: it does not start or restart the LaunchAgent and does not send a test notification. Do not give the AgentWatch password or Bark URL/key to Codex or place either in argv. The AgentWatch password also must not enter an environment variable, config file, or log. See `AI_INSTALL.md` for the full protocol.
