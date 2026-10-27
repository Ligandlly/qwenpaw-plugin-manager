# -*- coding: utf-8 -*-
"""Local web console for the fleet plugin manager.

Why a server at all: the browser cannot run ``docker inspect`` or open loopback
connections to every runtime.  This module is a thin HTTP layer over the exact
same discovery / planning / execution modules the CLI uses, so the console can
never diverge from ``qpm install``.

Security posture (the tool can change every tenant runtime, so this matters):

* binds to ``127.0.0.1`` by default, and to every interface (``0.0.0.0``) when
  ``--no-token`` asks for an unauthenticated console;
* every ``/api`` call needs ``X-QPM-Token`` (random per start unless
  ``--no-token``), compared with :func:`hmac.compare_digest`;
* state-changing calls must be ``application/json`` (or multipart for uploads)
  and must not come from a foreign ``Origin`` — that closes the CSRF door that
  a localhost service otherwise leaves open;
* strict CSP, ``no-store``, ``nosniff``; static assets are a fixed whitelist.
"""

from __future__ import annotations

import dataclasses
import hmac
import io
import json
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .bundles import DEFAULT_MAX_BUNDLE_BYTES, load_bundle_source
from .errors import QpmError, SourceError, UsageError
from .executor import ExecOptions, Executor
from .inventory import fetch_inventory
from .models import (
    Action,
    ActionResult,
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
from .sources import DiscoveryOptions, discover_runtimes, select_targets
from .state import parse_state, freeze_payload
from .versions import parse_version

__all__ = [
    "WebConfig",
    "WebContext",
    "build_plan",
    "create_server",
    "resolve_bind_host",
    "serve",
]

LOOPBACK_HOST = "127.0.0.1"
#: Bound when the token is disabled: no credential means the operator wants
#: to reach the console from other hosts, not just from this machine.
PUBLIC_HOST = "0.0.0.0"

WEB_DIR = Path(__file__).with_name("web")
_MAX_JSON_BYTES = 1024 * 1024
_MAX_UPLOAD_BYTES = 80 * 1024 * 1024
_JOB_HISTORY = 30

_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


# ── configuration ────────────────────────────────────────────────────────


@dataclass
class WebConfig:
    """Everything the console needs; built by ``qpm serve``."""

    discovery: DiscoveryOptions = field(default_factory=DiscoveryOptions)
    exec_options: ExecOptions = field(default_factory=ExecOptions)
    host: str = LOOPBACK_HOST
    port: int = 8787
    token: str = ""
    token_required: bool = True
    host_sdk: str = "1.0.0"
    open_browser: bool = False
    workdir: Path | None = None


def resolve_bind_host(
    host: str | None = None,
    *,
    token_required: bool = True,
) -> str:
    """Pick the bind address for an explicit ``--host`` or a token policy.

    ``--no-token`` opens the console to the network: an unauthenticated
    console that only listens on loopback is useless to the operator who
    asked for one.  An explicit ``--host`` always wins.
    """
    explicit = str(host or "").strip()
    if explicit:
        return explicit
    return LOOPBACK_HOST if token_required else PUBLIC_HOST


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── job objects ──────────────────────────────────────────────────────────


@dataclass
class Job:
    """One plan+execute run, polled by the UI."""

    job_id: str
    operation: str
    params: dict[str, Any]
    status: str = "pending"
    phase: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    targets: list[RuntimeTarget] = field(default_factory=list)
    snapshots: list[RuntimeSnapshot] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    bundles: dict[str, PluginBundle] = field(default_factory=dict)
    results: list[ActionResult] = field(default_factory=list)

    def brief(self) -> dict[str, Any]:
        """Return the cheap representation used by the job list."""
        return {
            "job_id": self.job_id,
            "operation": self.operation,
            "status": self.status,
            "phase": self.phase,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "runtimes": len(self.targets),
            "actions": len(self.actions),
            "results": len(self.results),
            "error": self.error,
        }

    def detail(self) -> dict[str, Any]:
        """Return the full representation polled while a job runs."""
        return {
            **self.brief(),
            "started_at": self.started_at,
            "params": _public_params(self.params),
            "warnings": list(self.warnings),
            "runtimes": [_target_dict(target) for target in self.targets],
            "bundles": [
                {
                    "id": bundle.plugin_id,
                    "version": bundle.version,
                    "origin": bundle.origin,
                    "size": bundle.size,
                    "sha256": bundle.sha256,
                }
                for bundle in sorted(
                    self.bundles.values(),
                    key=lambda item: item.plugin_id,
                )
            ],
            "plan": [_action_dict(action) for action in self.actions],
            "results": [_result_dict(result) for result in self.results],
            "summary": _summary(self.results),
        }


@dataclass
class Planned:
    """A fully resolved plan, shared by preview and execution."""

    operation: str
    targets: list[RuntimeTarget]
    snapshots: list[RuntimeSnapshot]
    actions: list[Action]
    bundles: dict[str, PluginBundle]
    warnings: list[str]

    def payload(self) -> dict[str, Any]:
        """Serialise the preview for the browser."""
        return {
            "operation": self.operation,
            "warnings": list(self.warnings),
            "runtimes": [_target_dict(target) for target in self.targets],
            "bundles": [
                {
                    "id": bundle.plugin_id,
                    "version": bundle.version,
                    "origin": bundle.origin,
                    "size": bundle.size,
                    "sha256": bundle.sha256,
                }
                for bundle in sorted(
                    self.bundles.values(),
                    key=lambda item: item.plugin_id,
                )
            ],
            "actions": [_action_dict(action) for action in self.actions],
            "summary": _plan_summary(self.actions),
        }


def _summary(results: list[ActionResult]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.state.value] = counts.get(result.state.value, 0) + 1
    return {
        "actions": len(results),
        "ok": counts.get(ActionState.OK.value, 0),
        "skipped": counts.get(ActionState.SKIPPED.value, 0),
        "failed": counts.get(ActionState.FAILED.value, 0),
        "rolled_back": counts.get(ActionState.ROLLED_BACK.value, 0),
    }


def _plan_summary(actions: list[Action]) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    for action in actions:
        kinds[action.kind.value] = kinds.get(action.kind.value, 0) + 1
    return {
        "actions": len(actions),
        "mutating": sum(1 for action in actions if action.mutating),
        "kinds": kinds,
    }


def _target_dict(target: RuntimeTarget) -> dict[str, Any]:
    return {
        "runtime_id": target.runtime_id,
        "tenant_id": target.tenant_id,
        "owner_id": target.owner_id,
        "state": target.state,
        "base_url": target.base_url,
        "container_name": target.container_name,
        "instance": target.instance,
        "image": target.image,
        "plugins_dir_host": target.plugins_dir_host,
        "workspace": target.workspace_container_path,
    }


def _action_dict(action: Action) -> dict[str, Any]:
    return {
        "runtime_id": action.target.runtime_id,
        "tenant_id": action.target.tenant_id,
        "plugin": action.plugin_id,
        "action": action.kind.value,
        "current_version": action.current_version,
        "target_version": action.target_version,
        "reason": action.reason,
        "warning": action.warning,
        "mutating": action.mutating,
        "optional": action.optional,
        "origin": action.origin,
    }


def _result_dict(result: ActionResult) -> dict[str, Any]:
    return {
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
        "backup_path": result.backup_path,
        "rollback": result.rollback,
    }


def _public_params(params: dict[str, Any]) -> dict[str, Any]:
    """Never echo tokens; redact inventory-like secrets just in case."""
    public = dict(params)
    if "state" in public and isinstance(public["state"], dict):
        public["state"] = "<inline state document>"
    return public


# ── context ──────────────────────────────────────────────────────────────


class WebContext:
    """Server-wide state: config, uploads, jobs, and the job lock."""

    def __init__(self, config: WebConfig) -> None:
        self.config = config
        self.lock = threading.RLock()
        self.uploads: dict[str, Path] = {}
        self.jobs: dict[str, Job] = {}
        self.job_order: list[str] = []
        self.active_job_id: str | None = None
        self.workdir = Path(
            config.workdir
            or tempfile.mkdtemp(prefix="qpm-web-"),
        )
        self.workdir.mkdir(parents=True, exist_ok=True)

    # ── logging ─────────────────────────────────────────────────────────

    def log(self, message: str) -> None:
        """Write an operator-facing line to stderr."""
        print(f"[qpm] {message}", file=sys.stderr, flush=True)

    # ── uploads ─────────────────────────────────────────────────────────

    def save_upload(self, filename: str, data: bytes) -> dict[str, Any]:
        """Persist an uploaded bundle and return its reference."""
        upload_id = f"up_{uuid.uuid4().hex[:10]}"
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "bundle.zip"
        path = self.workdir / f"{upload_id}-{safe_name}"
        path.write_bytes(data)
        with self.lock:
            self.uploads[upload_id] = path
        return {"upload_id": upload_id, "name": safe_name, "size": len(data)}

    def resolve_sources(self, sources: list[str]) -> list[str]:
        """Map ``@upload`` references to their on-disk paths."""
        resolved: list[str] = []
        with self.lock:
            for source in sources:
                if source.startswith("@"):
                    path = self.uploads.get(source[1:])
                    if path is None:
                        raise UsageError(
                            f"unknown upload reference {source!r}; upload the "
                            "bundle again",
                        )
                    resolved.append(str(path))
                else:
                    resolved.append(source)
        return resolved

    def cleanup(self) -> None:
        """Remove temporary uploads; called when the server stops."""
        shutil.rmtree(self.workdir, ignore_errors=True)

    # ── jobs ────────────────────────────────────────────────────────────

    def active_job(self) -> Job | None:
        with self.lock:
            if self.active_job_id is None:
                return None
            return self.jobs.get(self.active_job_id)

    def job_list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                self.jobs[job_id].brief()
                for job_id in reversed(self.job_order)
                if job_id in self.jobs
            ]

    def get_job(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)


