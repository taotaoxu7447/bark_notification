# Install Steps For The Other Mac

Give this folder to the other Mac. Then ask Codex on that Mac to run:

```bash
cd /path/to/codex-watch-notifier-handoff-2026-06-16
mkdir -p ~/.codex-watch-notifier
cp env.example ~/.codex-watch-notifier/env
chmod 600 ~/.codex-watch-notifier/env
$EDITOR ~/.codex-watch-notifier/env
./install_launch_agent.zsh
./codex-watch-notifier.zsh --test
launchctl print gui/$(id -u)/com.xutao.codex-watch-notifier | sed -n '1,60p'
tail -40 ~/.codex-watch-notifier/notifier.log
```

Expected result:

- Test Bark notification arrives on the same iPhone / Apple Watch.
- LaunchAgent state is `running`.
- Log says it is watching `~/.codex/sessions` with Bark.

Do not paste the Bark URL into public chat or commit it to Git. Store it only in `~/.codex-watch-notifier/env`.
