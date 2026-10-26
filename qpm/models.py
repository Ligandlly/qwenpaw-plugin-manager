# -*- coding: utf-8 -*-
"""Plain data models shared by discovery, planning, execution and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActionKind(str, Enum):
    """What the executor should do for one (runtime, plugin) pair."""

    INSTALL = "install"
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"
    REINSTALL = "reinstall"
    UNINSTALL = "uninstall"
    UNLOAD = "unload"
    ENABLE = "enable"
    DISABLE = "disable"
    CHECK = "check"
    SKIP = "skip"
    RUNTIME = "runtime"


#: Actions that change plugin files on the runtime.
MUTATING_KINDS = frozenset(
    {
        ActionKind.INSTALL,
        ActionKind.UPGRADE,
        ActionKind.DOWNGRADE,
        ActionKind.REINSTALL,
        ActionKind.UNINSTALL,
        ActionKind.UNLOAD,
        ActionKind.ENABLE,
        ActionKind.DISABLE,
    },
)

#: Actions that replace the plugin directory and can be rolled back.
REPLACING_KINDS = frozenset(
    {
        ActionKind.UPGRADE,
        ActionKind.DOWNGRADE,
        ActionKind.REINSTALL,
        ActionKind.UNINSTALL,
    },
)


class ActionState(str, Enum):
    """Outcome of one planned action."""

    PLANNED = "planned"
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class RuntimeTarget:
    """One managed QwenPaw runtime and how to reach it."""

    runtime_id: str
    base_url: str
    token: str
    tenant_id: str = ""
    owner_id: str = ""
    state: str = "running"
    image: str = ""
    container_id: str = ""
    container_name: str = ""
    workspace_container_path: str = ""
    plugins_dir_host: str = ""
    instance: str = ""
    source: str = "docker"

    @property
    def running(self) -> bool:
        """Return whether the runtime is currently able to serve the API."""
        return self.state == "running"

    @property
    def label(self) -> str:
        """Short human-readable identity used in tables and messages."""
        return self.runtime_id

    @property
    def key(self) -> str:
        """Stable identity for grouping actions (id plus endpoint)."""
        return f"{self.runtime_id}|{self.base_url}"

    @property
    def container_plugins_dir(self) -> str:
        """Path of the plugins directory *inside* the runtime container."""
        workspace = self.workspace_container_path or "/workspace"
        return f"{workspace.rstrip('/')}/plugins"


@dataclass(frozen=True)
class InstalledPlugin:
    """One plugin as reported by ``GET /api/plugins``."""

    plugin_id: str
    version: str
    enabled: bool = True
    loaded: bool = False
    status: str = ""
    plugin_type: str = ""

    @property
    def healthy(self) -> bool:
        """Return whether the plugin is loaded without a failed status."""
        return self.status not in {"failed", "error"}


@dataclass(frozen=True)
class PluginBundle:
    """A distributable plugin snapshot (always a ZIP payload)."""

    plugin_id: str
    version: str
    data: bytes
    origin: str
    name: str = ""
    plugin_type: str = ""
    sdk_version: str = ""
    qwenpaw_min: str = ""
    qwenpaw_max: str = ""
    sha256: str = ""
    description: str = ""

    @property
    def size(self) -> int:
        """Return the ZIP payload size in bytes."""
        return len(self.data)

    @property
    def filename(self) -> str:
        """Return a safe upload filename accepted by the runtime."""
        safe = "".join(
            char
            for char in self.plugin_id
            if char.isalnum() or char in {"-", "_", "."}
        )
        return f"{safe or 'plugin'}-{self.version or '0.0.0'}.zip"


@dataclass
class Action:
    """One planned operation against one runtime."""

    kind: ActionKind
    target: RuntimeTarget
    plugin_id: str
    current_version: str | None = None
    target_version: str | None = None
    reason: str = ""
    warning: str = ""
    force: bool = False
    origin: str = ""
    depends_on: str | None = None
    optional: bool = False

    @property
    def mutating(self) -> bool:
        """Return whether this action changes the runtime."""
        return self.kind in MUTATING_KINDS

    @property
    def transition(self) -> str:
        """Render the version transition for reports."""
        if self.kind in {
            ActionKind.INSTALL,
            ActionKind.UPGRADE,
            ActionKind.DOWNGRADE,
            ActionKind.REINSTALL,
        }:
            return f"{self.current_version or '-'} -> {self.target_version or '-'}"
        if self.kind in {ActionKind.UNINSTALL, ActionKind.UNLOAD}:
            return f"{self.current_version or '-'} -> -"
        return self.current_version or "-"


@dataclass
class ActionResult:
    """Outcome of executing (or deliberately skipping) one action."""

    action: Action
    state: ActionState
    message: str = ""
    http_status: int | None = None
    duration_ms: int = 0
    attempts: int = 1
    backup_path: str = ""
    rollback: str = ""
    details: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return whether this result should not fail the run."""
        return self.state in {ActionState.OK, ActionState.SKIPPED}


@dataclass
class RuntimeSnapshot:
    """Inventory of one runtime: app version plus installed plugins."""

    target: RuntimeTarget
    app_version: str | None = None
    upload_limit_mb: int | None = None
    plugins: dict[str, InstalledPlugin] = field(default_factory=dict)
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def reachable(self) -> bool:
        """Return whether the inventory request succeeded."""
        return not self.error
