#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="codex-watch-notifier"
CONFIG_DIR="$HOME/.codex-watch-notifier"
ENV_FILE="$CONFIG_DIR/env"
RUNTIME_DIR="$CONFIG_DIR/bin"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/$LABEL.service"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

mkdir -p "$CONFIG_DIR" "$RUNTIME_DIR" "$SERVICE_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$SCRIPT_DIR/env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE"
  echo "Edit it and set BARK_URL or BARK_KEY, then run this installer again."
  exit 0
fi

cp "$SCRIPT_DIR/codex_watch_notifier.py" "$RUNTIME_DIR/codex_watch_notifier.py"
chmod 700 "$RUNTIME_DIR/codex_watch_notifier.py"

cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Codex Watch Notifier
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$RUNTIME_DIR
ExecStart=$PYTHON_BIN $RUNTIME_DIR/codex_watch_notifier.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SERVICE

systemctl --user daemon-reload
systemctl --user enable --now "$LABEL.service"

echo "Installed and started $LABEL.service"
echo "Config: $ENV_FILE"
echo "Logs: journalctl --user -u $LABEL.service -n 80"
echo "Run: $PYTHON_BIN $RUNTIME_DIR/codex_watch_notifier.py --doctor"
