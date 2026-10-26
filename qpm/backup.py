# -*- coding: utf-8 -*-
"""Host-side snapshots of a plugin directory, used for rollback.

Hub mounts each runtime's working directory from the host, so the plugin
directory the runtime sees as ``<workspace>/plugins/<id>`` is also visible to
this tool as ``target.plugins_dir_host/<id>``.  Copying it before a destructive
operation is cheap (plugins are small) and gives a real rollback: restore the
files, then ask the runtime to re-install from the container path.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .models import RuntimeTarget

__all__ = [
    "Backup",
    "BackupOutcome",
    "default_backup_root",
    "create_backup",
    "restore_backup",
]


def default_backup_root() -> Path:
    """Return the default snapshot root (``~/.qpm/backups``)."""
    return Path.home() / ".qpm" / "backups"


@dataclass(frozen=True)
class Backup:
    """One plugin directory snapshot on the Hub host."""

    plugin_id: str
    version: str
    source_dir: Path
    backup_dir: Path
    container_path: str


@dataclass(frozen=True)
class BackupOutcome:
    """Result of taking a snapshot, including why it was skipped."""

    backup: Backup | None
    note: str = ""


def create_backup(
    target: RuntimeTarget,
    plugin_id: str,
    version: str,
    *,
    backup_root: Path | None = None,
    stamp: str | None = None,
) -> BackupOutcome:
    """Copy ``plugins/<id>`` out of the runtime's host directory."""
    if not target.plugins_dir_host:
        return BackupOutcome(
            None,
            "no host plugins directory known (inventory file may omit "
            "plugins_dir)",
        )
    source = Path(target.plugins_dir_host) / plugin_id
    if not source.is_dir():
        return BackupOutcome(
            None,
            f"plugin directory not visible on the host: {source}",
        )
    root = (backup_root or default_backup_root()).expanduser()
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    destination = root / stamp / target.runtime_id / plugin_id
    if destination.exists():
        suffix = 1
        while destination.with_name(f"{plugin_id}-{suffix}").exists():
            suffix += 1
        destination = destination.with_name(f"{plugin_id}-{suffix}")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    except OSError as exc:
        return BackupOutcome(None, f"backup failed: {exc}")
    return BackupOutcome(
        Backup(
            plugin_id=plugin_id,
            version=version,
            source_dir=source,
            backup_dir=destination,
            container_path=f"{target.container_plugins_dir}/{plugin_id}",
        ),
    )


def restore_backup(backup: Backup) -> None:
    """Replace the plugin directory with the snapshot contents."""
    if not backup.backup_dir.is_dir():
        raise OSError(f"backup is missing: {backup.backup_dir}")
    if backup.source_dir.exists():
        shutil.rmtree(backup.source_dir)
    shutil.copytree(backup.backup_dir, backup.source_dir)
