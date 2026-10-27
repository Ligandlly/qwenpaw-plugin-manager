# -*- coding: utf-8 -*-
"""Runtime discovery: Docker containers first, static inventory as fallback.

QwenPaw Hub's Docker provisioner labels every runtime container with

``io.qwenpaw.hub.managed=true``
``io.qwenpaw.hub.instance=<hub instance fingerprint>``
``io.qwenpaw.hub.runtime-id=<runtime>``
``io.qwenpaw.hub.owner-id=<hub user>``

and injects ``QWENPAW_RUNTIME_INTERNAL_TOKEN`` into the container environment.
That is everything this tool needs: the runtime endpoint is a loopback-published
port, authenticated with the same boundary token the Hub uses for readiness
checks.  No Hub database, no Fernet key, no QwenPaw import.

When the Hub host's Docker socket is not reachable from where the manager runs,
``--inventory inventory.json`` supplies the same facts by hand.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import logs
from .errors import DiscoveryError
from .models import RuntimeTarget

__all__ = [
    "DiscoveryOptions",
    "DiscoveryResult",
    "discover_runtimes",
    "select_targets",
]

MANAGED_LABEL = "io.qwenpaw.hub.managed"
INSTANCE_LABEL = "io.qwenpaw.hub.instance"
RUNTIME_LABEL = "io.qwenpaw.hub.runtime-id"
OWNER_LABEL = "io.qwenpaw.hub.owner-id"
TOKEN_ENV = "QWENPAW_RUNTIME_INTERNAL_TOKEN"

_DOCKER_TIMEOUT_SECONDS = 60

_LOG = logs.get_logger("docker")


@dataclass
class DiscoveryOptions:
    """Where runtimes come from and how strictly to filter them."""

    inventory: Path | None = None
    docker_bin: str = "docker"
    docker_instance: str = ""
    include_stopped: bool = False
    container_port: int = 8088


@dataclass
class DiscoveryResult:
    """Discovered runtimes plus human-readable notes."""

    targets: list[RuntimeTarget] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = "docker"


# ── Docker ───────────────────────────────────────────────────────────────


def _run_docker(binary: str, args: list[str]) -> str:
    command = " ".join([binary, *args])
    _LOG.debug("$ %s", command)
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - explicit argument list
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DiscoveryError(
            f"docker CLI not found ({binary!r}); install Docker or pass "
            "--inventory with a JSON runtime list",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryError(
            f"docker {' '.join(args[:2])} timed out after "
            f"{_DOCKER_TIMEOUT_SECONDS}s",
        ) from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        hint = ""
        if "permission denied" in stderr.lower():
            hint = (
                " — add the current user to the 'docker' group or run as a "
                "user with access to the Docker socket"
            )
        raise DiscoveryError(
            f"docker {args[0]} failed ({completed.returncode}): "
            f"{stderr.splitlines()[-1] if stderr else 'no output'}{hint}",
        )
    _LOG.debug(
        "$ %s -> %d line(s) in %dms",
        command,
        len(completed.stdout.splitlines()),
        int((time.monotonic() - started) * 1000),
    )
    return completed.stdout


def _parse_env(raw_env: list[str] | None) -> dict[str, str]:
    environment: dict[str, str] = {}
    for item in raw_env or []:
        if not isinstance(item, str) or "=" not in item:
            continue
        name, value = item.split("=", 1)
        environment[name] = value
    return environment


def _published_port(item: dict[str, Any], container_port: int) -> tuple[int, str]:
    ports = (item.get("NetworkSettings") or {}).get("Ports") or {}
    bindings = list(ports.get(f"{container_port}/tcp") or [])
    if not bindings:
        # Fall back to any published loopback port, in case the image changed
        # its listening port.
        for key in sorted(ports):
            values = ports.get(key) or []
            if values:
                bindings = list(values)
                break
    for binding in bindings:
        host_ip = str(binding.get("HostIp") or "")
        if host_ip not in {"", "127.0.0.1", "::1"}:
            # Hub only ever publishes loopback; anything else is not ours.
            continue
        try:
            port = int(binding.get("HostPort") or 0)
        except (TypeError, ValueError):
            continue
        if port:
            host = "[::1]" if host_ip == "::1" else "127.0.0.1"
            return port, host
    return 0, ""


def _host_plugins_dir(
    item: dict[str, Any],
    workspace: str,
) -> str:
    mounts = item.get("Mounts") or []
    wanted = PurePosixPath(workspace.rstrip("/") or "/workspace")
    for mount in mounts:
        destination = PurePosixPath(
            str(mount.get("Destination") or "").rstrip("/") or "/",
        )
        if destination == wanted and mount.get("Source"):
            return str(Path(str(mount["Source"])) / "plugins")
    return ""


def _target_from_inspect(
    item: dict[str, Any],
    *,
    container_port: int,
) -> tuple[RuntimeTarget | None, str]:
    container_id = str(item.get("Id") or "")
    name = str(item.get("Name") or "").lstrip("/")
    config = item.get("Config") or {}
    labels = config.get("Labels") or {}
    runtime_id = str(labels.get(RUNTIME_LABEL) or "")
    if not runtime_id:
        return None, f"container {name or container_id[:12]}: no runtime-id label"
    environment = _parse_env(config.get("Env"))
    token = environment.get(TOKEN_ENV, "")
    if not token:
        return (
            None,
            f"runtime {runtime_id}: container has no {TOKEN_ENV}; only "
            "Hub-managed runtimes with the boundary token are supported",
        )
    port, host = _published_port(item, container_port)
    if not port:
        return None, f"runtime {runtime_id}: no loopback port published"
    state = str((item.get("State") or {}).get("Status") or "unknown")
    workspace = environment.get("QWENPAW_WORKING_DIR", "/workspace")
    return (
        RuntimeTarget(
            runtime_id=runtime_id,
            base_url=f"http://{host}:{port}",
            token=token,
            tenant_id=environment.get("QWENPAW_TENANT_ID", ""),
            owner_id=str(labels.get(OWNER_LABEL) or ""),
            state=state,
            image=str(config.get("Image") or ""),
            container_id=container_id,
            container_name=name,
            workspace_container_path=workspace,
            plugins_dir_host=_host_plugins_dir(item, workspace),
            instance=str(labels.get(INSTANCE_LABEL) or ""),
            source="docker",
        ),
        "",
    )


def discover_docker(options: DiscoveryOptions) -> DiscoveryResult:
    """Enumerate Hub runtime containers through the Docker CLI."""
    binary = options.docker_bin or "docker"
    if shutil.which(binary) is None and not Path(binary).exists():
        raise DiscoveryError(
            f"docker CLI not found ({binary!r}); pass --inventory to read "
            "runtimes from a JSON file instead",
        )
    args = ["ps", "-a", "--filter", f"label={MANAGED_LABEL}=true"]
    if options.docker_instance:
        args += ["--filter", f"label={INSTANCE_LABEL}={options.docker_instance}"]
    args += ["--format", "{{.ID}}"]
    ids = [line.strip() for line in _run_docker(binary, args).splitlines()]
    ids = [item for item in ids if item]
    _LOG.debug(
        "found %d Hub-managed container(s)%s",
        len(ids),
        f" for instance {options.docker_instance}"
        if options.docker_instance
        else "",
    )
    result = DiscoveryResult(source="docker")
    if not ids:
        result.warnings.append(
            "no QwenPaw Hub runtime containers found"
            + (
                f" for instance {options.docker_instance}"
                if options.docker_instance
                else ""
            ),
        )
        return result

    raw = _run_docker(binary, ["inspect", *ids])
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"docker inspect returned invalid JSON: {exc}") from exc
    for item in items:
        target, warning = _target_from_inspect(
            item,
            container_port=options.container_port,
        )
        if target is not None:
            _LOG.debug(
                "runtime %s (%s) -> %s [%s]",
                target.runtime_id,
                target.container_name or target.container_id[:12],
                target.base_url,
                target.state,
            )
            result.targets.append(target)
        elif warning:
            _LOG.debug("skipped: %s", warning)
            result.warnings.append(warning)
    return result


# ── Static inventory ─────────────────────────────────────────────────────


def _inventory_entries(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DiscoveryError(f"{path}: cannot read inventory: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"{path}: inventory is not valid JSON: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("runtimes", payload.get("targets"))
    if not isinstance(payload, list):
        raise DiscoveryError(
            f"{path}: expected a JSON list or an object with a 'runtimes' list",
        )
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise DiscoveryError(f"{path}: runtimes[{index}] is not an object")
        entries.append(entry)
    return entries


def discover_inventory(path: Path) -> DiscoveryResult:
    """Read runtimes from a hand-maintained JSON inventory."""
    path = path.expanduser()
    if not path.is_file():
        raise DiscoveryError(f"{path}: inventory file not found")
    _LOG.debug("reading inventory %s", path)
    base = path.resolve().parent
    result = DiscoveryResult(source="inventory")
    seen: dict[str, str] = {}
    for index, entry in enumerate(_inventory_entries(path)):
        runtime_id = str(entry.get("runtime_id") or "").strip()
        if not runtime_id:
            raise DiscoveryError(f"{path}: runtimes[{index}] has no runtime_id")
        url = str(entry.get("url") or entry.get("base_url") or "").strip()
        if not url:
            raise DiscoveryError(f"{path}: {runtime_id} has no 'url'")
        token = str(entry.get("token") or "")
        token_env = str(entry.get("token_env") or "")
        if not token and token_env:
            token = os.environ.get(token_env, "")
            if not token:
                result.warnings.append(
                    f"{runtime_id}: token_env {token_env} is not set",
                )
        if not token:
            raise DiscoveryError(
                f"{path}: {runtime_id} has no 'token' or a usable 'token_env'",
            )
        plugins_dir = str(
            entry.get("plugins_dir") or entry.get("plugins_dir_host") or "",
        )
        if plugins_dir:
            plugins_path = Path(plugins_dir).expanduser()
            if not plugins_path.is_absolute():
                plugins_path = base / plugins_path
            plugins_dir = str(plugins_path)
        previous = seen.get(runtime_id)
        if previous is not None:
            raise DiscoveryError(
                f"{path}: duplicate runtime_id {runtime_id!r} "
                f"({previous} and {url})",
            )
        seen[runtime_id] = url
        _LOG.debug(
            "runtime %s -> %s (token %s)",
            runtime_id,
            url.rstrip("/"),
            "present" if token else "missing",
        )
        result.targets.append(
            RuntimeTarget(
                runtime_id=runtime_id,
                base_url=url.rstrip("/"),
                token=token,
                tenant_id=str(entry.get("tenant_id") or ""),
                owner_id=str(entry.get("owner_id") or ""),
                state=str(entry.get("state") or "running"),
                image=str(entry.get("image") or ""),
                container_id=str(entry.get("container_id") or ""),
                container_name=str(entry.get("container_name") or ""),
                workspace_container_path=str(
                    entry.get("workspace") or entry.get("workspace_container_path") or "",
                ),
                plugins_dir_host=plugins_dir,
                instance=str(entry.get("instance") or ""),
                source="inventory",
            ),
        )
    return result


# ── Entry point ──────────────────────────────────────────────────────────


def discover_runtimes(options: DiscoveryOptions) -> DiscoveryResult:
    """Discover runtimes from the configured source."""
    if options.inventory is not None:
        return discover_inventory(options.inventory)
    return discover_docker(options)


def _matches(pattern: str, value: str) -> bool:
    return fnmatch.fnmatchcase(value, pattern)


def select_targets(
    targets: list[RuntimeTarget],
    *,
    patterns: list[str] | None = None,
    tenants: list[str] | None = None,
    owners: list[str] | None = None,
    include_stopped: bool = False,
) -> tuple[list[RuntimeTarget], list[str]]:
    """Apply runtime/tenant/owner filters and the running-state filter."""
    warnings: list[str] = []
    selected: list[RuntimeTarget] = []
    stopped = 0
    for target in targets:
        if patterns and not any(
            _matches(pattern, target.runtime_id)
            or _matches(pattern, target.container_name)
            for pattern in patterns
        ):
            continue
        if tenants and not any(_matches(pattern, target.tenant_id) for pattern in tenants):
            continue
        if owners and not any(_matches(pattern, target.owner_id) for pattern in owners):
            continue
        if not include_stopped and not target.running:
            stopped += 1
            continue
        selected.append(target)
    if stopped:
        warnings.append(
            f"{stopped} runtime(s) skipped because they are not running; "
            "use --include-stopped to include them",
        )
    selected.sort(key=lambda target: (target.runtime_id, target.base_url))
    _LOG.debug(
        "filters (runtime=%s tenant=%s owner=%s include_stopped=%s) "
        "selected %d of %d runtime(s), %d skipped as stopped",
        patterns or "-",
        tenants or "-",
        owners or "-",
        include_stopped,
        len(selected),
        len(targets),
        stopped,
    )
    return selected, warnings
