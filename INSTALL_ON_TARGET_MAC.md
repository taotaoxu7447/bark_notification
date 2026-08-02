# Install Steps For The Other Mac

Give this folder to the other Mac. Then ask Codex on that Mac to run:

```bash
cd /path/to/codex-watch-notifier-handoff-2026-06-16
./install_launch_agent.zsh --json --no-login
```

Codex must then pause. The user personally runs this in Terminal and enters the
password in the hidden prompt:

```bash
~/.local/bin/agentwatch login
```

After the user confirms login, Codex may run:

```bash
~/.local/bin/agentwatch doctor --json
```

Expected result:

- The computer is authenticated to the user's private AgentWatch channel.
- LaunchAgent state is `running`.
- No test notification was sent during install or login.

Do not give the account password to Codex or place it in a command, environment variable, config file, or log. See `AI_INSTALL.md` for the full protocol.
