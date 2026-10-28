# -*- coding: utf-8 -*-
"""Display-only Hub username resolution (the runtime id stays the identity)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import qpm.hub_users as hub_users
from qpm.cli import main
from qpm.hub_users import HubUserDirectory, find_hub_database
from qpm.sources import DiscoveryOptions, discover_runtimes


def _write_hub_db(path: Path, users: dict[str, str]) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE hub_users ("
            "user_id TEXT PRIMARY KEY, username TEXT NOT NULL, "
            "deleted_at TEXT)",
        )
        connection.executemany(
            "INSERT INTO hub_users (user_id, username) VALUES (?, ?)",
            list(users.items()),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _write_inventory(path: Path, entries: list[dict]) -> Path:
    path.write_text(
        json.dumps({"runtimes": entries}, indent=2),
        encoding="utf-8",
    )
    return path


def _entry(runtime_id: str, **extra) -> dict:
    return {
        "runtime_id": runtime_id,
        "url": f"http://127.0.0.1/{runtime_id}",
        "token": "token",
        "owner_id": f"user-{runtime_id}",
        **extra,
    }


def test_directory_maps_user_ids_to_usernames(tmp_path: Path) -> None:
    database = _write_hub_db(
        tmp_path / "control.db",
        {"user-rt-a": "alice", "user-rt-b": "bob"},
    )
    directory = HubUserDirectory(database)
    assert directory.available is True
    assert directory.username("user-rt-a") == "alice"
    assert directory.username("user-missing") == ""
    assert directory.username("") == ""


def test_directory_accepts_the_hub_root_directory(tmp_path: Path) -> None:
    root = tmp_path / "hub"
    root.mkdir()
    _write_hub_db(root / "control.db", {"user-rt-a": "alice"})
    assert find_hub_database(root) == root / "control.db"
    assert HubUserDirectory(root).username("user-rt-a") == "alice"


def test_directory_ignores_deleted_accounts(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE hub_users (user_id TEXT PRIMARY KEY, "
            "username TEXT NOT NULL, deleted_at TEXT)",
        )
        connection.execute(
            "INSERT INTO hub_users (user_id, username, deleted_at) "
            "VALUES ('user-rt-a', 'alice', '2026-01-01T00:00:00Z')",
        )
        connection.commit()
    finally:
        connection.close()
    assert HubUserDirectory(database).username("user-rt-a") == ""


def test_directory_without_a_database_is_harmless(monkeypatch) -> None:
    monkeypatch.delenv("QPM_HUB_DB", raising=False)
    monkeypatch.delenv("QWENPAW_HUB_DIR", raising=False)
    monkeypatch.delenv("QWENPAW_WORKING_DIR", raising=False)
    monkeypatch.setattr(
        hub_users,
        "hub_database_candidates",
        lambda explicit=None: [],
    )
    directory = HubUserDirectory()
    assert directory.available is False
    assert directory.username("user-rt-a") == ""


def test_directory_reads_the_environment_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = _write_hub_db(tmp_path / "control.db", {"user-rt-a": "alice"})
    monkeypatch.setenv("QPM_HUB_DB", str(database))
    assert HubUserDirectory().username("user-rt-a") == "alice"


def test_discovery_attaches_usernames_for_display(tmp_path: Path) -> None:
    database = _write_hub_db(
        tmp_path / "control.db",
        {"user-rt-a": "alice"},
    )
    inventory = _write_inventory(
        tmp_path / "inventory.json",
        [_entry("rt-a"), _entry("rt-b")],
    )
    result = discover_runtimes(
        DiscoveryOptions(inventory=inventory, hub_db=database),
    )
    by_id = {target.runtime_id: target for target in result.targets}
    assert by_id["rt-a"].username == "alice"
    assert by_id["rt-a"].display_name == "alice"
    assert by_id["rt-b"].username == ""
    # The identity the backend uses never changes.
    assert by_id["rt-b"].display_name == "rt-b"


def test_inventory_username_wins_over_the_hub_lookup(tmp_path: Path) -> None:
    database = _write_hub_db(tmp_path / "control.db", {"user-rt-a": "alice"})
    inventory = _write_inventory(
        tmp_path / "inventory.json",
        [_entry("rt-a", username="override")],
    )
    result = discover_runtimes(
        DiscoveryOptions(inventory=inventory, hub_db=database),
    )
    assert result.targets[0].username == "override"
    assert result.targets[0].runtime_id == "rt-a"


def test_cli_keeps_the_username_in_json_and_saved_inventory(
    capsys,
    tmp_path: Path,
) -> None:
    database = _write_hub_db(tmp_path / "control.db", {"user-rt-a": "alice"})
    inventory = _write_inventory(
        tmp_path / "inventory.json",
        [_entry("rt-a")],
    )
    base = [
        "runtimes",
        "--inventory",
        str(inventory),
        "--hub-db",
        str(database),
    ]
    code = main([*base, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["runtimes"][0]["runtime_id"] == "rt-a"
    assert payload["runtimes"][0]["username"] == "alice"

    saved = tmp_path / "saved.json"
    assert main([*base, "--save-inventory", str(saved)]) == 0
    capsys.readouterr()
    entry = json.loads(saved.read_text(encoding="utf-8"))["runtimes"][0]
    assert entry["username"] == "alice"
