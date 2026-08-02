# HK_VPS ntfy deployment

The public relay base is:

```text
https://64.90.8.184:9444
```

The server uses `auth-default-access: deny-all`. v0.2 creates a random private
topic and a separate non-admin read-only ntfy principal for every AgentWatch
account. Topic names are not authorization: only the account's per-installation
read tokens may subscribe. The internal publisher remains non-admin and receives
an exact write-only ACL for each private topic. Never commit a topic together
with a token, password, password hash, `user.db`, or `cache.db`.

## Production layout

- ntfy `v2.26.3` from the official Linux amd64 Debian package; SHA256 is `fdfcb5f4f3318d2c35dd7edaa351abe4637eb53e7641245f9718cf7a2c0342f4`
- ntfy listens only on `127.0.0.1:2586`
- Caddy exposes TLS on `https://64.90.8.184:9444`
- the persistent nftables policy allows TCP/UDP `9444`; port `2586` remains loopback-only
- the existing sing-box listeners on `443` and Caddy site on `9443` are unchanged
- messages are cached for at most six hours; attachments and the web console are disabled
- `agent-watch-publisher` has exact `wo` ACLs for provisioned private topics
- every random `awu...` account principal has `ro` access to exactly one topic
- legacy `agent-watch` ACLs remain only during the controlled v0.1 migration

The live server files are based on `server.yml.example`, `Caddyfile.example`, and
`hardening.conf.example`. Caddy configuration must be validated before reload.

## Client setup

New desktop installs log in through the AgentWatch CLI. They receive an opaque
computer token for the AgentWatch `/publish` API, not an ntfy token or topic.
Android registration/login receives its private relay URL, topic, and read token
dynamically. Users must not configure a shared topic manually.

The following direct ntfy configuration is legacy-only and must not be issued
to new computers:

```bash
NTFY_URL=https://64.90.8.184:9444/agent-watch
NTFY_TOKEN=<publisher-token-from-administrator>
```

The custom AgentWatch Android app uses authenticated WebSockets. On ColorOS and
similar systems, users must still allow background behavior/autostart; the
standard battery-optimization exemption alone may not prevent a vendor freezer.

## Access administration

Run access commands as the package-created `ntfy` service user so the SQLite
database remains writable by the service:

```bash
sudo -u ntfy -- /usr/bin/ntfy user list
sudo -u ntfy -- /usr/bin/ntfy access
sudo -u ntfy -- /usr/bin/ntfy token list agent-watch-publisher
```

Tokens inherit all permissions of their user. Never make the publisher or a
random subscriber principal an admin. The registration service safely supplies
the random internal ntfy-user password through `NTFY_PASSWORD`, then creates an
exact reader ACL and publisher writer ACL. See the registration-service README
for per-device legacy audit/revocation and the guarded final shared-ACL reset.

## Verification and rollback

Before each deployment, keep a root-only backup of `/etc/caddy/Caddyfile` and any
existing `/etc/ntfy` and systemd overrides. Verify all of the following:

```bash
sudo /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl is-active ntfy caddy
sudo ss -lntp
curl --fail --silent --show-error https://64.90.8.184:9444/v1/health
```

For one provisioned private topic, also verify anonymous read/write are 403,
the installation token reads but cannot write, and the publisher token writes
but cannot read. Use one labelled notification for this preflight; repeated
tests create needless mobile alerts. A user/token from another account must
receive 403 for this topic.

The server health timer also checks `ntfy`, loopback port `2586`, Caddy, and public
port `9444`. The Caddy site does not enable an access log, so Authorization headers
and topic paths are not written to a separate request log.

If the ntfy site fails, restore the saved Caddyfile, validate it, reload Caddy,
and stop/disable ntfy. Do not change sing-box or its port `443` while rolling back
this service.
