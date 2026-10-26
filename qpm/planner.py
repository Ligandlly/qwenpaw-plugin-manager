# -*- coding: utf-8 -*-
"""Turn inventories plus bundles into an explicit list of actions.

Planning is pure: it never touches the network or the filesystem.  That keeps
``--dry-run`` honest — the printed plan is exactly what the executor runs.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Iterable

from .models import (
    Action,
    ActionKind,
    InstalledPlugin,
    PluginBundle,
    RuntimeSnapshot,
)
from .versions import check_runtime_compat, check_sdk_compat, compare, parse_version

__all__ = [
    "SyncOptions",
    "plan_bundle",
    "plan_sync",
    "plan_uninstall",
    "plan_toggle",
    "plan_check",
    "runtime_issue_actions",
    "matches_patterns",
]


@dataclass
class SyncOptions:
    """Knobs that decide how a bundle is reconciled with a runtime."""

    #: ``sync`` converges, ``install`` only fills gaps, ``upgrade`` only
    #: touches plugins that are already present and older.
    mode: str = "sync"
    reinstall: bool = False
    allow_downgrade: bool = False
    install_missing: bool = True
    plugin_patterns: tuple[str, ...] = ()
    protected: frozenset[str] = field(default_factory=frozenset)
    host_sdk: str = "1.0.0"
    enable: bool | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"sync", "install", "upgrade"}:
            raise ValueError(f"unknown sync mode: {self.mode}")


def matches_patterns(value: str, patterns: Iterable[str]) -> bool:
    """Return whether ``value`` matches no pattern or any fnmatch pattern.

    An empty pattern collection means "match everything"; a non-empty one
    means "value must match at least one", which is what both ``--plugin``
    and ``--protect`` need.
    """
    items = tuple(patterns)
    if not items:
        return True
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in items)


def _is_protected(plugin_id: str, protected: Iterable[str]) -> bool:
    """Protected entries are exact ids and globs; an empty set protects none."""
    return any(
        fnmatch.fnmatchcase(plugin_id, pattern)
        for pattern in tuple(protected)
    )


def _same_version(left: str, right: str) -> bool:
    left_version = parse_version(left)
    right_version = parse_version(right)
    if left_version is None or right_version is None:
        return left.strip() == right.strip()
    return compare(left_version, right_version) == 0


def _skip(
    snapshot: RuntimeSnapshot,
    bundle: PluginBundle,
    reason: str,
    *,
    current: InstalledPlugin | None = None,
    warning: str = "",
) -> Action:
    return Action(
        kind=ActionKind.SKIP,
        target=snapshot.target,
        plugin_id=bundle.plugin_id,
        current_version=current.version if current else None,
        target_version=bundle.version,
        reason=reason,
        warning=warning,
        origin=bundle.origin,
    )


def plan_bundle(
    snapshot: RuntimeSnapshot,
    bundle: PluginBundle,
    options: SyncOptions,
) -> list[Action]:
    """Plan the actions that converge one bundle onto one runtime."""
    if not matches_patterns(bundle.plugin_id, options.plugin_patterns):
        return []
    target = snapshot.target
    if _is_protected(bundle.plugin_id, options.protected):
        return [
            _skip(snapshot, bundle, "plugin is on the protected list"),
        ]
    if not target.running:
        return [
            _skip(snapshot, bundle, f"runtime is {target.state}, not running"),
        ]

    sdk_ok, sdk_reason = check_sdk_compat(bundle.sdk_version, options.host_sdk)
    if not sdk_ok:
        return [
            _skip(
                snapshot,
                bundle,
                f"incompatible SDK: {sdk_reason}",
                warning=sdk_reason,
            ),
        ]
    compat_ok, compat_note = check_runtime_compat(
        bundle.qwenpaw_min,
        bundle.qwenpaw_max,
        snapshot.app_version,
    )
    if not compat_ok:
        return [
            _skip(
                snapshot,
                bundle,
                f"incompatible runtime: {compat_note}",
                warning=compat_note,
            ),
        ]

    current = snapshot.plugins.get(bundle.plugin_id)
    actions: list[Action] = []
    if current is None:
        if options.mode == "upgrade" and not options.install_missing:
            actions.append(
                _skip(snapshot, bundle, "not installed (use --install-missing)"),
            )
        else:
            actions.append(
                Action(
                    kind=ActionKind.INSTALL,
                    target=target,
                    plugin_id=bundle.plugin_id,
                    target_version=bundle.version,
                    reason="not installed",
                    warning=compat_note,
                    force=False,
                    origin=bundle.origin,
                ),
            )
    else:
        current_version = parse_version(current.version)
        bundle_version = parse_version(bundle.version)
        relation = 0
        if current_version is not None and bundle_version is not None:
            relation = compare(current_version, bundle_version)
        elif current.version.strip() != bundle.version.strip():
            relation = -1 if current.version.strip() < bundle.version.strip() else 1

        if relation == 0:
            if options.reinstall and options.mode != "upgrade":
                actions.append(
                    Action(
                        kind=ActionKind.REINSTALL,
                        target=target,
                        plugin_id=bundle.plugin_id,
                        current_version=current.version,
                        target_version=bundle.version,
                        reason="reinstall requested",
                        warning=compat_note,
                        force=True,
                        origin=bundle.origin,
                    ),
                )
            else:
                actions.append(
                    _skip(
                        snapshot,
                        bundle,
                        "already at the target version",
                        current=current,
                    ),
                )
        elif relation < 0:
            if options.mode == "install":
                actions.append(
                    _skip(
                        snapshot,
                        bundle,
                        "already installed at a different version "
                        "(install mode does not upgrade)",
                        current=current,
                    ),
                )
            elif options.mode == "upgrade" or options.mode == "sync":
                actions.append(
                    Action(
                        kind=ActionKind.UPGRADE,
                        target=target,
                        plugin_id=bundle.plugin_id,
                        current_version=current.version,
                        target_version=bundle.version,
                        reason="newer bundle available",
                        warning=compat_note,
                        force=True,
                        origin=bundle.origin,
                    ),
                )
        else:
            if options.allow_downgrade:
                actions.append(
                    Action(
                        kind=ActionKind.DOWNGRADE,
                        target=target,
                        plugin_id=bundle.plugin_id,
                        current_version=current.version,
                        target_version=bundle.version,
                        reason="downgrade explicitly allowed",
                        warning=compat_note,
                        force=True,
                        origin=bundle.origin,
                    ),
                )
            else:
                actions.append(
                    _skip(
                        snapshot,
                        bundle,
                        f"installed version {current.version} is newer than "
                        f"the bundle (use --allow-downgrade)",
                        current=current,
                    ),
                )

    if options.enable is not None:
        desired = bool(options.enable)
        will_exist = current is not None or any(
            action.kind
            in {
                ActionKind.INSTALL,
                ActionKind.REINSTALL,
                ActionKind.UPGRADE,
                ActionKind.DOWNGRADE,
            }
            for action in actions
        )
        enabled_now = current.enabled if current is not None else True
        if will_exist and (current is None or enabled_now != desired):
            actions.append(
                Action(
                    kind=ActionKind.ENABLE if desired else ActionKind.DISABLE,
                    target=target,
                    plugin_id=bundle.plugin_id,
                    current_version=current.version if current else None,
                    target_version=bundle.version,
                    reason=(
                        "set enabled after sync"
                        if desired
                        else "set disabled after sync"
                    ),
                    depends_on=bundle.plugin_id,
                    origin=bundle.origin,
                ),
            )
    return actions


def plan_uninstall(
    snapshot: RuntimeSnapshot,
    plugin_ids: list[str],
    *,
    keep_files: bool = False,
    protected: frozenset[str] = frozenset(),
) -> list[Action]:
    """Plan uninstall (or unload-only) actions for explicit plugin ids."""
    actions: list[Action] = []
    for plugin_id in plugin_ids:
        current = snapshot.plugins.get(plugin_id)
        if _is_protected(plugin_id, protected):
            actions.append(
                Action(
                    kind=ActionKind.SKIP,
                    target=snapshot.target,
                    plugin_id=plugin_id,
                    current_version=current.version if current else None,
                    reason="plugin is on the protected list",
                ),
            )
            continue
        if current is None:
            actions.append(
                Action(
                    kind=ActionKind.SKIP,
                    target=snapshot.target,
                    plugin_id=plugin_id,
                    reason="not installed",
                ),
            )
            continue
        actions.append(
            Action(
                kind=ActionKind.UNLOAD if keep_files else ActionKind.UNINSTALL,
                target=snapshot.target,
                plugin_id=plugin_id,
                current_version=current.version,
                reason="requested by operator",
            ),
        )
    return actions


def plan_toggle(
    snapshot: RuntimeSnapshot,
    plugin_ids: list[str],
    *,
    enabled: bool,
    protected: frozenset[str] = frozenset(),
) -> list[Action]:
    """Plan enable/disable actions for explicit plugin ids."""
    actions: list[Action] = []
    for plugin_id in plugin_ids:
        current = snapshot.plugins.get(plugin_id)
        if _is_protected(plugin_id, protected):
            actions.append(
                Action(
                    kind=ActionKind.SKIP,
                    target=snapshot.target,
                    plugin_id=plugin_id,
                    current_version=current.version if current else None,
                    reason="plugin is on the protected list",
                ),
            )
            continue
        if current is None:
            actions.append(
                Action(
                    kind=ActionKind.SKIP,
                    target=snapshot.target,
                    plugin_id=plugin_id,
                    reason="not installed",
                ),
            )
            continue
        if current.enabled == enabled:
            actions.append(
                Action(
                    kind=ActionKind.SKIP,
                    target=snapshot.target,
                    plugin_id=plugin_id,
                    current_version=current.version,
                    reason=f"already {'enabled' if enabled else 'disabled'}",
                ),
            )
            continue
        actions.append(
            Action(
                kind=ActionKind.ENABLE if enabled else ActionKind.DISABLE,
                target=snapshot.target,
                plugin_id=plugin_id,
                current_version=current.version,
                reason="requested by operator",
            ),
        )
    return actions


def plan_check(
    snapshot: RuntimeSnapshot,
    plugin_id: str,
    desired_version: str,
    *,
    optional: bool = False,
) -> Action:
    """Plan a source-less drift check used by ``apply`` for pinned entries."""
    current = snapshot.plugins.get(plugin_id)
    kind = ActionKind.CHECK
    if current is None:
        return Action(
            kind=kind,
            target=snapshot.target,
            plugin_id=plugin_id,
            target_version=desired_version,
            reason="missing and no source in the state file",
            optional=optional,
        )
    if _same_version(current.version, desired_version):
        return Action(
            kind=ActionKind.SKIP,
            target=snapshot.target,
            plugin_id=plugin_id,
            current_version=current.version,
            target_version=desired_version,
            reason="matches the declared version",
            optional=optional,
        )
    return Action(
        kind=kind,
        target=snapshot.target,
        plugin_id=plugin_id,
        current_version=current.version,
        target_version=desired_version,
        reason=(
            f"version drift: installed {current.version}, declared "
            f"{desired_version}, no source in the state file"
        ),
        optional=optional,
    )


def plan_sync(
    snapshots: list[RuntimeSnapshot],
    bundles: dict[str, PluginBundle],
    options: SyncOptions,
) -> list[Action]:
    """Plan every bundle against every reachable runtime."""
    actions: list[Action] = []
    for snapshot in snapshots:
        if snapshot.error:
            continue
        for bundle in bundles.values():
            actions.extend(plan_bundle(snapshot, bundle, options))
    return actions


def runtime_issue_actions(snapshots: list[RuntimeSnapshot]) -> list[Action]:
    """Surface runtime-level inventory failures as first-class results.

    A runtime that is merely stopped is reported as skipped (the operator
    asked for it explicitly); a running runtime that cannot be inventoried is
    a real failure.
    """
    actions: list[Action] = []
    for snapshot in snapshots:
        if not snapshot.error:
            continue
        actions.append(
            Action(
                kind=(
                    ActionKind.RUNTIME
                    if snapshot.target.running
                    else ActionKind.SKIP
                ),
                target=snapshot.target,
                plugin_id="-",
                reason=snapshot.error,
            ),
        )
    return actions
