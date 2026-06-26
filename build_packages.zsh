#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
DIST="$ROOT/dist"
VERSION="${1:-internal}"

rm -rf "$DIST"
mkdir -p "$DIST"

COMMON=(
  codex_watch_notifier.py
  env.example
  README.md
  README_HANDOFF.md
  PACKAGING.md
  assets/cover-agent-watch.png
  assets/codex-icon-large-v1.png
  assets/zcode-icon-v1.png
)

make_pkg() {
  local name="$1"
  shift
  local pkg_dir="$DIST/$name"
  mkdir -p "$pkg_dir/assets"
  for file in "${COMMON[@]}"; do
    mkdir -p "$pkg_dir/${file:h}"
    cp "$ROOT/$file" "$pkg_dir/$file"
  done
  for file in "$@"; do
    cp "$ROOT/$file" "$pkg_dir/$file"
  done
}

make_pkg "codex-watch-notifier-macos-$VERSION" \
  codex-watch-notifier.zsh \
  install_launch_agent.zsh \
  uninstall_launch_agent.zsh

make_pkg "codex-watch-notifier-ubuntu-$VERSION" \
  install_systemd_user.sh \
  uninstall_systemd_user.sh

make_pkg "codex-watch-notifier-windows-$VERSION" \
  install_task_scheduler.ps1 \
  uninstall_task_scheduler.ps1

(
  cd "$DIST"
  zip -qr "codex-watch-notifier-macos-$VERSION.zip" "codex-watch-notifier-macos-$VERSION"
  tar -czf "codex-watch-notifier-ubuntu-$VERSION.tar.gz" "codex-watch-notifier-ubuntu-$VERSION"
  zip -qr "codex-watch-notifier-windows-$VERSION.zip" "codex-watch-notifier-windows-$VERSION"
)

echo "Built packages in $DIST"
