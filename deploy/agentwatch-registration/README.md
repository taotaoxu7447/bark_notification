# AgentWatch registration service

This is a small Python-standard-library API for the custom Android client. It
listens only on `127.0.0.1:2587`; Caddy exposes it under
`/agentwatch/api/v1/`. The existing ntfy service remains on loopback port 2586.

## API

All POST requests use `Content-Type: application/json` and have a 16 KiB hard
limit. Unknown or duplicate JSON fields are rejected.

- `POST /agentwatch/api/v1/register` accepts `username`, `password`,
  `invite_code`, `device_id`, and `device_name`. It creates the account and
  installation, then returns a per-installation read-only `ntfy_token` plus an
  `app_token` and the non-identifying `target_tag` used by test notifications.
- `POST /agentwatch/api/v1/login` accepts `username`, `password`, `device_id`,
  and `device_name`. It rotates both installation tokens. A successful login on
  the same installation invalidates the previous tokens.
- `POST /agentwatch/api/v1/logout` accepts `{}` with
  `Authorization: Bearer <app_token>`. It revokes every ntfy token carrying the
  current installation's derived label and only then removes the device row.
  Any ntfy or SQLite failure returns an error; the service never reports a
  successful logout after a partial revocation.
- `POST /agentwatch/api/v1/test` accepts `{}` or a validated optional `source`
  hint (`codex`, `zcode`, `kimi`, `grok`, or `other`) and requires
  `Authorization: Bearer <app_token>`. The client cannot choose notification
  content. The service publishes one fixed test notification with
  `X-Sequence-ID`, the matching source tag, and a device-only target tag through
  the server's private write-only publisher token. The target is exactly
  `target_<sha256(device_id UTF-8)[:24]>`; it does not reveal the original device
  ID. Every client receives the shared-topic frame, but only the matching
  Android installation may render or acknowledge it. A global 10-per-minute
  test limit and a per-device 3-per-minute limit prevent test-notification spam.
  The message uses normal ntfy caching so resume behavior is identical to
  production notifications. Its response returns the stable value as
  `event_id` and, for compatibility, `sequence_id`, plus `target_tag`.
- `POST /agentwatch/api/v1/ack` requires `event_id` (legacy `sequence_id` is
  accepted) and the same Bearer authentication. Optional `message_id`, `source`,
  `received_at`, and `app_version` diagnostics are validated but deliberately
  not retained. The database records device, event ID, and server time;
  there is deliberately no title, body, or message-content column. These
  delivery-only rows are automatically deleted after seven days.
- `GET /agentwatch/api/v1/health` checks SQLite availability.

Passwords use `hashlib.scrypt` with a random 16-byte salt. The service stores
only SHA-256 digests of app tokens and never stores ntfy tokens. The ntfy token
is managed by ntfy's own authentication database. Login rotates the ntfy token
identified by a deterministic, non-identifying device label. Authentication,
invitation, and token-digest comparisons use `hmac.compare_digest`; route and
per-device sliding-window limits provide basic brute-force and spam control.
The service caps the shared subscriber account at 50 installations, leaving
headroom below ntfy's 60-token-per-user limit for create-before-revoke rotation.

## Prerequisites

The live ntfy setup must already contain these non-admin users and ACLs:

```bash
sudo -u ntfy -- /usr/bin/ntfy access agent-watch-subscriber
sudo -u ntfy -- /usr/bin/ntfy access agent-watch-publisher
```

`agent-watch-subscriber` must have only `ro` access to `agent-watch`, while the
publisher must have only `wo`. ntfy tokens inherit all permissions of their
account, so do not make either account an administrator.

Create a dedicated publisher token and a high-entropy invitation code. Do not
paste either value into this repository, shell history, issue trackers, or
deployment logs. The committed `service.env.example` contains placeholders
only. The deployed secrets file and SQLite files must never enter Git.

## Install on HK_VPS

Copy the reviewed service files without copying tests or a local database:

