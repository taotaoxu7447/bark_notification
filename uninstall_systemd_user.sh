#!/usr/bin/env bash
set -euo pipefail

LABEL="codex-watch-notifier"
SERVICE_FILE="$HOME/.config/systemd/user/$LABEL.service"

systemctl --user disable --now "$LABEL.service" >/dev/null 2>&1 || true
rm -f "$SERVICE_FILE"
systemctl --user daemon-reload

echo "Stopped and removed $LABEL.service"
echo "Config/logs under ~/.codex-watch-notifier were left in place."
