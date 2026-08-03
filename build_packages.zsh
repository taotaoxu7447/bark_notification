#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
DIST="$ROOT/dist"
VERSION="${1:-internal}"
CLAUDE_ICON_SOURCE="android/app/src/main/res/drawable-nodpi/source_claude.png"

if [[ -z "$ROOT" || -z "$DIST" || "$ROOT" == "/" || "$DIST" != "$ROOT/dist" || "$DIST" == "/" ]]; then
  echo "Refusing unsafe build output path: ROOT=$ROOT DIST=$DIST" >&2
  exit 2
fi
if [[ -L "$DIST" ]]; then
  echo "Refusing symlink build output path: $DIST" >&2
  exit 2
fi
if [[ -e "$DIST" && ! -d "$DIST" ]]; then
  echo "Refusing non-directory build output path: $DIST" >&2
  exit 2
fi

echo "Rebuilding exact package output directory: $DIST"
rm -rf -- "$DIST"
mkdir -p "$DIST"

COMMON=(
  agentwatch.py
  agentwatch_core.py
  claude_hook_config.py
  tool_hook_config.py
  codex_watch_notifier.py
  env.example
  README.md
  AI_INSTALL.md
  README_HANDOFF.md
  PACKAGING.md
  assets/cover-agent-watch.png
  assets/cover-notification-loop.png
  assets/codex-icon-large-v1.png
  assets/zcode-icon-v1.png
  assets/kimi-icon-v1.png
  assets/grok-icon-v1.png
  assets/pi-icon-v1.png
  assets/opencode-icon-v1.png
  assets/README.md
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
  cp "$ROOT/$CLAUDE_ICON_SOURCE" "$pkg_dir/assets/claude-icon-v1.png"
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
  zip -Xqr "codex-watch-notifier-macos-$VERSION.zip" "codex-watch-notifier-macos-$VERSION"
  # Do not leak macOS AppleDouble, ACL, flag, or extended-attribute metadata
  # into the Linux package. GNU tar otherwise warns about LIBARCHIVE.xattr
  # records when extracting an archive produced by macOS bsdtar.
  COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata --no-acls --no-fflags \
    --uid 0 --gid 0 --uname root --gname root \
    -czf "codex-watch-notifier-ubuntu-$VERSION.tar.gz" "codex-watch-notifier-ubuntu-$VERSION"
  zip -Xqr "codex-watch-notifier-windows-$VERSION.zip" "codex-watch-notifier-windows-$VERSION"
)

echo "Built packages in $DIST"
