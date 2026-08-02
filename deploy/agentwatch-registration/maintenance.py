#!/usr/bin/env python3
"""Offline device inventory and revocation for AgentWatch administrators."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

# `python3 -I script.py` deliberately omits the script directory from sys.path.
# Add this one resolved sibling directory explicitly so the documented isolated
# maintenance command can import the reviewed production module and nothing else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import (
    Config,
    Database,
    DEVICE_ID_PATTERN,
    NtfyTokenManager,
    ProvisioningError,
    USERNAME_PATTERN,
)


def configuration() -> Config:
    # The maintenance commands need no invite or publisher secret. The two
    # placeholders below are never used or sent to a child process.
    return Config(
        database_path=Path(
            os.environ.get(
                "AGENTWATCH_DATABASE_PATH", "/var/lib/agentwatch-registration/registration.db"
            )
        ),
        invite_code="unused-maintenance-value",
        publisher_token="tk_" + "0" * 29,
        ntfy_binary=os.environ.get("AGENTWATCH_NTFY_BINARY", "/usr/bin/ntfy"),
        ntfy_config_file=os.environ.get(
            "AGENTWATCH_NTFY_CONFIG_FILE", "/etc/ntfy/server.yml"
        ),
        ntfy_subscriber_user=os.environ.get(
            "AGENTWATCH_NTFY_SUBSCRIBER_USER", "agent-watch-subscriber"
        ),
        ntfy_publisher_user=os.environ.get(
            "AGENTWATCH_NTFY_PUBLISHER_USER", "agent-watch-publisher"
        ),
    )


def validate_username(value: str) -> str:
    normalized = value.lower()
    if not 3 <= len(normalized) <= 32 or not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("invalid username")
    return normalized


def validate_device_id(value: str) -> str:
    if not 8 <= len(value) <= 128 or not DEVICE_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid device ID")
    return value


def list_devices(database: Database, username: str) -> int:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT devices.device_id, devices.device_name, devices.last_login_at,
                   devices.private_ready_at
            FROM devices JOIN users ON users.id = devices.user_id
            WHERE users.username = ?
            ORDER BY devices.last_login_at DESC, devices.id DESC
            """,
            (username,),
        ).fetchall()
    for row in rows:
        print(
            f"{row['device_id']}\t{row['device_name']}\tlast_seen={row['last_login_at']}\t"
            f"private_ready={'yes' if row['private_ready_at'] is not None else 'no'}"
        )
    return 0 if rows else 1


def list_computers(database: Database, username: str) -> int:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT computers.computer_id, computers.computer_name, computers.platform,
                   computers.last_seen_at, computers.revoked_at
            FROM computers JOIN users ON users.id = computers.user_id
            WHERE users.username = ?
            ORDER BY computers.last_seen_at DESC, computers.id DESC
            """,
            (username,),
        ).fetchall()
    for row in rows:
        state = "revoked" if row["revoked_at"] is not None else "active"
        print(
            f"{row['computer_id']}\t{row['computer_name']}\t{row['platform']}\t"
            f"last_seen={row['last_seen_at']}\tstate={state}"
        )
    return 0 if rows else 1


def revoke_computer(database: Database, username: str, computer_id: str) -> int:
    replacement_hash = hashlib.sha256(secrets.token_bytes(32)).digest()
    with database.connect() as connection:
        cursor = connection.execute(
            """
            UPDATE computers SET revoked_at = ?, token_hash = ?
            WHERE id = (
                SELECT computers.id
                FROM computers JOIN users ON users.id = computers.user_id
                WHERE users.username = ? AND computers.computer_id = ?
                  AND computers.revoked_at IS NULL
            )
            """,
            (int(time.time()), replacement_hash, username, computer_id),
        )
        connection.commit()
    if cursor.rowcount != 1:
        raise ValueError("active computer was not found for that username")
    print(f"revoked computer {computer_id}")
    return 0


def revoke_device(database: Database, manager: NtfyTokenManager, username: str, device_id: str) -> int:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT devices.id, users.ntfy_subscriber_user
            FROM devices JOIN users ON users.id = devices.user_id
            WHERE users.username = ? AND devices.device_id = ?
            """,
            (username, device_id),
        ).fetchone()
    if row is None:
        raise ValueError("device was not found for that username")

    legacy_tokens = manager.revoke_legacy_device(device_id)
    private_tokens = 0
    if row["ntfy_subscriber_user"]:
        private_tokens = manager.revoke_device(row["ntfy_subscriber_user"], device_id)
    with database.connect() as connection:
        cursor = connection.execute(
            "DELETE FROM devices WHERE id = ?",
            (row["id"],),
        )
        connection.commit()
    if cursor.rowcount != 1:
        raise RuntimeError("device changed while it was being revoked")
    print(
        f"revoked device {device_id}; "
        f"private_tokens_removed={private_tokens}; legacy_tokens_removed={legacy_tokens}"
    )
    return 0


