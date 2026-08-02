# AgentWatch account and relay service

This dependency-free Python API is the authenticated control plane for the
AgentWatch Android app and desktop watcher. It listens only on
`127.0.0.1:2587`; Caddy exposes `/agentwatch/api/v1/`. ntfy remains a short-term
relay on loopback port 2586 and retains messages for at most six hours. This
service never creates a message-history table.

## Privacy model

Every AgentWatch account receives both:

- a cryptographically random topic (`aw-` plus 128 random bits); and
- a distinct random ntfy non-admin principal with an exact read-only ACL for
  that topic.

Each Android installation gets a separate token for that principal. The
existing `agent-watch-publisher` principal receives an exact write-only ACL for
each private topic as it is provisioned. It is never promoted to admin and
cannot read a topic. A random topic name alone is not treated as authorization.

Desktop computers do not receive ntfy credentials. Account/password login
issues one opaque `awc_...` computer token. The database stores only its SHA-256
digest. `/publish` derives the account and private topic from that token; the
request cannot provide a topic, user, URL, target tag, or protocol tag. Computer
tokens are write-only at the AgentWatch API, remain valid until explicitly
revoked, and can be revoked individually in the app or through computer logout.
The account password is verified once, never stored on the computer, and never
logged by this service.

## API v2

All POST bodies are strict JSON objects with a 16 KiB limit. Unknown and
duplicate fields are rejected. Errors have the form
`{"error":"code","message":"sanitized text"}`.

Android/account endpoints:

- `POST /register`: `username`, `password`, `invite_code`, `device_id`,
  `device_name`. Creates a private channel and returns `api_version=2`, dynamic
  `ntfy_topic`, `ntfy_url`, `ntfy_ws_url`, per-installation read-only
  `ntfy_token`, `app_token`, and `target_tag`.
- `POST /login`: `username`, `password`, `device_id`, `device_name`. Rotates the
  installation's app and private read tokens. If this is a v0.1 account it also
  provisions the private channel.
- `POST /session/upgrade`: `{}` plus `Authorization: Bearer <app_token>`. Lets a
  signed-in v0.1 app obtain its private channel and read token without entering
  the account password. The old shared read token is revoked only after private
  provisioning and the database commit succeed. Devices migrate independently.
- `POST /logout`: `{}` plus App Bearer. Revokes both private and residual shared
  read tokens before deleting the installation.
- `GET /computers`: App Bearer. Returns only the account's active computers as
  `computer_id`, `computer_name`, `platform`, `created_at`, and `last_seen_at`.
- `POST /computers/revoke`: `computer_id` plus App Bearer. Cross-account and
  unknown IDs both return `computer_not_found`.
- `POST /test`: optional validated `source` (`codex`, `zcode`, `kimi`, `grok`,
  `claude`, or `other`) plus App Bearer. Sends exactly one device-targeted v2
  test notification on the account's private topic.
- `POST /ack`: App Bearer plus `event_id` (legacy `sequence_id` accepted).
  Delivery-only rows contain device, event ID, and server time and expire after
  seven days; title and message body are never stored.

Computer endpoints:

- `POST /computers/login`: `username`, `password`, `computer_id`,
  `computer_name`, `platform`. Returns the computer token once. Re-login for the
  same account/computer rotates the token; a computer ID owned by another
  account returns `computer_conflict`. A legacy account must first open the v2
  Android app and finish `/session/upgrade`; this prevents a new computer from
  switching delivery to a private topic before the phone has its read token.
- `POST /computers/logout`: `{}` plus Computer Bearer. Revokes the current
  computer token. A repeated request with the old token returns 401, which a CLI
  may treat as already logged out.
- `POST /publish`: Computer Bearer plus exactly `event_id`, `source` (including
  first-class `claude`), `title`, `body`, and optional `priority`. The server
  sets receipt time and publishes a
  compact ntfy message envelope:

```json
{"schema":"agentwatch_event_v2","event_id":"...","source":"codex","title":"...","body":"...","computer_id":"...","computer_name":"...","sent_at":1785600000}
```

The ntfy `sequence_id` is identical to `event_id`; fixed tags include
`agentwatch_v2` and `source_<source>`. The UTF-8 envelope is capped below the
configured ntfy 4 KiB message limit. Client timestamps are not accepted, so a
misconfigured computer clock cannot corrupt Android history ordering/cleanup.

Authentication, publishing, tests, and revocation have both route/IP and
identity-specific sliding-window limits. Publishing is capped at 120 events per
minute for the entire account (as well as per computer), so adding computers
cannot create a notification burst above the mobile catch-up window. Passwords
use scrypt with random
16-byte salts. App and computer tokens are never stored in plaintext.

## Safe ntfy provisioning

The service runs `ntfy user add` with a one-time random internal password in the
child's `NTFY_PASSWORD` environment. The password never appears in argv,
service logs, or the AgentWatch database; Android uses read tokens instead.
Provisioning order is subscriber user, exact read ACL, exact publisher write
ACL, then device token. Failed operations reset the exact publisher/topic ACL
and delete only the newly generated subscriber principal.

The live ntfy configuration must use `auth-default-access: deny-all` and contain
the legacy principals during migration:

```bash
sudo -u ntfy -- /usr/bin/ntfy access agent-watch-subscriber
sudo -u ntfy -- /usr/bin/ntfy access agent-watch-publisher
```

