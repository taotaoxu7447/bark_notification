# AgentWatch Android

This module is the custom Android receiver for the project. The public server
address and topic are compiled into the app; users register or log in once,
then a foreground `remoteMessaging` service maintains one authenticated ntfy
WebSocket connection. There is no message-history screen.

The phone never connects directly to a sender computer. Each computer publishes
over authenticated HTTPS to the self-hosted HK_VPS, while AgentWatch maintains
an authenticated `wss://` connection to that same relay. ADB is needed only for
development or sideloading and may be disabled after installation.

## Delivery behavior

- Sources are grouped into separate Codex, ZCode, Kimi Code, and Grok Build
  notification channels with distinct icons.
- The publisher sends a stable `X-Sequence-ID`. The app persists that event key
  before posting and uses the same notification tag/ID on recovery, so a retry
  cannot create repeated audible alerts.
- WebSocket resume uses ntfy server timestamps, not message IDs. The timestamp
  query deliberately replays the final second; persistent event-key dedupe
  absorbs those copies without sound while preventing same-second message loss.
- The app stores only the short resume cursor and bounded dedupe state. It does
  not show or retain a user-facing notification archive.
- Delivery ACKs use a persistent, metadata-only outbox with bounded exponential
  retry. A temporary API or network failure cannot silently lose the server-side
  delivery evidence.
- Session tokens are encrypted with an Android Keystore AES-GCM key. Android
  cloud backup and device-to-device transfer are both disabled for app data.
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
“退出并撤销此设备” first revokes both server tokens and only then clears the
local encrypted session.

Version 0.1 uses one shared, ACL-protected topic. It is a trusted broadcast
group, not tenant isolation: every invited account can read every task body on
that topic. Implement per-user topics and ACLs before onboarding mutually
untrusted users.
