# -*- coding: utf-8 -*-
"""Execute a plan: concurrent across runtimes, sequential within a runtime.

Two deliberate choices:

* runtimes run in parallel (they are independent containers);
* actions inside one runtime run in order, because the runtime serialises
  plugin lifecycle work behind a per-plugin lock and because the plan uses
  order to express intent (install, then enable).

Every mutating action is verified afterwards by reading the plugin back from
the runtime.  Destructive actions take a host-side snapshot first and are
rolled back when verification fails.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import logs
from .backup import Backup, create_backup, default_backup_root, restore_backup
from .errors import (
    PluginAlreadyInstalled,
    QpmError,
    RuntimeApiError,
)
from .models import (
    Action,
    ActionKind,
    ActionResult,
    ActionState,
    PluginBundle,
    REPLACING_KINDS,
    RuntimeSnapshot,
)
from .runtime_client import RuntimeClient
from .versions import compare, parse_version

__all__ = ["ExecOptions", "Executor"]

_LOG = logs.get_logger("executor")


@dataclass
class ExecOptions:
    """Execution knobs shared by every command."""

    dry_run: bool = False
    concurrency: int = 4
    timeout: float = 30.0
    operation_timeout: float = 180.0
    retries: int = 2
    interrupt_tasks: bool = False
    backup: bool = True
    backup_root: Path = field(default_factory=default_backup_root)
    rollback: bool = True
    verify: bool = True
    fail_fast: bool = False


def _same_version(left: str, right: str) -> bool:
    left_version = parse_version(left)
    right_version = parse_version(right)
    if left_version is None or right_version is None:
        return left.strip() == right.strip()
    return compare(left_version, right_version) == 0


class Executor:
    """Run planned actions and report one result per action."""

    def __init__(
        self,
        bundles: dict[str, PluginBundle],
        options: ExecOptions,
        *,
        on_result: Callable[[ActionResult], None] | None = None,
    ) -> None:
        self.bundles = bundles
        self.options = options
        self.on_result = on_result
        self._stamp = time.strftime("%Y%m%d-%H%M%S")

    # ── public entry point ───────────────────────────────────────────────

    def run(
        self,
        snapshots: list[RuntimeSnapshot],
        actions: list[Action],
    ) -> list[ActionResult]:
        """Execute ``actions`` across ``snapshots`` and return all results."""
        if not actions:
            return []
        snapshot_by_key = {snapshot.target.key: snapshot for snapshot in snapshots}
        order: list[str] = []
        grouped: dict[str, list[Action]] = {}
        for action in actions:
            key = action.target.key
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(action)

        results: list[ActionResult] = []
        workers = max(1, min(self.options.concurrency, len(order)))
        _LOG.debug(
            "executing %d action(s) across %d runtime(s) with %d worker(s)",
            len(actions),
            len(order),
            workers,
        )
        if workers == 1:
            for key in order:
                results.extend(
                    self._run_runtime(snapshot_by_key.get(key), grouped[key]),
                )
            return results

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    self._run_runtime,
                    snapshot_by_key.get(key),
                    grouped[key],
                )
                for key in order
            ]
            for future in futures:
                results.extend(future.result())
        return results

    # ── per-runtime ──────────────────────────────────────────────────────

    def _run_runtime(
        self,
        snapshot: RuntimeSnapshot | None,
        actions: list[Action],
    ) -> list[ActionResult]:
        target = actions[0].target
        client = RuntimeClient(
            target,
            timeout=self.options.timeout,
            operation_timeout=self.options.operation_timeout,
            retries=self.options.retries,
        )
        results: list[ActionResult] = []
        failed_plugins: set[str] = set()
        for action in actions:
            if (
                action.depends_on is not None
                and action.depends_on in failed_plugins
            ):
                result = ActionResult(
                    action=action,
                    state=ActionState.SKIPPED,
                    message=(
                        f"skipped because {action.depends_on} did not converge"
                    ),
                )
            else:
                result = self._execute(client, snapshot, action)
            if result.state == ActionState.FAILED:
                failed_plugins.add(action.plugin_id)
            results.append(result)
            if self.on_result is not None:
                self.on_result(result)
            if self.options.fail_fast and result.state == ActionState.FAILED:
                for remaining in actions[len(results) :]:
                    results.append(
                        ActionResult(
                            action=remaining,
                            state=ActionState.SKIPPED,
                            message="not attempted (--fail-fast)",
                        ),
                    )
                break
        return results

    # ── single action ────────────────────────────────────────────────────

    def _execute(
        self,
        client: RuntimeClient,
        snapshot: RuntimeSnapshot | None,
        action: Action,
    ) -> ActionResult:
        started = time.monotonic()
        result = ActionResult(action=action, state=ActionState.PLANNED)
        backup: Backup | None = None
        _LOG.debug(
            "%s: start %s %s (%s -> %s)%s",
            action.target.runtime_id,
            action.kind.value,
            action.plugin_id,
            action.current_version or "-",
            action.target_version or "-",
            " [dry-run]" if self.options.dry_run else "",
        )
        try:
            if action.kind == ActionKind.RUNTIME:
                result.state = ActionState.FAILED
                result.message = action.reason or "runtime is unavailable"
                return result

            # A source-less drift check is read-only, so it must be evaluated
            # even in --dry-run and before the "nothing to do" shortcut.
            if action.kind == ActionKind.CHECK:
                result.state = (
                    ActionState.SKIPPED if action.optional else ActionState.FAILED
                )
                result.message = action.reason
                return result

            if not action.mutating:
                result.state = ActionState.SKIPPED
                result.message = action.reason or "nothing to do"
                return result

            if self.options.dry_run:
                result.state = ActionState.PLANNED
                result.message = "would " + action.kind.value
                return result

            if action.kind in REPLACING_KINDS and self.options.backup:
                outcome = create_backup(
                    action.target,
                    action.plugin_id,
                    action.current_version or "",
                    backup_root=self.options.backup_root,
                    stamp=self._stamp,
                )
                backup = outcome.backup
                if backup is not None:
                    result.backup_path = str(backup.backup_dir)
                    _LOG.debug(
                        "%s: snapshot %s to %s",
                        action.target.runtime_id,
                        action.plugin_id,
                        backup.backup_dir,
                    )
                elif outcome.note:
                    _LOG.debug(
                        "%s: no snapshot for %s (%s)",
                        action.target.runtime_id,
                        action.plugin_id,
                        outcome.note,
                    )
                    result.details["backup_note"] = outcome.note

            if action.kind in {
                ActionKind.INSTALL,
                ActionKind.UPGRADE,
                ActionKind.DOWNGRADE,
                ActionKind.REINSTALL,
            }:
                message = self._sync_plugin(client, snapshot, action)
                result.state = ActionState.OK
                result.message = self._with_warning(message, action)
            elif action.kind in {ActionKind.UNINSTALL, ActionKind.UNLOAD}:
                message = self._remove_plugin(client, action)
                result.state = ActionState.OK
                result.message = message
            elif action.kind in {ActionKind.ENABLE, ActionKind.DISABLE}:
                message = self._toggle_plugin(client, action)
                result.state = ActionState.OK
                result.message = message
            else:  # pragma: no cover - every kind is handled above
                raise RuntimeApiError(
                    f"unsupported action kind: {action.kind.value}",
                )
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            result.state = ActionState.FAILED
            result.message = self._failure_message(exc)
            _LOG.debug(
                "%s: %s %s failed: %s",
                action.target.runtime_id,
                action.kind.value,
                action.plugin_id,
                result.message,
            )
            status = getattr(exc, "status", None)
            result.http_status = status if isinstance(status, int) else None
            if (
                backup is not None
                and self.options.rollback
                and action.kind in REPLACING_KINDS
            ):
                rollback_note = self._rollback(client, action, backup)
                result.rollback = rollback_note
                if rollback_note.startswith("rolled back"):
                    result.state = ActionState.ROLLED_BACK
                else:
                    result.message = f"{result.message}; {rollback_note}"
            elif (
                action.kind in REPLACING_KINDS
                and self.options.backup
                and not result.details.get("no_rollback_note_shown")
            ):
                note = result.details.get("backup_note")
                if note:
                    result.message = (
                        f"{result.message}; no rollback available ({note})"
                    )
                    result.details["no_rollback_note_shown"] = True
        finally:
            result.duration_ms = int((time.monotonic() - started) * 1000)
            _LOG.debug(
                "%s: %s %s -> %s in %dms%s",
                action.target.runtime_id,
                action.kind.value,
                action.plugin_id,
                result.state.value,
                result.duration_ms,
                f" ({result.message})" if result.message else "",
            )
        return result

    # ── operations ───────────────────────────────────────────────────────

    def _sync_plugin(
        self,
        client: RuntimeClient,
        snapshot: RuntimeSnapshot | None,
        action: Action,
    ) -> str:
        bundle = self.bundles.get(action.plugin_id)
        if bundle is None:
            raise RuntimeApiError(
                f"no bundle is loaded for plugin {action.plugin_id!r}",
            )
        limit = snapshot.upload_limit_mb if snapshot is not None else None
        if limit and bundle.size > limit * 1024 * 1024:
            raise RuntimeApiError(
                f"bundle is {bundle.size / (1024 * 1024):.1f} MiB but the "
                f"runtime upload limit is {limit} MiB",
            )
        try:
            client.upload_plugin(
                bundle,
                force=action.force,
                interrupt_tasks=self.options.interrupt_tasks,
            )
        except PluginAlreadyInstalled:
            current = client.get_plugin(action.plugin_id)
            if current is not None and _same_version(
                current.version,
                bundle.version,
            ):
                return f"already installed at {bundle.version}"
            # Another manager won the race with a different payload: force.
            client.upload_plugin(
                bundle,
                force=True,
                interrupt_tasks=self.options.interrupt_tasks,
            )
        if self.options.verify:
            current = self._verify_installed(client, action.plugin_id, bundle)
            suffix = "" if current.enabled else " (disabled)"
        else:
            suffix = ""
        if action.kind == ActionKind.INSTALL:
            return f"installed {bundle.version}{suffix}"
        if action.kind == ActionKind.REINSTALL:
            return f"reinstalled {bundle.version}{suffix}"
        return (
            f"{action.kind.value} {action.current_version} -> "
            f"{bundle.version}{suffix}"
        )

    def _remove_plugin(self, client: RuntimeClient, action: Action) -> str:
        keep_files = action.kind == ActionKind.UNLOAD
        client.uninstall_plugin(
            action.plugin_id,
            interrupt_tasks=self.options.interrupt_tasks,
            keep_files=keep_files,
        )
        if self.options.verify:
            current = client.get_plugin(action.plugin_id)
            if keep_files:
                if current is not None and current.loaded:
                    raise RuntimeApiError(
                        "runtime still reports the plugin as loaded after "
                        "unload",
                    )
            elif current is not None:
                raise RuntimeApiError(
                    "runtime still reports the plugin after uninstall "
                    f"(version {current.version})",
                )
        if keep_files:
            return f"unloaded {action.current_version} (files kept)"
        return f"removed {action.current_version}"

    def _toggle_plugin(self, client: RuntimeClient, action: Action) -> str:
        desired = action.kind == ActionKind.ENABLE
        client.set_enabled(
            action.plugin_id,
            desired,
            interrupt_tasks=self.options.interrupt_tasks,
        )
        if self.options.verify:
            current = client.get_plugin(action.plugin_id)
            if current is None:
                raise RuntimeApiError(
                    "plugin disappeared while changing its enabled state",
                )
            if current.enabled != desired:
                raise RuntimeApiError(
                    f"runtime reports enabled={current.enabled}, "
                    f"expected {desired}",
                )
        return "enabled" if desired else "disabled"

    # ── helpers ──────────────────────────────────────────────────────────

    def _verify_installed(
        self,
        client: RuntimeClient,
        plugin_id: str,
        bundle: PluginBundle,
    ):
        current = client.get_plugin(plugin_id)
        if current is None:
            raise RuntimeApiError(
                f"runtime accepted the install but reports no plugin "
                f"{plugin_id!r}; check the runtime log",
            )
        if not _same_version(current.version, bundle.version):
            raise RuntimeApiError(
                f"expected version {bundle.version}, runtime reports "
                f"{current.version}",
            )
        if not current.healthy:
            raise RuntimeApiError(
                f"plugin is installed but reports status "
                f"{current.status!r} (loaded={current.loaded})",
            )
        return current

    def _rollback(
        self,
        client: RuntimeClient,
        action: Action,
        backup: Backup,
    ) -> str:
        try:
            _LOG.debug(
                "%s: restoring %s files from %s",
                action.target.runtime_id,
                action.plugin_id,
                backup.backup_dir,
            )
            restore_backup(backup)
        except OSError as exc:
            return f"rollback failed: cannot restore files ({exc})"
        try:
            _LOG.debug(
                "%s: reinstalling %s from %s",
                action.target.runtime_id,
                action.plugin_id,
                backup.container_path,
            )
            client.install_path(
                backup.container_path,
                force=True,
                interrupt_tasks=True,
            )
        except QpmError as exc:
            return (
                "rollback partial: files restored from "
                f"{backup.backup_dir} but reinstalling "
                f"{backup.version} failed ({exc}); restart the runtime or "
                "re-run install"
            )
        return (
            f"rolled back to {backup.version or 'previous files'} from "
            f"{backup.backup_dir}"
        )

    @staticmethod
    def _with_warning(message: str, action: Action) -> str:
        if action.warning:
            return f"{message} ({action.warning})"
        return message

    @staticmethod
    def _failure_message(exc: BaseException) -> str:
        if isinstance(exc, RuntimeApiError) and exc.is_busy:
            return f"{exc}; the runtime is busy — retry with --interrupt-tasks"
        if isinstance(exc, KeyError):
            return f"unknown plugin or bundle: {exc}"
        return str(exc) or type(exc).__name__
