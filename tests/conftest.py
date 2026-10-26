# -*- coding: utf-8 -*-
"""Shared fixtures: plugin directories, fake runtimes, inventory files."""

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_runtime import FakeRuntime  # noqa: E402


def _write_plugin(
    directory: Path,
    *,
    name: str,
    plugin_id: str | None = None,
    version: str = "0.1.0",
    plugin_type: str = "hub",
    sdk_version: str = "1.0.0",
    qwenpaw_min: str = "2.2.2",
    extra: dict | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id or name,
        "name": name,
        "version": version,
        "description": f"{name} test plugin",
        "author": "tests",
        "type": plugin_type,
        "entry": {"backend": "backend.py"},
        "sdk": {"version": sdk_version, "capabilities": []},
        "qwenpaw_version": {"min": qwenpaw_min},
    }
    if extra:
        manifest.update(extra)
    (directory / "plugin.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    (directory / "backend.py").write_text(
        "PLUGIN_ID = %r\n" % manifest["id"],
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def plugin_factory(tmp_path: Path) -> Callable[..., Path]:
    """Return a factory that writes a valid plugin directory."""

    counter = {"n": 0}

    def factory(
        name: str = "offline-market",
        *,
        version: str = "0.1.0",
        directory: Path | None = None,
        **kwargs,
    ) -> Path:
        counter["n"] += 1
        target = directory or (
            tmp_path
            / "sources"
            / f"{name}-{version}-{counter['n']}"
        )
        return _write_plugin(target, name=name, version=version, **kwargs)

    return factory


class RuntimeFactory:
    """Create fake runtimes and their host working directories."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.created: list[FakeRuntime] = []

    def __call__(
        self,
        runtime_id: str = "rt-1",
        *,
        plugins_dir: bool = True,
        **kwargs,
    ) -> FakeRuntime:
        working = self.root / runtime_id
        plugins = working / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        runtime = FakeRuntime(
            runtime_id=runtime_id,
            plugins_dir=plugins if plugins_dir else None,
            **kwargs,
        )
        runtime.start()
        self.created.append(runtime)
        return runtime

    def close(self) -> None:
        for runtime in self.created:
            runtime.stop()
        self.created.clear()


@pytest.fixture
def runtime_factory(tmp_path: Path) -> Iterator[RuntimeFactory]:
    factory = RuntimeFactory(tmp_path / "host")
    try:
        yield factory
    finally:
        factory.close()


@pytest.fixture
def file_server(tmp_path: Path) -> Iterator[str]:
    """Serve ``tmp_path`` over HTTP for bundle URL tests."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def inventory_file(tmp_path: Path) -> Callable[..., Path]:
    """Write an inventory JSON file for a set of fake runtimes."""

    def factory(
        runtimes,
        *,
        name: str = "inventory.json",
        workspace: str = "/workspace",
    ) -> Path:
        entries = []
        for runtime in runtimes:
            entries.append(
                {
                    "runtime_id": runtime.runtime_id,
                    "tenant_id": f"tenant-{runtime.runtime_id}",
                    "owner_id": f"owner-{runtime.runtime_id}",
                    "url": runtime.url,
                    "token": runtime.token,
                    "state": "running",
                    "workspace": workspace,
                    "plugins_dir": str(runtime.host_plugins_dir),
                    "container_name": f"qwenpaw-hub-test-{runtime.runtime_id}",
                },
            )
        path = tmp_path / name
        path.write_text(
            json.dumps({"runtimes": entries}, indent=2),
            encoding="utf-8",
        )
        return path

    return factory
