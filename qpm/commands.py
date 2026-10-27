# -*- coding: utf-8 -*-
"""Command implementations: discovery, planning, confirmation, execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from . import logs
from .bundles import (
    DEFAULT_MAX_BUNDLE_BYTES,
    load_bundle_source,
)
from .errors import QpmError, SourceError, UsageError
from .executor import ExecOptions, Executor
from .inventory import fetch_inventory, fetch_snapshot
from .models import (
    Action,
    ActionResult,
    ActionKind,
    ActionState,
    PluginBundle,
    RuntimeSnapshot,
    RuntimeTarget,
)
from .planner import (
    SyncOptions,
    matches_patterns,
    plan_bundle,
    plan_check,
    plan_sync,
    plan_toggle,
    plan_uninstall,
    runtime_issue_actions,
)
from .report import Reporter
from .runtime_client import RuntimeClient
from .sources import (
    DiscoveryOptions,
    DiscoveryResult,
    discover_runtimes,
    select_targets,
)
from .state import freeze_payload, load_state, write_state
from .versions import parse_version

__all__ = [
    "cmd_apply",
    "cmd_doctor",
    "cmd_freeze",
    "cmd_install",
    "cmd_plugins",
    "cmd_runtimes",
    "cmd_serve",
    "cmd_toggle",
    "cmd_uninstall",
]

_MUTATING_LABEL = {
    ActionKind.INSTALL: "install",
    ActionKind.UPGRADE: "upgrade",
    ActionKind.DOWNGRADE: "downgrade",
    ActionKind.REINSTALL: "reinstall",
    ActionKind.UNINSTALL: "uninstall",
    ActionKind.UNLOAD: "unload",
    ActionKind.ENABLE: "enable",
    ActionKind.DISABLE: "disable",
}


# ── shared helpers ───────────────────────────────────────────────────────


def _reporter(args: argparse.Namespace) -> Reporter:
    return Reporter(
        json_output=bool(getattr(args, "json", False)),
        quiet=bool(getattr(args, "quiet", False)),
        verbose=bool(getattr(args, "verbose", False)),
    )


def _discovery_options(args: argparse.Namespace) -> DiscoveryOptions:
    inventory = getattr(args, "inventory", None)
    return DiscoveryOptions(
        inventory=Path(inventory).expanduser() if inventory else None,
        docker_bin=str(getattr(args, "docker_bin", "") or "docker"),
        docker_instance=str(getattr(args, "docker_instance", "") or ""),
        include_stopped=bool(getattr(args, "include_stopped", False)),
        container_port=int(getattr(args, "container_port", 8088) or 8088),
    )


def _exec_options(args: argparse.Namespace) -> ExecOptions:
    backup_dir = getattr(args, "backup_dir", None)
    return ExecOptions(
        dry_run=bool(getattr(args, "dry_run", False)),
        concurrency=max(1, int(getattr(args, "concurrency", 4) or 4)),
        timeout=float(getattr(args, "timeout", 30.0) or 30.0),
        operation_timeout=float(
            getattr(args, "operation_timeout", 180.0) or 180.0,
        ),
        retries=max(0, int(getattr(args, "retries", 2) or 0)),
        interrupt_tasks=bool(getattr(args, "interrupt_tasks", False)),
        backup=not bool(getattr(args, "no_backup", False)),
        backup_root=(
            Path(backup_dir).expanduser()
            if backup_dir
            else ExecOptions().backup_root
        ),
        rollback=not bool(getattr(args, "no_rollback", False)),
        verify=not bool(getattr(args, "no_verify", False)),
        fail_fast=bool(getattr(args, "fail_fast", False)),
    )


def _sync_options(args: argparse.Namespace) -> SyncOptions:
    return SyncOptions(
        mode=str(getattr(args, "mode", "sync") or "sync"),
        reinstall=bool(getattr(args, "reinstall", False)),
        allow_downgrade=bool(getattr(args, "allow_downgrade", False)),
        install_missing=bool(getattr(args, "install_missing", True)),
        plugin_patterns=tuple(getattr(args, "plugin", None) or ()),
        protected=frozenset(_split_values(getattr(args, "protect", None))),
        host_sdk=str(getattr(args, "host_sdk", "") or "1.0.0"),
        enable=getattr(args, "enable", None),
    )


def _split_values(values: Sequence[str] | None) -> list[str]:
    items: list[str] = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if part:
                items.append(part)
    return items


def _discover(args: argparse.Namespace, reporter: Reporter) -> list[RuntimeTarget]:
    result: DiscoveryResult = discover_runtimes(_discovery_options(args))
    for warning in result.warnings:
        reporter.warning(warning)
    targets, warnings = select_targets(
        result.targets,
        patterns=_split_values(getattr(args, "runtime", None)),
        tenants=_split_values(getattr(args, "tenant", None)),
        owners=_split_values(getattr(args, "owner", None)),
        include_stopped=bool(getattr(args, "include_stopped", False)),
    )
    for warning in warnings:
        reporter.warning(warning)
    if not targets:
        discovered = len(result.targets)
        raise UsageError(
            f"no runtimes selected (discovered {discovered} via "
            f"{result.source}); check --runtime/--tenant/--owner filters"
            + (
                ", or --include-stopped"
                if not getattr(args, "include_stopped", False)
                else ""
            ),
        )
    reporter.info(
        f"selected {len(targets)} runtime(s) via {result.source}",
    )
    return targets


def _snapshots(
    args: argparse.Namespace,
    targets: list[RuntimeTarget],
    reporter: Reporter,
) -> list[RuntimeSnapshot]:
    snapshots = fetch_inventory(
        targets,
        concurrency=max(1, int(getattr(args, "concurrency", 4) or 4)),
        timeout=float(getattr(args, "timeout", 30.0) or 30.0),
        operation_timeout=float(
            getattr(args, "operation_timeout", 180.0) or 180.0,
        ),
        retries=max(0, int(getattr(args, "retries", 2) or 0)),
    )
    for snapshot in snapshots:
        if snapshot.error:
            reporter.warning(
                f"{snapshot.target.runtime_id}: inventory failed: "
                f"{snapshot.error}",
            )
    return snapshots


def _load_bundles(
    sources: Sequence[str],
    *,
    timeout: float,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> dict[str, PluginBundle]:
    if not sources:
        raise UsageError("at least one plugin source is required")
    bundles: dict[str, PluginBundle] = {}
    for source in sources:
        for bundle in load_bundle_source(
            source,
            timeout=timeout,
            max_bytes=max_bytes,
        ):
            previous = bundles.get(bundle.plugin_id)
            if previous is not None:
                raise SourceError(
                    f"plugin {bundle.plugin_id!r} provided twice "
                    f"({previous.origin} and {bundle.origin}); pass one "
                    "source per plugin",
                )
            bundles[bundle.plugin_id] = bundle
    if not bundles:
        raise SourceError("no plugin bundles found in the given sources")
    return bundles


def _plan_per_snapshot(
    snapshots: list[RuntimeSnapshot],
    planner,
) -> list[Action]:
    actions: list[Action] = []
    for snapshot in snapshots:
        if snapshot.error:
            continue
        actions.extend(planner(snapshot))
    return actions


def _describe(mutating: list[Action]) -> str:
    counts = Counter(_MUTATING_LABEL[action.kind] for action in mutating)
    return ", ".join(f"{count} {label}" for label, count in sorted(counts.items()))


def _confirm(
    args: argparse.Namespace,
    reporter: Reporter,
    mutating: list[Action],
) -> bool:
    """Return whether the run should proceed."""
    if not mutating:
        return True
    if bool(getattr(args, "dry_run", False)):
        return True
    count = len(mutating)
    runtimes = len({action.target.key for action in mutating})
    description = _describe(mutating)
    if bool(getattr(args, "yes", False)):
        return True
    if not sys.stdin.isatty():
        raise UsageError(
            f"refusing to apply {count} change(s) ({description}) on "
            f"{runtimes} runtime(s) without --yes",
        )
    print(
        f"about to {description} ({count} action(s) on {runtimes} "
        f"runtime(s)); proceed? [y/N] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    answer = sys.stdin.readline().strip().lower()
    return answer in {"y", "yes"}


def _payload(
    command: str,
    args: argparse.Namespace,
    targets: list[RuntimeTarget],
    snapshots: list[RuntimeSnapshot],
    bundles: dict[str, PluginBundle],
    results: list[ActionResult],
    elapsed: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    counts = Counter(result.state.value for result in results)
    snapshot_by_key = {snapshot.target.key: snapshot for snapshot in snapshots}
    payload: dict[str, Any] = {
        "command": command,
        "dry_run": bool(getattr(args, "dry_run", False)),
        "elapsed_seconds": round(elapsed, 3),
        "runtimes": [
            {
                "runtime_id": target.runtime_id,
                "tenant_id": target.tenant_id,
                "owner_id": target.owner_id,
                "state": target.state,
                "base_url": target.base_url,
                "container_name": target.container_name,
                "qwenpaw_version": (
                    snapshot_by_key[target.key].app_version
                    if target.key in snapshot_by_key
                    else None
                ),
                "inventory_error": (
                    snapshot_by_key[target.key].error
                    if target.key in snapshot_by_key
                    else ""
                ),
            }
            for target in targets
        ],
        "bundles": [
            {
                "id": bundle.plugin_id,
                "version": bundle.version,
                "origin": bundle.origin,
                "sha256": bundle.sha256,
                "size": bundle.size,
            }
            for bundle in sorted(
                bundles.values(),
                key=lambda item: item.plugin_id,
            )
        ],
        "results": [
            {
                "runtime_id": result.action.target.runtime_id,
                "tenant_id": result.action.target.tenant_id,
                "plugin": result.action.plugin_id,
                "action": result.action.kind.value,
                "state": result.state.value,
                "current_version": result.action.current_version,
                "target_version": result.action.target_version,
                "message": result.message,
                "duration_ms": result.duration_ms,
                "http_status": result.http_status,
                "rolled_back": result.state == ActionState.ROLLED_BACK,
                "rollback": result.rollback,
                "backup_path": result.backup_path,
            }
            for result in results
        ],
        "summary": {
            "actions": len(results),
            "runtimes": len(targets),
            "ok": counts.get(ActionState.OK.value, 0),
            "skipped": counts.get(ActionState.SKIPPED.value, 0),
            "planned": counts.get(ActionState.PLANNED.value, 0),
            "failed": counts.get(ActionState.FAILED.value, 0),
            "rolled_back": counts.get(ActionState.ROLLED_BACK.value, 0),
        },
    }
    if extra:
        payload.update(extra)
    return payload


def _finish(
    command: str,
    args: argparse.Namespace,
    reporter: Reporter,
    targets: list[RuntimeTarget],
    snapshots: list[RuntimeSnapshot],
    bundles: dict[str, PluginBundle],
    results: list[ActionResult],
    elapsed: float,
    extra: dict[str, Any] | None = None,
) -> int:
    if bool(getattr(args, "json", False)):
        reporter.emit_json(
            _payload(
                command,
                args,
                targets,
                snapshots,
                bundles,
                results,
                elapsed,
                extra,
            ),
        )
    else:
        reporter.print_results(results)
    failed = any(
        result.state in {ActionState.FAILED, ActionState.ROLLED_BACK}
        for result in results
    )
    return 1 if failed else 0


def _execute(
    command: str,
    args: argparse.Namespace,
    reporter: Reporter,
    targets: list[RuntimeTarget],
    snapshots: list[RuntimeSnapshot],
    actions: list[Action],
    bundles: dict[str, PluginBundle],
    *,
    extra: dict[str, Any] | None = None,
) -> int:
    actions = runtime_issue_actions(snapshots) + actions
    mutating = [action for action in actions if action.mutating]
    for action in actions:
        logs.debug(
            "plan %s: %s %s %s%s",
            action.target.runtime_id,
            "change" if action.mutating else "no-op",
            action.kind.value,
            action.plugin_id,
            f" ({action.reason})" if action.reason else "",
        )
    reporter.info(
        f"plan: {len(mutating)} change(s), "
        f"{len(actions) - len(mutating)} no-op(s), "
        f"{len(targets)} runtime(s)",
    )
    if not _confirm(args, reporter, mutating):
        reporter.info("aborted")
        return 130
    started = time.monotonic()
    options = _exec_options(args)
    # Stream per-action progress only when a human is watching (or -v);
    # piped output gets the final table and the summary, nothing else.
    stream_progress = (
        not bool(getattr(args, "json", False))
        and not bool(getattr(args, "quiet", False))
        and (bool(getattr(args, "verbose", False)) or sys.stderr.isatty())
    )
    executor = Executor(
        bundles,
        options,
        on_result=reporter.result_line if stream_progress else None,
    )
    results = executor.run(snapshots, actions)
    elapsed = time.monotonic() - started
    reporter.info(f"finished in {elapsed:.1f}s")
    return _finish(
        command,
        args,
        reporter,
        targets,
        snapshots,
        bundles,
        results,
        elapsed,
        extra,
    )


# ── commands ─────────────────────────────────────────────────────────────


def cmd_runtimes(args: argparse.Namespace) -> int:
    """List discovered runtimes, optionally probing their API."""
    reporter = _reporter(args)
    targets = _discover(args, reporter)
    save_path = getattr(args, "save_inventory", None)
    if save_path:
        _save_inventory(Path(str(save_path)), targets, reporter)
    snapshots: list[RuntimeSnapshot] = []
    if bool(getattr(args, "probe", False)):
        snapshots = _snapshots(args, targets, reporter)
    if bool(getattr(args, "json", False)):
        snapshot_by_key = {snapshot.target.key: snapshot for snapshot in snapshots}
        reporter.emit_json(
            {
                "count": len(targets),
                "runtimes": [
                    {
                        "runtime_id": target.runtime_id,
                        "tenant_id": target.tenant_id,
                        "owner_id": target.owner_id,
                        "state": target.state,
                        "base_url": target.base_url,
                        "container_name": target.container_name,
                        "image": target.image,
                        "instance": target.instance,
                        "plugins_dir_host": target.plugins_dir_host,
                        "qwenpaw_version": (
                            snapshot_by_key[target.key].app_version
                            if target.key in snapshot_by_key
                            else None
                        ),
                        "plugins": (
                            len(snapshot_by_key[target.key].plugins)
                            if target.key in snapshot_by_key
                            else None
                        ),
                        "error": (
                            snapshot_by_key[target.key].error
                            if target.key in snapshot_by_key
                            else ""
                        ),
                    }
                    for target in targets
                ],
            },
        )
        return 0
    rows = []
    for target in targets:
        version = ""
        plugins = ""
        if snapshots:
            snapshot = next(
                (item for item in snapshots if item.target.key == target.key),
                None,
            )
            if snapshot is not None:
                version = snapshot.app_version or "-"
                plugins = str(len(snapshot.plugins)) if not snapshot.error else "error"
        rows.append(
            [
                target.runtime_id,
                target.tenant_id or "-",
                target.state,
                target.base_url,
                version or "-",
                plugins or "-",
                target.container_name or "-",
            ],
        )
    reporter.table(
        ["runtime", "tenant", "state", "endpoint", "qwenpaw", "plugins", "container"],
        rows,
    )
    return 0


def _save_inventory(
    path: Path,
    targets: list[RuntimeTarget],
    reporter: Reporter,
) -> None:
    """Write a portable inventory file (contains boundary tokens)."""
    payload = {
        "runtimes": [
            {
                "runtime_id": target.runtime_id,
                "tenant_id": target.tenant_id,
                "owner_id": target.owner_id,
                "url": target.base_url,
                "token": target.token,
                "state": target.state,
                "workspace": target.workspace_container_path,
                "plugins_dir": target.plugins_dir_host,
                "container_name": target.container_name,
                "instance": target.instance,
                "image": target.image,
            }
            for target in targets
        ],
    }
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    reporter.warning(
        f"wrote {len(targets)} runtime(s) including boundary tokens to "
        f"{path}; treat that file as a secret and delete it when done",
    )


def cmd_plugins(args: argparse.Namespace) -> int:
    """Show the plugin inventory of the selected runtimes."""
    reporter = _reporter(args)
    targets = _discover(args, reporter)
    snapshots = _snapshots(args, targets, reporter)
    patterns = tuple(_split_values(getattr(args, "plugin", None)))
    if patterns:
        for snapshot in snapshots:
            snapshot.plugins = {
                plugin_id: plugin
                for plugin_id, plugin in snapshot.plugins.items()
                if matches_patterns(plugin_id, patterns)
            }
    if bool(getattr(args, "json", False)):
        reporter.emit_json(
            {
                "runtimes": [
                    {
                        "runtime_id": snapshot.target.runtime_id,
                        "tenant_id": snapshot.target.tenant_id,
                        "state": snapshot.target.state,
                        "qwenpaw_version": snapshot.app_version,
                        "error": snapshot.error,
                        "plugins": [
                            {
                                "id": plugin.plugin_id,
                                "version": plugin.version,
                                "enabled": plugin.enabled,
                                "loaded": plugin.loaded,
                                "status": plugin.status,
                            }
                            for plugin in sorted(
                                snapshot.plugins.values(),
                                key=lambda item: item.plugin_id,
                            )
                        ],
                    }
                    for snapshot in snapshots
                ],
            },
        )
    else:
        reporter.print_plugin_matrix(snapshots)
    # A stopped runtime the operator explicitly included is not a failure.
    problems = [
        snapshot
        for snapshot in snapshots
        if snapshot.error and snapshot.target.running
    ]
    return 1 if problems else 0


def cmd_install(args: argparse.Namespace) -> int:
    """Install or upgrade bundles across the selected runtimes."""
    reporter = _reporter(args)
    timeout = float(getattr(args, "timeout", 30.0) or 30.0)
    bundles = _load_bundles(
        list(getattr(args, "source", None) or ()),
        timeout=timeout,
    )
    for bundle in bundles.values():
        reporter.debug(
            f"bundle {bundle.plugin_id} {bundle.version} "
            f"({bundle.size} bytes) from {bundle.origin}",
        )
    targets = _discover(args, reporter)
    snapshots = _snapshots(args, targets, reporter)
    options = _sync_options(args)
    actions = plan_sync(snapshots, bundles, options)
    if not actions and not any(snapshot.error for snapshot in snapshots):
        reporter.info(
            "nothing matched: no bundle applies to the selected runtimes "
            "(check --plugin patterns)",
        )
    return _execute(
        "install",
        args,
        reporter,
        targets,
        snapshots,
        actions,
        bundles,
    )


def _expand_plugin_ids(
    snapshots: list[RuntimeSnapshot],
    requested: Sequence[str],
) -> list[str]:
    ids: list[str] = []
    for value in requested:
        if any(char in value for char in "*?["):
            for snapshot in snapshots:
                for plugin_id in snapshot.plugins:
                    if matches_patterns(plugin_id, (value,)) and plugin_id not in ids:
                        ids.append(plugin_id)
        elif value not in ids:
            ids.append(value)
    return ids


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Uninstall (or unload) plugins across the selected runtimes."""
    reporter = _reporter(args)
    requested = list(getattr(args, "plugins", None) or ())
    if not requested:
        raise UsageError("at least one plugin id or glob is required")
    targets = _discover(args, reporter)
    snapshots = _snapshots(args, targets, reporter)
    plugin_ids = _expand_plugin_ids(snapshots, requested)
    if not plugin_ids:
        reporter.info("no installed plugin matched the requested ids")
    protected = frozenset(_split_values(getattr(args, "protect", None)))
    keep_files = bool(getattr(args, "keep_files", False))
    actions = _plan_per_snapshot(
        snapshots,
        lambda snapshot: plan_uninstall(
            snapshot,
            plugin_ids,
            keep_files=keep_files,
            protected=protected,
        ),
    )
    return _execute(
        "uninstall",
        args,
        reporter,
        targets,
        snapshots,
        actions,
        bundles={},
    )


