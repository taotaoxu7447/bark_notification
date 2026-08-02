#!/usr/bin/env python3
"""Offline device inventory and revocation for AgentWatch administrators."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
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
            SELECT devices.device_id, devices.device_name, devices.last_login_at
            FROM devices JOIN users ON users.id = devices.user_id
            WHERE users.username = ?
            ORDER BY devices.last_login_at DESC, devices.id DESC
            """,
            (username,),
        ).fetchall()
    for row in rows:
        print(f"{row['device_id']}\t{row['device_name']}\tlast_seen={row['last_login_at']}")
    return 0 if rows else 1


def revoke_device(database: Database, manager: NtfyTokenManager, username: str, device_id: str) -> int:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT devices.id
            FROM devices JOIN users ON users.id = devices.user_id
            WHERE users.username = ? AND devices.device_id = ?
            """,
            (username, device_id),
        ).fetchone()
    if row is None:
        raise ValueError("device was not found for that username")

    removed_tokens = manager.revoke_device(device_id)
    with database.connect() as connection:
        cursor = connection.execute(
            "DELETE FROM devices WHERE id = ?",
            (row["id"],),
        )
        connection.commit()
    if cursor.rowcount != 1:
        raise RuntimeError("device changed while it was being revoked")
    print(f"revoked device {device_id}; ntfy_tokens_removed={removed_tokens}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentWatch device maintenance")
    subcommands = parser.add_subparsers(dest="command", required=True)
    listing = subcommands.add_parser("list-devices")
    listing.add_argument("username")
    revocation = subcommands.add_parser("revoke-device")
    revocation.add_argument("username")
    revocation.add_argument("device_id")
    revocation.add_argument("--yes", action="store_true", help="confirm irreversible revocation")
    arguments = parser.parse_args()

    try:
        username = validate_username(arguments.username)
        config = configuration()
        if not config.database_path.is_file():
            raise ValueError("registration database does not exist")
        database = Database(config.database_path)
        if arguments.command == "list-devices":
            return list_devices(database, username)
        if not arguments.yes:
            raise ValueError("revoke-device requires --yes")
        device_id = validate_device_id(arguments.device_id)
        return revoke_device(database, NtfyTokenManager(config), username, device_id)
    except (OSError, sqlite3.Error, ValueError, RuntimeError, ProvisioningError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
