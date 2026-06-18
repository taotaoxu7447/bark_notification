#!/bin/zsh
set -euo pipefail

LABEL="com.xutao.codex-watch-notifier"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VALUE="$(id -u)"

launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"

echo "Stopped and removed $LABEL"
echo "Config/logs under ~/.codex-watch-notifier were left in place."
