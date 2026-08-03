# AgentWatch Android

This module is the custom Android receiver for the project. The public API
address is compiled into the app. Registration/login returns a random,
per-account ntfy topic plus per-install read token; all session fields are
encrypted with Android Keystore before a foreground `remoteMessaging` service
opens the authenticated WebSocket.

The phone never connects directly to a sender computer. Each computer publishes
over authenticated HTTPS to the self-hosted HK_VPS, while AgentWatch maintains
an authenticated `wss://` connection to that same relay. ADB is needed only for
development or sideloading and may be disabled after installation.

## Delivery behavior

- Sources are grouped into separate Codex, ZCode, Kimi Code, Grok Build,
  Claude Code, Pi Agent, and OpenCode notification channels with distinct
  small and large icons. Each source also has its own Android history category.
- The main UI has Messages, Devices, and Settings pages. Messages are grouped
  by source and support detail view, search, single/source/all deletion. The
  Devices page lists account-bound computers and can revoke one sender.
- Message title/body and computer attribution are stored as plaintext SQLite
  rows in the app-private data directory. They are isolated by account, are
  excluded from cloud/device-transfer backups, and are not uploaded as a
  server-side archive. Authentication and relay credentials remain encrypted.
- History defaults to seven days and is always capped at the newest 500 rows
  per account. Settings offer 1/7/30 days or no age expiry; the 500-row cap
  still applies to the no-expiry option.
- The publisher sends a stable `X-Sequence-ID`. The app persists that event key
  as a unique local history key before posting, then uses the same notification
  tag/ID on recovery and queues ACK only after display commit. A retry cannot
  create repeated history or audible alerts.
- WebSocket resume uses ntfy server timestamps, not message IDs. The timestamp
  query deliberately replays the final second; persistent event-key dedupe
  absorbs those copies without sound while preventing same-second message loss.
- Delivery ACKs use a persistent, metadata-only outbox with bounded exponential
  retry. A temporary API or network failure cannot silently lose the server-side
  delivery evidence.
- Session tokens are encrypted with an Android Keystore AES-GCM key. Android
  cloud backup and device-to-device transfer are both disabled for app data.
- Version 0.1 sessions are upgraded through the authenticated
  `session/upgrade` endpoint, replacing the shared subscription with the
  account's private topic without asking for the password again.
- End-to-end tests carry a hash-derived installation target tag. Every client
  advances its cursor, but only the initiating device displays and ACKs it.

## Build

Install JDK 17 plus Android SDK platform/build-tools 36, then create
`local.properties` with `sdk.dir=...` and run:

```bash
./gradlew --no-daemon testDebugUnitTest lintDebug assembleDebug
```

Release builds require the long-lived signing keystore. On this maintainer Mac,
the keystore is kept outside Git at
`~/.agentwatch-signing/agentwatch-release.jks`; its password is stored in the
macOS Keychain service `io.github.taotaoxu7447.agentwatch.release`, account
`agentwatch`. Build without exposing the password in process output:

```bash
./build_release.zsh
```

Back up the release keystore securely. Losing it prevents future APKs from
updating an installed copy. Never commit the keystore, its password, ntfy
tokens, invitation codes, or generated `local.properties`.

## Manual onboarding

The user must personally approve Android notification permission and the phone
vendor's autostart/battery settings. The app provides buttons that open those
settings but does not automate them. After login, the connection card should
show `已连接`; the end-to-end test button publishes one fixed Codex test and the
server can confirm the matching delivery ACK without storing its message body.
“退出并撤销此设备” first revokes both server tokens, asks whether the user wants
to keep that account's local history, and only then clears the encrypted local
session. Sender computers log in with the same account through the desktop CLI;
the Android app never asks for or displays a computer pairing code.

Version 0.4.0 consumes only the dynamic per-account topic returned by API v2
and recognizes Claude, Pi Agent, and OpenCode as first-class sources in the
notification and history UI.
Other accounts have no ACL read access to that topic. ntfy remains a short-term
offline-delivery buffer (currently about six hours), not a long-term message
archive; a phone offline beyond that window cannot reconstruct expired events.