# ── planning ─────────────────────────────────────────────────────────────


def _as_str_list(value: object, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise UsageError(f"{name} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    return bool(value)


def _as_tristate(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _filters(payload: dict[str, Any]) -> dict[str, Any]:
    filters = payload.get("filters") or {}
    if not isinstance(filters, dict):
        raise UsageError("filters must be an object")
    return {
        "runtime": _as_str_list(filters.get("runtime"), "filters.runtime"),
        "tenant": _as_str_list(filters.get("tenant"), "filters.tenant"),
        "owner": _as_str_list(filters.get("owner"), "filters.owner"),
        "include_stopped": _as_bool(filters.get("include_stopped")),
    }


def _resolve_runtimes(
    ctx: WebContext,
    payload: dict[str, Any],
) -> tuple[list[RuntimeTarget], list[str], list[RuntimeSnapshot]]:
    filters = _filters(payload)
    discovered = discover_runtimes(ctx.config.discovery)
    warnings = list(discovered.warnings)
    targets, selection_warnings = select_targets(
        discovered.targets,
        patterns=filters["runtime"],
        tenants=filters["tenant"],
        owners=filters["owner"],
        include_stopped=filters["include_stopped"],
    )
    warnings.extend(selection_warnings)
    if not targets:
        raise UsageError(
            "no runtimes selected; pick at least one target in the console "
            "or relax the filters",
        )
    snapshots = fetch_inventory(
        targets,
        concurrency=ctx.config.exec_options.concurrency,
        timeout=ctx.config.exec_options.timeout,
        operation_timeout=ctx.config.exec_options.operation_timeout,
        retries=ctx.config.exec_options.retries,
    )
    for snapshot in snapshots:
        if snapshot.error:
            warnings.append(
                f"{snapshot.target.runtime_id}: {snapshot.error}",
            )
    return targets, warnings, snapshots


def _sync_options(ctx: WebContext, params: dict[str, Any]) -> SyncOptions:
    mode = str(params.get("mode") or "sync")
    if mode not in {"sync", "install", "upgrade"}:
        raise UsageError("mode must be sync, install, or upgrade")
    return SyncOptions(
        mode=mode,
        reinstall=_as_bool(params.get("reinstall")),
        allow_downgrade=_as_bool(params.get("allow_downgrade")),
        install_missing=_as_bool(params.get("install_missing"), True),
        plugin_patterns=tuple(
            _as_str_list(params.get("plugins"), "params.plugins"),
        ),
        protected=frozenset(
            _as_str_list(params.get("protect"), "params.protect"),
        ),
        host_sdk=ctx.config.host_sdk,
        enable=_as_tristate(params.get("enable")),
    )


def _load_bundles(ctx: WebContext, sources: list[str]) -> dict[str, PluginBundle]:
    if not sources:
        raise UsageError("at least one plugin source is required")
    resolved = ctx.resolve_sources(sources)
    bundles: dict[str, PluginBundle] = {}
    for path in resolved:
        for bundle in load_bundle_source(
            path,
            timeout=ctx.config.exec_options.timeout,
            max_bytes=DEFAULT_MAX_BUNDLE_BYTES,
        ):
            previous = bundles.get(bundle.plugin_id)
            if previous is not None:
                raise SourceError(
                    f"plugin {bundle.plugin_id!r} provided twice "
                    f"({previous.origin} and {bundle.origin})",
                )
            bundles[bundle.plugin_id] = bundle
    return bundles


def build_plan(ctx: WebContext, payload: dict[str, Any]) -> Planned:
    """Resolve filters and params into an explicit plan (no side effects)."""
    operation = str(payload.get("operation") or "").strip()
    if operation not in {"install", "uninstall", "enable", "disable", "apply"}:
        raise UsageError(
            "operation must be install, uninstall, enable, disable, or apply",
        )
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise UsageError("params must be an object")

    targets, warnings, snapshots = _resolve_runtimes(ctx, payload)
    actions: list[Action] = []
    bundles: dict[str, PluginBundle] = {}

    if operation == "install":
        bundles = _load_bundles(
            ctx,
            _as_str_list(params.get("sources"), "params.sources"),
        )
        options = _sync_options(ctx, params)
        actions = plan_sync(snapshots, bundles, options)

    elif operation in {"uninstall", "enable", "disable"}:
        requested = _as_str_list(params.get("plugins"), "params.plugins")
        if not requested:
            raise UsageError("params.plugins must name at least one plugin")
        protected = frozenset(
            _as_str_list(params.get("protect"), "params.protect"),
        )
        expanded: list[str] = []
        for value in requested:
            if any(char in value for char in "*?["):
                for snapshot in snapshots:
                    for plugin_id in snapshot.plugins:
                        if (
                            matches_patterns(plugin_id, (value,))
                            and plugin_id not in expanded
                        ):
                            expanded.append(plugin_id)
            elif value not in expanded:
                expanded.append(value)
        for snapshot in snapshots:
            if snapshot.error:
                continue
            if operation == "uninstall":
                actions.extend(
                    plan_uninstall(
                        snapshot,
                        expanded,
                        keep_files=_as_bool(params.get("keep_files")),
                        protected=protected,
                    ),
                )
            else:
                actions.extend(
                    plan_toggle(
                        snapshot,
                        expanded,
                        enabled=operation == "enable",
                        protected=protected,
                    ),
                )

    else:  # apply
        state_payload = params.get("state")
        if not isinstance(state_payload, dict):
            raise UsageError("params.state must be a state document object")
        desired = parse_state(state_payload, origin="<request>")
        options = _sync_options(ctx, params)
        patterns = options.plugin_patterns
        options.plugin_patterns = ()
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
        cache: dict[str, dict[str, PluginBundle]] = {}
        for entry in entries:
            if not entry.source:
                if not entry.version:
                    raise UsageError(
                        f"plugin {entry.plugin_id!r} has neither source nor "
                        "version",
                    )
                continue
            if entry.source not in cache:
                cache[entry.source] = _load_bundles(ctx, [entry.source])
            loaded = cache[entry.source]
            bundle = loaded.get(entry.plugin_id)
            if bundle is None:
                raise SourceError(
                    f"{entry.source} does not contain plugin "
                    f"{entry.plugin_id!r}",
                )
            if entry.version and not _same_version(
                entry.version,
                bundle.version,
            ):
                raise UsageError(
                    f"state declares {entry.plugin_id} {entry.version} but "
                    f"{entry.source} contains {bundle.version}",
                )
            bundles[entry.plugin_id] = bundle

        for snapshot in snapshots:
            if snapshot.error:
                continue
            for entry in entries:
                if entry.runtimes and not _matches_runtime(
                    entry.runtimes,
                    snapshot.target,
                ):
                    continue
                bundle = bundles.get(entry.plugin_id)
                if bundle is not None:
                    actions.extend(
                        plan_bundle_for_entry(
                            snapshot,
                            bundle,
                            options,
                            entry.enabled,
                        ),
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
                        protected=options.protected,
                    ),
                )

    actions = runtime_issue_actions(snapshots) + actions
    return Planned(
        operation=operation,
        targets=targets,
        snapshots=snapshots,
        actions=actions,
        bundles=bundles,
        warnings=warnings,
    )


def plan_bundle_for_entry(
    snapshot: RuntimeSnapshot,
    bundle: PluginBundle,
    options: SyncOptions,
    enabled: bool | None,
) -> list[Action]:
    """Plan one state-file entry, honouring its ``enabled`` field."""
    per_entry = dataclasses.replace(options, enable=enabled, plugin_patterns=())
    return plan_bundle(snapshot, bundle, per_entry)


def _matches_runtime(patterns: tuple[str, ...], target: RuntimeTarget) -> bool:
    return any(
        matches_patterns(target.runtime_id, (pattern,))
        or matches_patterns(target.container_name, (pattern,))
        for pattern in patterns
    )


def _same_version(left: str, right: str) -> bool:
    left_version = parse_version(left)
    right_version = parse_version(right)
    if left_version is None or right_version is None:
        return left.strip() == right.strip()
    return left_version == right_version


# ── job execution ────────────────────────────────────────────────────────


def start_job(ctx: WebContext, payload: dict[str, Any]) -> Job:
    """Validate the request shape and start a background plan+execute job."""
    operation = str(payload.get("operation") or "").strip()
    if operation not in {"install", "uninstall", "enable", "disable", "apply"}:
        raise UsageError("unknown operation")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise UsageError("params must be an object")
    # Resolve upload references now: a typo should fail the request, not the job.
    if operation == "install":
        ctx.resolve_sources(_as_str_list(params.get("sources"), "params.sources"))

    with ctx.lock:
        if ctx.active_job_id is not None:
            active = ctx.jobs.get(ctx.active_job_id)
            raise RuntimeError(
                "a job is already running"
                + (f" ({active.operation} {active.job_id})" if active else ""),
            )
        job = Job(
            job_id=uuid.uuid4().hex[:12],
            operation=operation,
            params=dict(params),
            status="running",
            phase="planning",
            started_at=_now(),
        )
        ctx.jobs[job.job_id] = job
        ctx.job_order.append(job.job_id)
        while len(ctx.job_order) > _JOB_HISTORY:
            expired = ctx.job_order.pop(0)
            ctx.jobs.pop(expired, None)
        ctx.active_job_id = job.job_id

    ctx.log(f"job {job.job_id}: {operation} started")
    thread = threading.Thread(
        target=_run_job,
        args=(ctx, job, payload),
        name=f"qpm-job-{job.job_id}",
        daemon=True,
    )
    thread.start()
    return job


def _append_result(ctx: WebContext, job: Job, result: ActionResult) -> None:
    with ctx.lock:
        job.results.append(result)


def _run_job(
    ctx: WebContext,
    job: Job,
    payload: dict[str, Any],
) -> None:
    try:
        planned = build_plan(ctx, payload)
        with ctx.lock:
            job.phase = "executing"
            job.warnings = list(planned.warnings)
            job.targets = list(planned.targets)
            job.snapshots = list(planned.snapshots)
            job.actions = list(planned.actions)
            job.bundles = dict(planned.bundles)
        executor = Executor(
            planned.bundles,
            dataclasses.replace(ctx.config.exec_options),
            on_result=lambda result: _append_result(ctx, job, result),
        )
        results = executor.run(planned.snapshots, planned.actions)
        with ctx.lock:
            # The callback streams results as they land; keep the returned
            # list as the source of truth (it also holds fail-fast remnants).
            if len(results) != len(job.results):
                job.results = list(results)
            failed = any(
                result.state in {ActionState.FAILED, ActionState.ROLLED_BACK}
                for result in job.results
            )
            job.status = "failed" if failed else "success"
    except QpmError as exc:
        with ctx.lock:
            job.status = "failed"
            job.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        with ctx.lock:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
    finally:
        with ctx.lock:
            job.phase = "done"
            job.finished_at = _now()
            if ctx.active_job_id == job.job_id:
                ctx.active_job_id = None
        ctx.log(
            f"job {job.job_id}: {job.status}"
            + (f" ({job.error})" if job.error else ""),
        )


# ── HTTP handler ─────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """One request; see the module docstring for the security rules."""

    protocol_version = "HTTP/1.1"
    server_version = "qpm-web"

    # http.server API -----------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return  # qpm logs job lifecycle itself; polling would be noise

    @property
    def ctx(self) -> WebContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    # responses -----------------------------------------------------------

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        # Errors can happen before the body is consumed; closing the
        # connection avoids leaving unread bytes on a keep-alive socket.
        self.close_connection = True
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self._send(
            status,
            body,
            "application/json; charset=utf-8",
            {"Connection": "close"},
        )

    # request helpers -----------------------------------------------------

    def _authorized(self) -> bool:
        if not self.ctx.config.token_required:
            return True
        supplied = self.headers.get("X-QPM-Token", "")
        expected = self.ctx.config.token
        if expected and supplied and hmac.compare_digest(supplied, expected):
            return True
        self._error(401, "missing or invalid console token")
        return False

    def _same_origin(self) -> bool:
        """Reject state-changing requests that a foreign page triggered."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        expected = {f"http://{host}", f"https://{host}"}
        return origin in expected

    def _read_body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise UsageError("invalid Content-Length")
        if length < 0:
            raise UsageError("invalid Content-Length")
        if length > limit:
            raise UsageError(
                f"request body is larger than {limit // (1024 * 1024)} MiB",
            )
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").lower()
        if not content_type.startswith("application/json"):
            raise UsageError("Content-Type must be application/json")
        body = self._read_body(_MAX_JSON_BYTES)
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise UsageError("JSON body must be an object")
        return payload

    # routing -------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        try:
            if method == "GET" and path in _STATIC_FILES:
                self._serve_static(path)
                return
            if not path.startswith("/api/"):
                self._error(404, "not found")
                return
            if not self._authorized():
                return
            if method == "POST" and not self._same_origin():
                self._error(403, "cross-origin requests are not allowed")
                return
            if method == "GET":
                self._route_get(path, parse_qs(parsed.query))
            else:
                self._route_post(path)
        except UsageError as exc:
            self._error(400, str(exc))
        except QpmError as exc:
            self._error(400, str(exc))
        except BrokenPipeError:  # pragma: no cover - browser navigated away
            return
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            self.ctx.log(f"internal error on {method} {path}: {exc}")
            self._error(500, f"internal error: {type(exc).__name__}: {exc}")

    def _serve_static(self, path: str) -> None:
        filename, content_type = _STATIC_FILES[path]
        file_path = WEB_DIR / filename
        if not file_path.is_file():
            self._error(500, f"console asset is missing: {filename}")
            return
        self._send(200, file_path.read_bytes(), content_type)

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/meta":
            config = self.ctx.config
            self._json(
                200,
                {
                    "version": __version__,
                    "token_required": config.token_required,
                    "host_sdk": config.host_sdk,
                    "discovery": {
                        "source": (
                            "inventory"
                            if config.discovery.inventory
                            else "docker"
                        ),
                        "inventory": (
                            str(config.discovery.inventory)
                            if config.discovery.inventory
                            else ""
                        ),
                        "docker_instance": config.discovery.docker_instance,
                    },
                    "execution": {
                        "concurrency": config.exec_options.concurrency,
                        "dry_run": config.exec_options.dry_run,
                        "backup": config.exec_options.backup,
                        "backup_root": str(config.exec_options.backup_root),
                        "rollback": config.exec_options.rollback,
                        "verify": config.exec_options.verify,
                        "interrupt_tasks": config.exec_options.interrupt_tasks,
                    },
                },
            )
            return
        if path == "/api/runtimes":
            self._json(200, self._runtimes_payload(query))
            return
        if path == "/api/inventory":
            self._json(200, self._inventory_payload(query))
            return
        if path == "/api/jobs":
            self._json(200, {"jobs": self.ctx.job_list()})
            return
        if path == "/api/jobs/active":
            active = self.ctx.active_job()
            self._json(
                200,
                {"job": active.detail() if active else None},
            )
            return
        if path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/") :]
            job = self.ctx.get_job(job_id)
            if job is None:
                self._error(404, f"no job {job_id!r}")
                return
            with self.ctx.lock:
                detail = job.detail()
            self._json(200, {"job": detail})
            return
        if path == "/api/freeze":
            self._freeze(query)
            return
        self._error(404, "not found")

    def _route_post(self, path: str) -> None:
        if path == "/api/plan":
            payload = self._read_json()
            planned = build_plan(self.ctx, payload)
            self._json(200, planned.payload())
            return
        if path == "/api/run":
            payload = self._read_json()
            try:
                job = start_job(self.ctx, payload)
            except RuntimeError as exc:
                self._error(409, str(exc))
                return
            self._json(202, {"job_id": job.job_id})
            return
        if path == "/api/upload":
            self._upload()
            return
        self._error(404, "not found")

    # endpoints -----------------------------------------------------------

    def _filters_from_query(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "runtime": query.get("runtime", []),
            "tenant": query.get("tenant", []),
            "owner": query.get("owner", []),
            "include_stopped": (
                (query.get("include_stopped") or ["0"])[0].lower()
                in {"1", "true", "yes"}
            ),
        }

    def _runtimes_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        filters = self._filters_from_query(query)
        discovered = discover_runtimes(self.ctx.config.discovery)
        targets, warnings = select_targets(
            discovered.targets,
            patterns=filters["runtime"],
            tenants=filters["tenant"],
            owners=filters["owner"],
            include_stopped=filters["include_stopped"],
        )
        return {
            "source": discovered.source,
            "warnings": [*discovered.warnings, *warnings],
            "runtimes": [_target_dict(target) for target in targets],
        }

    def _inventory_payload(
        self,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        filters = self._filters_from_query(query)
        discovered = discover_runtimes(self.ctx.config.discovery)
        targets, warnings = select_targets(
            discovered.targets,
            patterns=filters["runtime"],
            tenants=filters["tenant"],
            owners=filters["owner"],
            include_stopped=filters["include_stopped"],
        )
        snapshots = fetch_inventory(
            targets,
            concurrency=self.ctx.config.exec_options.concurrency,
            timeout=self.ctx.config.exec_options.timeout,
            operation_timeout=self.ctx.config.exec_options.operation_timeout,
            retries=self.ctx.config.exec_options.retries,
        )
        rows = []
        for snapshot in snapshots:
            rows.append(
                {
                    **_target_dict(snapshot.target),
                    "qwenpaw_version": snapshot.app_version,
                    "upload_limit_mb": snapshot.upload_limit_mb,
                    "error": snapshot.error,
                    "plugins": [
                        {
                            "id": plugin.plugin_id,
                            "version": plugin.version,
                            "enabled": plugin.enabled,
                            "loaded": plugin.loaded,
                            "status": plugin.status,
                            "type": plugin.plugin_type,
                        }
                        for plugin in sorted(
                            snapshot.plugins.values(),
                            key=lambda item: item.plugin_id,
                        )
                    ],
                },
            )
        return {
            "source": discovered.source,
            "warnings": [
                *discovered.warnings,
                *warnings,
                *[
                    f"{snapshot.target.runtime_id}: {snapshot.error}"
                    for snapshot in snapshots
                    if snapshot.error
                ],
            ],
            "runtimes": rows,
        }

    def _freeze(self, query: dict[str, list[str]]) -> None:
        runtime_id = (query.get("runtime") or [""])[0].strip()
        if not runtime_id:
            raise UsageError("runtime query parameter is required")
        discovered = discover_runtimes(self.ctx.config.discovery)
        targets, _ = select_targets(
            discovered.targets,
            patterns=[runtime_id],
            include_stopped=True,
        )
        if len(targets) != 1:
            raise UsageError(
                f"runtime {runtime_id!r} matched {len(targets)} targets; "
                "freeze needs exactly one",
            )
        snapshot = fetch_inventory(
            targets,
            concurrency=1,
            timeout=self.ctx.config.exec_options.timeout,
            retries=self.ctx.config.exec_options.retries,
        )[0]
        if snapshot.error:
            raise UsageError(
                f"cannot read {runtime_id}: {snapshot.error}",
            )
        payload = freeze_payload(snapshot)
        body = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8",
        )
        self._send(
            200,
            body,
            "application/json; charset=utf-8",
            {
                "Content-Disposition": (
                    f'attachment; filename="{runtime_id}-plugins.lock.json"'
                ),
            },
        )

    def _upload(self) -> None:
        content_type = self.headers.get("Content-Type") or ""
        if not content_type.lower().startswith("multipart/form-data"):
            raise UsageError("upload must be multipart/form-data")
        body = self._read_body(_MAX_UPLOAD_BYTES)
        raw = (
            b"Content-Type: "
            + content_type.encode("latin-1")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        message = BytesParser(policy=email_policy).parsebytes(raw)
        for part in message.iter_parts():
            disposition = part.get("Content-Disposition", "")
            if "form-data" not in disposition:
                continue
            if part.get_param("name", header="content-disposition") != "file":
                continue
            filename = part.get_filename() or "bundle.zip"
            if not filename.lower().endswith(".zip"):
                raise UsageError("only .zip plugin bundles can be uploaded")
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes) or not data:
                raise UsageError("uploaded file is empty")
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    archive.namelist()
            except zipfile.BadZipFile as exc:
                raise UsageError(f"uploaded file is not a ZIP: {exc}") from exc
            self._json(200, self.ctx.save_upload(filename, data))
            return
        raise UsageError("multipart body has no 'file' field")


# ── server lifecycle ─────────────────────────────────────────────────────


def create_server(config: WebConfig) -> ThreadingHTTPServer:
    """Create the console server without starting it (port 0 is allowed)."""
    if not config.token:
        config.token = secrets.token_urlsafe(24)
    if not config.token_required:
        config.token = ""
    context = WebContext(config)
    server = ThreadingHTTPServer((config.host, config.port), _Handler)
    server.daemon_threads = True
    server.ctx = context  # type: ignore[attr-defined]
    return server


def _reachable_addresses() -> list[str]:
    """Best-effort list of this host's non-loopback IPv4 addresses."""
    addresses: list[str] = []
    try:
        for info in socket.getaddrinfo(
            socket.gethostname(),
            None,
            socket.AF_INET,
        ):
            address = str(info[4][0])
            if address not in addresses and not address.startswith("127."):
                addresses.append(address)
    except OSError:
        pass
    if not addresses:
        # Hostnames often do not resolve inside containers; ask the routing
        # table instead (UDP connect sends nothing).
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))  # TEST-NET-1
                address = str(probe.getsockname()[0])
                if address and not address.startswith("127."):
                    addresses.append(address)
        except OSError:
            pass
    return addresses


