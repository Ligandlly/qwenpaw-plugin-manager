# -*- coding: utf-8 -*-
"""HTTP client behaviour against the fake runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from qpm.bundles import build_bundle_from_dir
from qpm.errors import (
    PluginAlreadyInstalled,
    RuntimeAuthError,
    RuntimeUnreachable,
)
from qpm.models import RuntimeTarget
from qpm.runtime_client import RuntimeClient


def target_for(runtime) -> RuntimeTarget:
    return RuntimeTarget(
        runtime_id=runtime.runtime_id,
        base_url=runtime.url,
        token=runtime.token,
        tenant_id="tenant-a",
        owner_id="owner-a",
        plugins_dir_host=str(runtime.host_plugins_dir),
        workspace_container_path="/workspace",
    )


def test_metadata_and_empty_inventory(runtime_factory) -> None:
    runtime = runtime_factory("rt-1")
    client = RuntimeClient(target_for(runtime))
    assert client.app_version() == "2.2.2"
    assert client.upload_limit_mb() is None
    assert client.list_plugins() == {}
    healthy, reason = client.healthz()
    assert healthy is True and reason == ""


def test_healthz_reports_starting(runtime_factory) -> None:
    runtime = runtime_factory("rt-1", healthy=False)
    healthy, reason = RuntimeClient(target_for(runtime)).healthz()
    assert healthy is False
    assert reason == "booting"


def test_upload_install_and_force_replace(
    runtime_factory,
    plugin_factory,
) -> None:
    runtime = runtime_factory("rt-1")
    client = RuntimeClient(target_for(runtime))
    bundle = build_bundle_from_dir(plugin_factory(version="1.0.0"))

    payload = client.upload_plugin(bundle, force=False)
    assert payload["id"] == "offline-market"
    assert payload["version"] == "1.0.0"

    installed = client.list_plugins()
    assert installed["offline-market"].version == "1.0.0"
    assert (Path(runtime.host_plugins_dir) / "offline-market" / "plugin.json").is_file()

    with pytest.raises(PluginAlreadyInstalled):
        client.upload_plugin(bundle, force=False)

    newer = build_bundle_from_dir(plugin_factory(version="2.0.0"))
    client.upload_plugin(newer, force=True)
    assert client.get_plugin("offline-market").version == "2.0.0"


def test_uninstall_and_missing(runtime_factory, plugin_factory) -> None:
    runtime = runtime_factory("rt-1")
    client = RuntimeClient(target_for(runtime))
    assert client.uninstall_plugin("absent") is None

    client.upload_plugin(
        build_bundle_from_dir(plugin_factory(version="1.0.0")),
        force=False,
    )
    assert client.uninstall_plugin("offline-market") is not None
    assert client.get_plugin("offline-market") is None
    assert not (
        Path(runtime.host_plugins_dir) / "offline-market"
    ).exists()


def test_unload_keeps_files(runtime_factory, plugin_factory) -> None:
    runtime = runtime_factory("rt-1")
    client = RuntimeClient(target_for(runtime))
    client.upload_plugin(
        build_bundle_from_dir(plugin_factory(version="1.0.0")),
        force=False,
    )
    client.uninstall_plugin("offline-market", keep_files=True)
    plugin = client.get_plugin("offline-market")
    assert plugin is not None
    assert plugin.loaded is False
    assert (Path(runtime.host_plugins_dir) / "offline-market").is_dir()


def test_set_enabled(runtime_factory, plugin_factory) -> None:
    runtime = runtime_factory("rt-1")
    client = RuntimeClient(target_for(runtime))
    client.upload_plugin(
        build_bundle_from_dir(plugin_factory(version="1.0.0")),
        force=False,
    )
    client.set_enabled("offline-market", False)
    assert client.get_plugin("offline-market").enabled is False
    client.set_enabled("offline-market", True)
    assert client.get_plugin("offline-market").enabled is True


def test_bad_token(runtime_factory) -> None:
    runtime = runtime_factory("rt-1")
    target = RuntimeTarget(
        runtime_id="rt-1",
        base_url=runtime.url,
        token="wrong",
    )
    with pytest.raises(RuntimeAuthError):
        RuntimeClient(target, retries=0).list_plugins()


def test_transient_503_is_retried(runtime_factory, plugin_factory) -> None:
    runtime = runtime_factory("rt-1")
    runtime.reject_next_upload_status = 503
    client = RuntimeClient(target_for(runtime), retries=2, backoff=0)
    client.upload_plugin(
        build_bundle_from_dir(plugin_factory(version="1.0.0")),
        force=False,
    )
    assert client.get_plugin("offline-market") is not None


def test_unreachable_endpoint() -> None:
    target = RuntimeTarget(
        runtime_id="rt-dead",
        base_url="http://127.0.0.1:1",
        token="t",
    )
    with pytest.raises(RuntimeUnreachable):
        RuntimeClient(target, retries=0, timeout=1).list_plugins()
