# -*- coding: utf-8 -*-
"""Read the plugin inventory of every selected runtime in parallel."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .errors import QpmError
from .models import RuntimeSnapshot, RuntimeTarget
from .runtime_client import RuntimeClient

__all__ = ["fetch_snapshot", "fetch_inventory"]


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
        return snapshot
    client = RuntimeClient(
        target,
        timeout=timeout,
        operation_timeout=operation_timeout,
        retries=retries,
    )
    try:
        snapshot.app_version = client.app_version()
        snapshot.upload_limit_mb = client.upload_limit_mb()
        snapshot.plugins = client.list_plugins()
    except QpmError as exc:
        snapshot.error = str(exc)
    except Exception as exc:  # defensive: never let one runtime break the run
        snapshot.error = f"{type(exc).__name__}: {exc}"
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