def cmd_toggle(args: argparse.Namespace, *, enabled: bool) -> int:
    """Enable or disable plugins across the selected runtimes."""
    reporter = _reporter(args)
    requested = list(getattr(args, "plugins", None) or ())
    if not requested:
        raise UsageError("at least one plugin id or glob is required")
    targets = _discover(args, reporter)
    snapshots = _snapshots(args, targets, reporter)
    plugin_ids = _expand_plugin_ids(snapshots, requested)
    protected = frozenset(_split_values(getattr(args, "protect", None)))
    actions = _plan_per_snapshot(
        snapshots,
        lambda snapshot: plan_toggle(
            snapshot,
            plugin_ids,
            enabled=enabled,
            protected=protected,
        ),
    )
    return _execute(
        "enable" if enabled else "disable",
        args,
        reporter,
        targets,
        snapshots,
        actions,
        bundles={},
    )


def cmd_apply(args: argparse.Namespace) -> int:
    """Converge every selected runtime to a declarative state file."""
    reporter = _reporter(args)
    state_path = Path(str(getattr(args, "state_file"))).expanduser()
    desired = load_state(state_path)
    if not desired.plugins and not desired.removed:
        raise UsageError(f"{state_path}: state file declares nothing to do")

    timeout = float(getattr(args, "timeout", 30.0) or 30.0)
    bundle_by_plugin: dict[str, PluginBundle] = {}
    bundle_by_source: dict[str, dict[str, PluginBundle]] = {}
    for entry in desired.plugins:
        if not entry.source:
            if not entry.version:
                raise UsageError(
                    f"{state_path}: plugin {entry.plugin_id!r} has neither "
                    "'source' nor 'version'; nothing to converge",
                )
            continue
        if entry.source not in bundle_by_source:
            bundle_by_source[entry.source] = _load_bundles(
                [entry.source],
                timeout=timeout,
            )
        loaded = bundle_by_source[entry.source]
        bundle = loaded.get(entry.plugin_id)
        if bundle is None:
            raise SourceError(
                f"{entry.source}: does not contain plugin {entry.plugin_id!r} "
                f"(found: {', '.join(sorted(loaded)) or 'nothing'})",
            )
        if entry.version and not _same_version(entry.version, bundle.version):
            raise UsageError(
                f"{state_path}: plugin {entry.plugin_id!r} declares version "
                f"{entry.version} but {entry.source} contains "
                f"{bundle.version}",
            )
        bundle_by_plugin[entry.plugin_id] = bundle

    targets = _discover(args, reporter)
    snapshots = _snapshots(args, targets, reporter)
    base_options = _sync_options(args)
    patterns = base_options.plugin_patterns
    base_options.plugin_patterns = ()
    protected = base_options.protected
    entries = [
        entry
        for entry in desired.plugins
        if matches_patterns(entry.plugin_id, patterns)
    ]
    removed = [
        plugin_id
        for plugin_id in desired.removed
        if matches_patterns(plugin_id, patterns)
    ]
    if patterns:
        reporter.info(
            f"state file filtered by --plugin: {len(entries)} of "
            f"{len(desired.plugins)} plugin(s), {len(removed)} of "
            f"{len(desired.removed)} removal(s)",
        )

    actions: list[Action] = []
    for snapshot in snapshots:
        if snapshot.error:
            continue
        for entry in entries:
            if entry.runtimes and not _matches_runtime(
                entry.runtimes,
                snapshot.target,
            ):
                continue
            if entry.plugin_id in bundle_by_plugin:
                options = replace(
                    base_options,
                    enable=entry.enabled,
                    plugin_patterns=(),
                )
                actions.extend(
                    plan_bundle(snapshot, bundle_by_plugin[entry.plugin_id], options),
                )
            else:
                actions.append(
                    plan_check(
                        snapshot,
                        entry.plugin_id,
                        entry.version,
                        optional=entry.optional,
                    ),
                )
        if removed:
            actions.extend(
                plan_uninstall(
                    snapshot,
                    removed,
                    keep_files=False,
                    protected=protected,
                ),
            )

    return _execute(
        "apply",
        args,
        reporter,
        targets,
        snapshots,
        actions,
        bundle_by_plugin,
        extra={"state_file": str(state_path)},
    )


