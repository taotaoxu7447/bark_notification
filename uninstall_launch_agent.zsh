#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

exec "$PYTHON_BIN" "$SCRIPT_DIR/agentwatch.py" uninstall "$@"
