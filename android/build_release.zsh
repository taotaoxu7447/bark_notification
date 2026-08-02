#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
KEYSTORE_PATH="${AGENTWATCH_RELEASE_KEYSTORE:-${HOME}/.agentwatch-signing/agentwatch-release.jks}"
KEYCHAIN_SERVICE="io.github.taotaoxu7447.agentwatch.release"
KEYCHAIN_ACCOUNT="agentwatch"

if [[ ! -f "$KEYSTORE_PATH" || -L "$KEYSTORE_PATH" ]]; then
  print -u2 "Release keystore is missing or unsafe: $KEYSTORE_PATH"
  exit 1
fi

STORE_PASSWORD="$(security find-generic-password \
  -s "$KEYCHAIN_SERVICE" \
  -a "$KEYCHAIN_ACCOUNT" \
  -w)"
if [[ -z "$STORE_PASSWORD" ]]; then
  print -u2 "Release signing password was not found in macOS Keychain"
  exit 1
fi

export AGENTWATCH_STORE_FILE="$KEYSTORE_PATH"
export AGENTWATCH_STORE_PASSWORD="$STORE_PASSWORD"
export AGENTWATCH_KEY_ALIAS="agentwatch"
export AGENTWATCH_KEY_PASSWORD="$STORE_PASSWORD"

export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home}"
cd "$SCRIPT_DIR"
exec ./gradlew --offline --no-daemon testDebugUnitTest lintDebug assembleRelease