def _matches_runtime(patterns: Sequence[str], target: RuntimeTarget) -> bool:
    return any(
        matches_patterns(target.runtime_id, (pattern,))
        or matches_patterns(target.container_name, (pattern,))
        for pattern in patterns
    )


def cmd_freeze(args: argparse.Namespace) -> int:
    """Write a state file that describes one runtime's current plugins."""
    reporter = _reporter(args)
    from_runtime = _split_values(getattr(args, "from_runtime", None))
    if from_runtime:
        # Re-run discovery with the extra filter applied.
        setattr(
            args,
            "runtime",
            list(getattr(args, "runtime", None) or ()) + from_runtime,
        )
    targets = _discover(args, reporter)
    snapshots = _snapshots(args, targets, reporter)
    healthy = [snapshot for snapshot in snapshots if not snapshot.error]
    if len(healthy) != 1:
        ids = ", ".join(snapshot.target.runtime_id for snapshot in healthy) or "none"
        raise UsageError(
            "freeze needs exactly one reachable runtime "
            f"(matched {len(healthy)}: {ids}); narrow with --from-runtime",
        )
    snapshot = healthy[0]
    payload = freeze_payload(snapshot)
    output = str(getattr(args, "output", None) or "plugins.lock.json")
    if output == "-":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    write_state(Path(output), payload)
    reporter.info(
        f"wrote {len(payload['plugins'])} plugin entr(ies) from "
        f"{snapshot.target.runtime_id} to {output}",
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the web console."""
    from .webapi import WebConfig, resolve_bind_host, serve

    port_value = getattr(args, "port", 8787)
    no_token = bool(getattr(args, "no_token", False))
    config = WebConfig(
        discovery=_discovery_options(args),
        exec_options=_exec_options(args),
        host=resolve_bind_host(
            getattr(args, "host", None),
            token_required=not no_token,
        ),
        port=8787 if port_value is None else int(port_value),
        token_required=not no_token,
        host_sdk=str(getattr(args, "host_sdk", "") or "1.0.0"),
        open_browser=bool(getattr(args, "open", False)),
    )
    return serve(config)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check discovery, connectivity, tokens, and rollback capability."""
    reporter = _reporter(args)
    discovery = discover_runtimes(_discovery_options(args))
    for warning in discovery.warnings:
        reporter.warning(warning)
    targets, warnings = select_targets(
        discovery.targets,
        patterns=_split_values(getattr(args, "runtime", None)),
        tenants=_split_values(getattr(args, "tenant", None)),
        owners=_split_values(getattr(args, "owner", None)),
        include_stopped=True,
    )
    for warning in warnings:
        reporter.warning(warning)
    if not targets:
        raise UsageError(
            f"no runtimes found via {discovery.source}; check Docker access or "
            "--inventory",
        )

    rows: list[list[str]] = []
    problems = 0
    json_rows: list[dict[str, Any]] = []
    for target in targets:
        row, problem, record = _doctor_row(
            target,
            timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            retries=max(0, int(getattr(args, "retries", 2) or 0)),
        )
        rows.append(row)
        json_rows.append(record)
        if problem:
            problems += 1
    if bool(getattr(args, "json", False)):
        reporter.emit_json(
            {
                "source": discovery.source,
                "count": len(targets),
                "problems": problems,
                "runtimes": json_rows,
            },
        )
    else:
        reporter.table(
            [
                "runtime",
                "state",
                "endpoint",
                "token",
                "qwenpaw",
                "plugins-api",
                "upload-cap",
                "host-dir",
            ],
            rows,
        )
        if problems:
            reporter.error(f"{problems} runtime(s) reported a problem")
    return 1 if problems else 0


def _doctor_row(
    target: RuntimeTarget,
    *,
    timeout: float,
    retries: int,
) -> tuple[list[str], bool, dict[str, Any]]:
    record: dict[str, Any] = {
        "runtime_id": target.runtime_id,
        "tenant_id": target.tenant_id,
        "state": target.state,
        "endpoint": target.base_url,
        "token": "present",
        "qwenpaw_version": None,
        "plugins_api": "not checked",
        "upload_limit_mb": None,
        "host_plugins_dir": target.plugins_dir_host or "",
        "host_dir_writable": None,
        "error": "",
    }
    if not target.running:
        record["error"] = f"runtime is {target.state}"
        return (
            [
                target.runtime_id,
                target.state,
                target.base_url,
                "present",
                "-",
                "skipped",
                "-",
                "-",
            ],
            False,
            record,
        )

    host_dir = "-"
    if target.plugins_dir_host:
        path = Path(target.plugins_dir_host)
        writable = path.is_dir() and os.access(path, os.W_OK)
        record["host_dir_writable"] = writable
        host_dir = "writable" if writable else ("read-only" if path.is_dir() else "missing")

    snapshot = fetch_snapshot(
        target,
        timeout=timeout,
        retries=retries,
    )
    record["qwenpaw_version"] = snapshot.app_version
    record["upload_limit_mb"] = snapshot.upload_limit_mb
    if snapshot.error:
        record["error"] = snapshot.error
        record["plugins_api"] = "error"
        token_state = "rejected" if "401" in snapshot.error else "present"
        return (
            [
                target.runtime_id,
                target.state,
                target.base_url,
                token_state,
                snapshot.app_version or "-",
                "error",
                str(snapshot.upload_limit_mb or "-"),
                host_dir,
            ],
            True,
            record,
        )
    try:
        healthy, reason = RuntimeClient(
            target,
            timeout=timeout,
            retries=retries,
        ).healthz()
    except QpmError as exc:
        healthy, reason = False, str(exc)
    record["plugins_api"] = f"{len(snapshot.plugins)} plugin(s)"
    if not healthy:
        record["error"] = reason or "runtime is still starting"
        return (
            [
                target.runtime_id,
                target.state,
                target.base_url,
                "present",
                snapshot.app_version or "-",
                "starting",
                str(snapshot.upload_limit_mb or "-"),
                host_dir,
            ],
            True,
            record,
        )
    return (
        [
            target.runtime_id,
            target.state,
            target.base_url,
            "present",
            snapshot.app_version or "-",
            f"{len(snapshot.plugins)} plugin(s)",
            str(snapshot.upload_limit_mb or "unlimited"),
            host_dir,
        ],
        False,
        record,
    )


def _same_version(left: str, right: str) -> bool:
    left_version = parse_version(left)
    right_version = parse_version(right)
    if left_version is None or right_version is None:
        return left.strip() == right.strip()
    return left_version == right_version