```bash
sudo install -d -o root -g root -m 0755 /opt/agentwatch-registration
sudo install -o root -g root -m 0644 server.py /opt/agentwatch-registration/server.py
sudo install -o root -g root -m 0644 maintenance.py /opt/agentwatch-registration/maintenance.py
sudo install -d -o root -g ntfy -m 0750 /etc/agentwatch-registration
sudo install -o root -g root -m 0644 agentwatch-registration.service.example \
  /etc/systemd/system/agentwatch-registration.service
sudoedit /etc/agentwatch-registration/service.env
sudo chown root:ntfy /etc/agentwatch-registration/service.env
sudo chmod 0640 /etc/agentwatch-registration/service.env
```

Use the committed example as a reference and enter the real values directly
inside `/etc/agentwatch-registration/service.env`; never create a populated
`service.env` inside the checkout. Generate the invite with a cryptographically
secure tool, and place the existing **write-only** publisher token in the file.
Do not use the subscriber token or expose the publisher token to Android clients.

The service runs as the package-created `ntfy` user because `/usr/bin/ntfy token
add` must update `/var/lib/ntfy/user.db`. Its systemd sandbox grants writes only
to `/var/lib/agentwatch-registration` and `/var/lib/ntfy`, allows network access
only to localhost, drops every capability, and caps memory/tasks. If the live
ntfy state directory differs, adjust the one exact `ReadWritePaths` entry; do
not broaden it to `/var/lib`.

Replace the current :9444 Caddy site's single ntfy proxy with the ordered
`handle` layout from `Caddyfile.example`, then validate before reload:

```bash
sudo /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now agentwatch-registration.service
sudo systemctl reload caddy
sudo systemctl status --no-pager agentwatch-registration.service
curl --fail --silent --show-error \
  https://64.90.8.184:9444/agentwatch/api/v1/health
```

Do not open firewall port 2587. Both 2586 and 2587 must remain loopback-only.
Do not remove the Caddy `/v1/account*` denial: ntfy 2.26.3 tokens have full
account-level token-management access, so exposing that endpoint would let one
installation enumerate or revoke the other installations' subscriber tokens.
The Caddy site has no access log; keep it that way so Authorization headers are
not copied into a request log. The Python service logs only HTTP methods, fixed
paths, and sanitized event names—never bodies, passwords, invite codes, or
tokens.

## Test locally

No third-party packages or live ntfy instance are required:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py
```

Unit tests use fake token/publish adapters. Production validation should then
perform exactly one registration, one `/test`, and one `/ack`, followed by a
read-only check that the Android client received the matching sequence ID.

## Rotation and recovery

A successful login issues a new token before deleting the old token, avoiding
an authentication gap. If old-token cleanup fails, the service returns the new
working credentials and logs only `ntfy_old_token_cleanup_failed`; remove stale
tokens later with `sudo -u ntfy -- /usr/bin/ntfy token list
agent-watch-subscriber`. Never paste token-list output into tickets or logs.

Before deployment, back up the existing Caddy configuration and take a
transactionally consistent SQLite backup of the ntfy auth DB into a root-only
location. A normal rollback restores and validates the previous Caddyfile,
stops/disables this service, and removes only the exact dedicated publisher
token created for AgentWatch; it leaves ntfy and the rest of its auth DB
running. Never copy an old `user.db` over the live database. Restoring the full
auth backup is disaster recovery only: stop both AgentWatch and ntfy first,
restore the database while no writer is running, then start ntfy again. The
registration DB contains password hashes and app-token hashes and must still be
treated as sensitive authentication data.

Android creates a new device ID after uninstalling or clearing app data. Revoke
the abandoned installation so its app token, ntfy token, and capacity slot do
not remain valid. Stop the API to avoid racing a simultaneous login, inspect the
account's exact device IDs, revoke one explicit target, and restart:

```bash
sudo systemctl stop agentwatch-registration.service
sudo -u ntfy -- /usr/bin/python3 -I /opt/agentwatch-registration/maintenance.py \
  list-devices alice
sudo -u ntfy -- /usr/bin/python3 -I /opt/agentwatch-registration/maintenance.py \
  revoke-device alice device-id-from-the-list --yes
sudo systemctl start agentwatch-registration.service
```

The command never prints an app or ntfy token. `revoke-device` first removes all
ntfy tokens bearing that installation's derived label, then deletes its hashed
app-token row. If it fails, leave the API stopped, resolve the reported ntfy or
SQLite error, and rerun the same exact target; do not broaden the deletion.
