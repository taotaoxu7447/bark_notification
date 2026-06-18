#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
CONFIG_DIR="${CODEX_WATCH_CONFIG_DIR:-$HOME/.codex-watch-notifier}"
ENV_FILE="$CONFIG_DIR/env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

exec /usr/bin/python3 "$SCRIPT_DIR/codex_watch_notifier.py" "$@"
