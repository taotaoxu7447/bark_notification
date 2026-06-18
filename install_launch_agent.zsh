#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
LABEL="com.xutao.codex-watch-notifier"
CONFIG_DIR="$HOME/.codex-watch-notifier"
ENV_FILE="$CONFIG_DIR/env"
RUNTIME_DIR="$CONFIG_DIR/bin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VALUE="$(id -u)"

mkdir -p "$CONFIG_DIR" "$RUNTIME_DIR" "$HOME/Library/LaunchAgents"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$SCRIPT_DIR/env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE"
  echo "Edit it and set SERVERCHAN_SENDKEY or another push channel, then run this installer again."
  exit 0
fi

cp "$SCRIPT_DIR/codex_watch_notifier.py" "$RUNTIME_DIR/codex_watch_notifier.py"
cp "$SCRIPT_DIR/codex-watch-notifier.zsh" "$RUNTIME_DIR/codex-watch-notifier.zsh"
chmod 700 "$RUNTIME_DIR/codex-watch-notifier.zsh" "$RUNTIME_DIR/codex_watch_notifier.py"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$RUNTIME_DIR/codex-watch-notifier.zsh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$CONFIG_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$CONFIG_DIR/launchd.err.log</string>
  <key>WorkingDirectory</key>
  <string>$RUNTIME_DIR</string>
</dict>
</plist>
PLIST

chmod 644 "$PLIST"

launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_VALUE" "$PLIST"
launchctl enable "gui/$UID_VALUE/$LABEL"
launchctl kickstart -k "gui/$UID_VALUE/$LABEL"

echo "Installed and started $LABEL"
echo "Config: $ENV_FILE"
echo "Logs: $CONFIG_DIR/notifier.log and $CONFIG_DIR/launchd.err.log"
