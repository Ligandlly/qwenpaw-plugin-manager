# -*- coding: utf-8 -*-
"""Declarative state files: ``qpm freeze`` writes them, ``qpm apply`` reads them."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import UsageError
from .models import RuntimeSnapshot

__all__ = [
    "DesiredPlugin",
    "DesiredState",
    "load_state",
    "parse_state",
    "freeze_payload",
    "write_state",
]

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DesiredPlugin:
    """One plugin the fleet should have, optionally pinned to a runtime set."""

    plugin_id: str
    version: str = ""
    source: str = ""
    enabled: bool | None = None
    runtimes: tuple[str, ...] = ()
    optional: bool = False


@dataclass(frozen=True)
class DesiredState:
    """A whole desired plugin set."""

    plugins: tuple[DesiredPlugin, ...]
    removed: tuple[str, ...] = ()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise UsageError(message)


def _parse_entry(entry: object, index: int, path: Path) -> DesiredPlugin:
    _require(isinstance(entry, dict), f"{path}: plugins[{index}] is not an object")
    assert isinstance(entry, dict)
    plugin_id = str(entry.get("id") or entry.get("plugin_id") or "").strip()
    _require(bool(plugin_id), f"{path}: plugins[{index}] has no 'id'")
    runtimes = entry.get("runtimes") or ()
    if isinstance(runtimes, str):
        runtimes = (runtimes,)
    _require(
        isinstance(runtimes, (list, tuple))
        and all(isinstance(item, str) for item in runtimes),
        f"{path}: plugins[{index}].runtimes must be a list of strings",
    )
    enabled = entry.get("enabled")
    _require(
        enabled is None or isinstance(enabled, bool),
        f"{path}: plugins[{index}].enabled must be true or false",
    )
    optional = entry.get("optional", False)
    _require(
        isinstance(optional, bool),
        f"{path}: plugins[{index}].optional must be true or false",
    )
    return DesiredPlugin(
        plugin_id=plugin_id,
        version=str(entry.get("version") or "").strip(),
        source=str(entry.get("source") or "").strip(),
        enabled=enabled,
        runtimes=tuple(str(item) for item in runtimes),
        optional=optional,
    )


def load_state(path: Path) -> DesiredState:
    """Read and validate a state file."""
    path = Path(path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UsageError(f"{path}: cannot read state file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path}: state file is not valid JSON: {exc}") from exc
    return parse_state(payload, origin=str(path))


def parse_state(payload: object, *, origin: str = "<state>") -> DesiredState:
    """Validate an already-parsed state document."""
    path = origin
    _require(isinstance(payload, dict), f"{path}: state file must be an object")
    assert isinstance(payload, dict)
    schema = payload.get("schema_version", _SCHEMA_VERSION)
    _require(
        schema == _SCHEMA_VERSION,
        f"{path}: unsupported schema_version {schema!r} "
        f"(expected {_SCHEMA_VERSION})",
    )
    raw_plugins = payload.get("plugins", [])
    _require(isinstance(raw_plugins, list), f"{path}: 'plugins' must be a list")
    plugins = tuple(
        _parse_entry(entry, index, path)
        for index, entry in enumerate(raw_plugins)
    )
    raw_removed = payload.get("removed", [])
    _require(isinstance(raw_removed, list), f"{path}: 'removed' must be a list")
    removed = tuple(str(item).strip() for item in raw_removed if str(item).strip())

    ids = [plugin.plugin_id for plugin in plugins]
    duplicates = {item for item in ids if ids.count(item) > 1}
    _require(
        not duplicates,
        f"{path}: duplicate plugin entries: {', '.join(sorted(duplicates))}",
    )
    overlap = set(ids) & set(removed)
    _require(
        not overlap,
        f"{path}: plugins both declared and removed: "
        f"{', '.join(sorted(overlap))}",
    )
    return DesiredState(plugins=plugins, removed=removed)


def freeze_payload(snapshot: RuntimeSnapshot) -> dict[str, Any]:
    """Build a state document from one runtime's installed plugin set."""
    plugins = [
        {
            "id": plugin.plugin_id,
            "version": plugin.version,
            "enabled": plugin.enabled,
            "source": "",
        }
        for plugin in sorted(
            snapshot.plugins.values(),
            key=lambda item: item.plugin_id,
        )
    ]
    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generated_from": {
            "runtime_id": snapshot.target.runtime_id,
            "tenant_id": snapshot.target.tenant_id,
            "qwenpaw_version": snapshot.app_version or "",
        },
        "plugins": plugins,
        "removed": [],
        "_note": (
            "Fill in 'source' (directory, .zip, or URL) to let 'qpm apply' "
            "install or upgrade; entries without a source are drift-checked "
            "only."
        ),
    }


def write_state(path: Path, payload: dict[str, Any]) -> None:
    """Write a state document, creating parent directories."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
