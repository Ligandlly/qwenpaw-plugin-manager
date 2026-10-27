# -*- coding: utf-8 -*-
"""Read the plugin inventory of every selected runtime in parallel."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import logs
from .errors import QpmError
from .models import RuntimeSnapshot, RuntimeTarget
from .runtime_client import RuntimeClient

__all__ = ["fetch_snapshot", "fetch_inventory"]

_LOG = logs.get_logger("inventory")


def fetch_snapshot(
    target: RuntimeTarget,
    *,
    timeout: float = 30.0,
    operation_timeout: float = 180.0,
    retries: int = 2,
) -> RuntimeSnapshot:
    """Collect version and installed plugins for one runtime."""
    snapshot = RuntimeSnapshot(target=target)
    if not target.running:
        snapshot.error = f"runtime is {target.state}, not running"
        _LOG.debug("%s: skipped, %s", target.runtime_id, snapshot.error)
        return snapshot
    client = RuntimeClient(
        target,
        timeout=timeout,
        operation_timeout=operation_timeout,
        retries=retries,
    )
    _LOG.debug(
        "%s: reading inventory from %s",
        target.runtime_id,
        target.base_url,
    )
    started = time.monotonic()
    try:
        snapshot.app_version = client.app_version()
        snapshot.upload_limit_mb = client.upload_limit_mb()
        snapshot.plugins = client.list_plugins()
    except QpmError as exc:
        snapshot.error = str(exc)
    except Exception as exc:  # defensive: never let one runtime break the run
        snapshot.error = f"{type(exc).__name__}: {exc}"
    if snapshot.error:
        _LOG.debug(
            "%s: inventory failed: %s",
            target.runtime_id,
            snapshot.error,
        )
    else:
        _LOG.debug(
            "%s: qwenpaw %s, %d plugin(s), upload limit %s in %dms",
            target.runtime_id,
            snapshot.app_version or "unknown",
            len(snapshot.plugins),
            (
                f"{snapshot.upload_limit_mb} MiB"
                if snapshot.upload_limit_mb
                else "-"
            ),
            int((time.monotonic() - started) * 1000),
        )
    return snapshot


def fetch_inventory(
    targets: list[RuntimeTarget],
    *,
    concurrency: int = 4,
    timeout: float = 30.0,
    operation_timeout: float = 180.0,
    retries: int = 2,
) -> list[RuntimeSnapshot]:
    """Fetch snapshots concurrently, preserving the input order."""
    if not targets:
        return []
    workers = max(1, min(concurrency, len(targets)))
    _LOG.debug(
        "fetching %d inventory(ies) with %d worker(s)",
        len(targets),
        workers,
    )
    results: dict[str, RuntimeSnapshot] = {}
    if workers == 1:
        for target in targets:
            results[target.key] = fetch_snapshot(
                target,
                timeout=timeout,
                operation_timeout=operation_timeout,
                retries=retries,
            )
        return [results[target.key] for target in targets]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                fetch_snapshot,
                target,
                timeout=timeout,
                operation_timeout=operation_timeout,
                retries=retries,
            ): target
            for target in targets
        }
        for future in as_completed(futures):
            target = futures[future]
            try:
                results[target.key] = future.result()
            except Exception as exc:  # pragma: no cover - fetch_snapshot guards
                snapshot = RuntimeSnapshot(target=target)
                snapshot.error = f"{type(exc).__name__}: {exc}"
                results[target.key] = snapshot
    return [results[target.key] for target in targets]