`agent-watch-subscriber` keeps read-only access to shared `agent-watch` only
until all old app installations migrate. `agent-watch-publisher` is write-only;
the API dynamically adds exact private-topic write ACLs. Neither is admin.

After a private channel is created, perform a bounded permission preflight with
that installation's private read token and the server's private publisher token
(never paste real values into a ticket or shell history):

```text
anonymous private-topic read/write: 403
installation token private-topic read: 2xx
installation token private-topic write: 403
publisher token private-topic write: 2xx
publisher token private-topic read: 403
```

Use one clearly labelled test event for the write check; do not loop or send a
second notification just to reconfirm it.

## Database migration

`Database.initialize()` migrates a v0.1 `user_version=0` database in place:

- nullable private channel columns are added to `users`;
- nullable `devices.private_ready_at` records each installation's successful
  private-token migration independently;
- the hashed-token `computers` table and indexes are created; and
- `PRAGMA user_version` is set to `2`.

Initialization is idempotent. It does not provision ntfy resources or revoke a
shared token on startup. Each user is provisioned under the existing
serialization lock when its first v2 login/upgrade occurs, so a failed external
operation leaves the v0.1 app session and shared token usable. Computer login is
blocked until every registered mobile installation has `private_ready_at`, so
one upgraded phone cannot strand another phone on the old shared channel.
Always back up both SQLite databases transactionally before deployment.

## Install on HK_VPS

Install reviewed files and a root-owned secret environment file; never populate
`service.env` in the checkout:

```bash
sudo install -d -o root -g root -m 0755 /opt/agentwatch-registration
sudo install -o root -g root -m 0644 server.py maintenance.py /opt/agentwatch-registration/
sudo install -d -o root -g ntfy -m 0750 /etc/agentwatch-registration
sudo install -o root -g root -m 0644 agentwatch-registration.service.example \
  /etc/systemd/system/agentwatch-registration.service
sudoedit /etc/agentwatch-registration/service.env
sudo chown root:ntfy /etc/agentwatch-registration/service.env
sudo chmod 0640 /etc/agentwatch-registration/service.env
```

The process runs as `ntfy` because the CLI updates `/var/lib/ntfy/user.db`.
The unit grants write access only to the two exact application state paths.
Ports 2586 and 2587 stay loopback-only. Keep Caddy's `/v1/account*` denial and
access logging disabled: ntfy access tokens inherit their principal's complete
ACL/token-management capabilities.

Validate before reload, then check schema and service health:

```bash
sudo /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl daemon-reload
sudo systemctl restart agentwatch-registration.service
curl --fail --silent --show-error https://64.90.8.184:9444/agentwatch/api/v1/health
sudo -u ntfy -- /usr/bin/python3 -I -c \
  'import sqlite3; db=sqlite3.connect("/var/lib/agentwatch-registration/registration.db"); print(db.execute("PRAGMA user_version").fetchone()[0]); print(db.execute("PRAGMA quick_check").fetchone()[0]); db.close()'
```

The HK host does not require the optional `sqlite3` shell. Use Python's stdlib
`sqlite3.Connection.backup()` for online, transactionally consistent backups
of both `registration.db` and ntfy's `user.db`, then run `PRAGMA quick_check`
against each backup before replacing server code.

## Controlled legacy retirement

Do not remove the shared topic ACL when only one phone has upgraded. Audit and
revoke one installation at a time while the API is stopped if doing manual
repair:

```bash
sudo systemctl stop agentwatch-registration.service
sudo -u ntfy -- /usr/bin/python3 -I /opt/agentwatch-registration/maintenance.py audit-private-channels
sudo -u ntfy -- /usr/bin/python3 -I /opt/agentwatch-registration/maintenance.py audit-legacy-tokens
sudo -u ntfy -- /usr/bin/python3 -I /opt/agentwatch-registration/maintenance.py \
  revoke-legacy-device alice exact-device-id --yes
sudo systemctl start agentwatch-registration.service
```

Emergency computer inventory/revocation is also exact and never prints a token:

```bash
sudo -u ntfy -- /usr/bin/python3 -I /opt/agentwatch-registration/maintenance.py list-computers alice
sudo -u ntfy -- /usr/bin/python3 -I /opt/agentwatch-registration/maintenance.py \
  revoke-computer alice exact-computer-id --yes
```

Only after every app and computer is verified on private delivery, no legacy
device tokens remain, and the old desktop publisher has stopped, reset the two
exact shared ACLs:

```bash
sudo systemctl stop agentwatch-registration.service
sudo -u ntfy -- /usr/bin/python3 -I /opt/agentwatch-registration/maintenance.py \
  reset-legacy-acls --yes --all-computers-migrated
sudo systemctl start agentwatch-registration.service
```

The explicit confirmation prevents an audit command from disconnecting an
unmigrated computer. The tool never prints a token.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py maintenance.py
python3 -I maintenance.py --help
```

Tests cover v0.1-to-v2 and repeated initialization, safe provisioning rollback,
private-topic separation, cross-account computer isolation, token rotation,
expiry-policy enforcement, revocation, strict payloads, v2 envelopes, and rate
limits. ntfy continues to cache message bodies for only six hours; Android owns
the long-term local history.
