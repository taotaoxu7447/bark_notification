# HK_VPS ntfy deployment

The official project subscription endpoint is:

```text
https://64.90.8.184:9444/agent-watch
```

The URL and topic are public metadata. They are not credentials. The server uses
`auth-default-access: deny-all`; publishers receive a write-only account/token and
Android clients receive a separate read-only account. Never commit a token,
password, password hash, `user.db`, or `cache.db`.

## Production layout

- ntfy `v2.26.3` from the official Linux amd64 Debian package; SHA256 is `fdfcb5f4f3318d2c35dd7edaa351abe4637eb53e7641245f9718cf7a2c0342f4`
- ntfy listens only on `127.0.0.1:2586`
- Caddy exposes TLS on `https://64.90.8.184:9444`
- the persistent nftables policy allows TCP/UDP `9444`; port `2586` remains loopback-only
- the existing sing-box listeners on `443` and Caddy site on `9443` are unchanged
- messages are cached for at most six hours; attachments and the web console are disabled
- `agent-watch-publisher` has `wo` access to `agent-watch`
- `agent-watch-subscriber` has `ro` access to `agent-watch`

The live server files are based on `server.yml.example`, `Caddyfile.example`, and
`hardening.conf.example`. Caddy configuration must be validated before reload.

## Client setup

New repository installs already inherit the official URL from `env.example`.
Existing installs must set both values in their private configuration:

```bash
NTFY_URL=https://64.90.8.184:9444/agent-watch
NTFY_TOKEN=<publisher-token-from-administrator>
```

On Android, add the server `https://64.90.8.184:9444`, sign in with the read-only
subscriber account supplied privately by the administrator, and subscribe to
`agent-watch`. Do not reuse publisher credentials on a receiving device.
On ColorOS and similar systems, set ntfy's power-use management to fully allow
background behavior; the standard battery-optimization exemption alone may not
prevent the vendor freezer from stopping the live subscription. Enable
WebSockets when the ntfy client offers it; the Caddy proxy supports the upgrade.

## Access administration

Run access commands as the package-created `ntfy` service user so the SQLite
database remains writable by the service:

```bash
sudo -u ntfy -- /usr/bin/ntfy user list
sudo -u ntfy -- /usr/bin/ntfy access
sudo -u ntfy -- /usr/bin/ntfy token list agent-watch-publisher
```

Tokens inherit all permissions of their user, so each automation should use a
dedicated non-admin user with only the required topic ACL. Rotate a publisher
token by adding a replacement, updating clients, verifying one bounded test, and
then removing the old token.

## Verification and rollback

Before each deployment, keep a root-only backup of `/etc/caddy/Caddyfile` and any
existing `/etc/ntfy` and systemd overrides. Verify all of the following:

```bash
sudo /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl is-active ntfy caddy
sudo ss -lntp
curl --fail --silent --show-error https://64.90.8.184:9444/v1/health
```

The server health timer also checks `ntfy`, loopback port `2586`, Caddy, and public
port `9444`. The Caddy site does not enable an access log, so Authorization headers
and topic paths are not written to a separate request log.

If the ntfy site fails, restore the saved Caddyfile, validate it, reload Caddy,
and stop/disable ntfy. Do not change sing-box or its port `443` while rolling back
this service.