def audit_legacy_tokens(database: Database, manager: NtfyTokenManager) -> int:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT users.username, users.private_topic, devices.device_id,
                   devices.private_ready_at
            FROM devices JOIN users ON users.id = devices.user_id
            ORDER BY users.username, devices.device_id
            """
        ).fetchall()
    total = 0
    for row in rows:
        count = manager.legacy_token_count(row["device_id"])
        total += count
        print(
            f"{row['username']}\t{row['device_id']}\tprivate_channel="
            f"{'yes' if row['private_topic'] else 'no'}\tprivate_ready="
            f"{'yes' if row['private_ready_at'] is not None else 'no'}\tlegacy_tokens={count}"
        )
    print(f"legacy_token_total={total}")
    return 1 if total else 0


def audit_private_channels(database: Database, manager: NtfyTokenManager) -> int:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT username, private_topic, ntfy_subscriber_user
            FROM users ORDER BY username
            """
        ).fetchall()
    failures = 0
    for row in rows:
        if not row["private_topic"] or not row["ntfy_subscriber_user"]:
            print(f"{row['username']}\tprivate_channel=missing")
            failures += 1
            continue
        subscriber_ro, publisher_wo = manager.audit_channel_acl(
            row["ntfy_subscriber_user"], row["private_topic"]
        )
        if not subscriber_ro or not publisher_wo:
            failures += 1
        print(
            f"{row['username']}\tsubscriber_ro={'yes' if subscriber_ro else 'no'}\t"
            f"publisher_wo={'yes' if publisher_wo else 'no'}"
        )
    return 1 if failures else 0


def revoke_legacy_device(
    database: Database, manager: NtfyTokenManager, username: str, device_id: str
) -> int:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT users.private_topic, devices.private_ready_at
            FROM devices JOIN users ON users.id = devices.user_id
            WHERE users.username = ? AND devices.device_id = ?
            """,
            (username, device_id),
        ).fetchone()
    if row is None:
        raise ValueError("device was not found for that username")
    if not row["private_topic"]:
        raise ValueError("device account has not been migrated to a private channel")
    if row["private_ready_at"] is None:
        raise ValueError("this app installation has not received private credentials")
    removed = manager.revoke_legacy_device(device_id)
    print(f"revoked legacy token label for {device_id}; tokens_removed={removed}")
    return 0


def reset_legacy_acls(
    database: Database, manager: NtfyTokenManager, computers_confirmed: bool
) -> int:
    if not computers_confirmed:
        raise ValueError("legacy ACL reset requires --all-computers-migrated")
    with database.connect() as connection:
        unmigrated_accounts = connection.execute(
            "SELECT count(*) FROM users WHERE private_topic IS NULL OR ntfy_subscriber_user IS NULL"
        ).fetchone()[0]
        unmigrated_devices = connection.execute(
            "SELECT count(*) FROM devices WHERE private_ready_at IS NULL"
        ).fetchone()[0]
        device_ids = [row[0] for row in connection.execute("SELECT device_id FROM devices")]
        channels = connection.execute(
            "SELECT ntfy_subscriber_user, private_topic FROM users ORDER BY id"
        ).fetchall()
    if unmigrated_accounts or unmigrated_devices:
        raise ValueError("one or more app installations have not migrated to private channels")
    if any(manager.legacy_token_count(device_id) for device_id in device_ids):
        raise ValueError("legacy device tokens remain; audit and revoke them first")
    if any(
        manager.audit_channel_acl(row["ntfy_subscriber_user"], row["private_topic"])
        != (True, True)
        for row in channels
    ):
        raise ValueError("one or more private ntfy ACLs failed strict audit")
    manager.reset_legacy_acls()
    print("reset exact legacy subscriber and publisher ACLs for the shared topic")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentWatch device maintenance")
    subcommands = parser.add_subparsers(dest="command", required=True)
    listing = subcommands.add_parser("list-devices")
    listing.add_argument("username")
    computer_listing = subcommands.add_parser("list-computers")
    computer_listing.add_argument("username")
    revocation = subcommands.add_parser("revoke-device")
    revocation.add_argument("username")
    revocation.add_argument("device_id")
    revocation.add_argument("--yes", action="store_true", help="confirm irreversible revocation")
    subcommands.add_parser("audit-legacy-tokens")
    legacy_revocation = subcommands.add_parser("revoke-legacy-device")
    legacy_revocation.add_argument("username")
    legacy_revocation.add_argument("device_id")
    legacy_revocation.add_argument("--yes", action="store_true", help="confirm token revocation")
    computer_revocation = subcommands.add_parser("revoke-computer")
    computer_revocation.add_argument("username")
    computer_revocation.add_argument("computer_id")
    computer_revocation.add_argument("--yes", action="store_true", help="confirm token revocation")
    subcommands.add_parser("audit-private-channels")
    acl_reset = subcommands.add_parser("reset-legacy-acls")
    acl_reset.add_argument("--yes", action="store_true", help="confirm ACL reset")
    acl_reset.add_argument("--all-computers-migrated", action="store_true")
    arguments = parser.parse_args()

    try:
        config = configuration()
        if not config.database_path.is_file():
            raise ValueError("registration database does not exist")
        database = Database(config.database_path)
        manager = NtfyTokenManager(config)
        if arguments.command == "audit-legacy-tokens":
            return audit_legacy_tokens(database, manager)
        if arguments.command == "audit-private-channels":
            return audit_private_channels(database, manager)
        if arguments.command == "reset-legacy-acls":
            if not arguments.yes:
                raise ValueError("reset-legacy-acls requires --yes")
            return reset_legacy_acls(database, manager, arguments.all_computers_migrated)
        username = validate_username(arguments.username)
        if arguments.command == "list-devices":
            return list_devices(database, username)
        if arguments.command == "list-computers":
            return list_computers(database, username)
        if not arguments.yes:
            raise ValueError(f"{arguments.command} requires --yes")
        if arguments.command == "revoke-computer":
            computer_id = validate_device_id(arguments.computer_id)
            return revoke_computer(database, username, computer_id)
        device_id = validate_device_id(arguments.device_id)
        if arguments.command == "revoke-legacy-device":
            return revoke_legacy_device(database, manager, username, device_id)
        return revoke_device(database, manager, username, device_id)
    except (OSError, sqlite3.Error, ValueError, RuntimeError, ProvisioningError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