def serve(config: WebConfig) -> int:
    """Run the console until interrupted."""
    server = create_server(config)
    context: WebContext = server.ctx  # type: ignore[attr-defined]
    host, port = server.server_address[:2]
    bound_everywhere = str(host) in {"0.0.0.0", "::", ""}
    display_host = LOOPBACK_HOST if bound_everywhere else host
    token_fragment = (
        f"#token={config.token}" if config.token_required else ""
    )
    url = f"http://{display_host}:{port}/{token_fragment}"
    loopback = str(host) in {LOOPBACK_HOST, "::1", "localhost"}
    if not loopback:
        print(
            f"[qpm] WARNING: console is listening on {host}:{port} (all "
            "interfaces); anyone who can reach this port"
            + (" and holds the token" if config.token_required else "")
            + " can change every runtime",
            file=sys.stderr,
            flush=True,
        )
    if not config.token_required:
        print(
            "[qpm] WARNING: console token disabled; any page or script that "
            "can reach this port can change the fleet",
            file=sys.stderr,
            flush=True,
        )
    if config.token_required:
        print(
            f"[qpm] console token: {config.token}",
            file=sys.stderr,
            flush=True,
        )
    print(f"[qpm] console ready: {url}", file=sys.stderr, flush=True)
    if bound_everywhere:
        for address in _reachable_addresses():
            print(
                f"[qpm] also reachable at: http://{address}:{port}/"
                f"{token_fragment}",
                file=sys.stderr,
                flush=True,
            )
    print(
        "[qpm] listening on all interfaces; Ctrl-C to stop"
        if bound_everywhere
        else "[qpm] loopback-only by default; Ctrl-C to stop",
        file=sys.stderr,
        flush=True,
    )
    if config.open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless hosts are normal
            pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("[qpm] shutting down", file=sys.stderr, flush=True)
    finally:
        server.shutdown()
        server.server_close()
        context.cleanup()
    return 0
