# -*- coding: utf-8 -*-
"""Best-effort Hub username lookup for display.

The console labels a runtime with its owner's username, while every plan,
request and report keeps using the Hub runtime id (the container label).  This
module is therefore read-only and cosmetic: it opens Hub's SQLite database just
long enough to map ``hub_users.user_id`` to ``username`` and never writes to it.

Hub is not required.  When the database is absent, unreadable or has no match,
the runtime id is shown instead; ``QPM_HUB_DB``/``--hub-db`` point at an
explicit file, and the usual locations are probed automatically.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import logs

__all__ = ["HubUserDirectory", "find_hub_database"]

_LOG = logs.get_logger("hub-users")

#: Explicit database path; overrides every auto-detected location.
ENV_HUB_DB = "QPM_HUB_DB"
#: The same environment variables Hub itself uses to find its data root.
_ENV_HUB_DIR = "QWENPAW_HUB_DIR"
_ENV_WORKING_DIR = "QWENPAW_WORKING_DIR"

_DATABASE_NAME = "control.db"


def _as_database(path: Path) -> Path:
    """Accept either the database file or the directory that contains it."""
    expanded = path.expanduser()
    if expanded.name == _DATABASE_NAME:
        return expanded
    return expanded / _DATABASE_NAME


def hub_database_candidates(explicit: Path | str | None = None) -> list[Path]:
    """Return every location worth probing, most explicit first."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(_as_database(Path(explicit)))
    for variable in (ENV_HUB_DB, _ENV_HUB_DIR):
        value = os.environ.get(variable, "").strip()
        if value:
            candidates.append(_as_database(Path(value)))
    working = os.environ.get(_ENV_WORKING_DIR, "").strip()
    if working:
        candidates.append(_as_database(Path(working) / "hub"))
    for base in ("~/.qwenpaw", "~/.copaw"):
        candidates.append(_as_database(Path(base) / "hub"))
        candidates.append(_as_database(Path(base)))
    candidates.append(_as_database(Path("/app/working/hub")))
    return candidates


def find_hub_database(explicit: Path | str | None = None) -> Path | None:
    """Return the first existing Hub database, or ``None``."""
    for candidate in hub_database_candidates(explicit):
        if candidate.is_file():
            return candidate
    return None


class HubUserDirectory:
    """Read-only ``user_id -> username`` map backed by Hub's database."""

    def __init__(self, database: Path | str | None = None) -> None:
        self.path = find_hub_database(database)
        self._names: dict[str, str] | None = None

    @property
    def available(self) -> bool:
        """Return whether a Hub database was found."""
        return self.path is not None

    def username(self, user_id: str) -> str:
        """Return the username for ``user_id``, or an empty string."""
        if not user_id:
            return ""
        return self._load().get(user_id, "")

    def _load(self) -> dict[str, str]:
        if self._names is not None:
            return self._names
        names: dict[str, str] = {}
        if self.path is None:
            _LOG.debug(
                "no Hub database found; runtimes are labelled by runtime id",
            )
            self._names = names
            return names
        try:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                rows = connection.execute(
                    "SELECT user_id, username FROM hub_users "
                    "WHERE deleted_at IS NULL",
                ).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            _LOG.debug(
                "cannot read Hub users from %s: %s (showing runtime ids)",
                self.path,
                exc,
            )
        else:
            names = {
                str(user_id): str(username)
                for user_id, username in rows
                if user_id and username
            }
            _LOG.debug(
                "Hub user directory: %d account(s) from %s",
                len(names),
                self.path,
            )
        self._names = names
        return names
