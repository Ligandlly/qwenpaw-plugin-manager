# -*- coding: utf-8 -*-
"""Runtime discovery: Docker labels/ports/tokens and static inventories."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from qpm.errors import DiscoveryError
from qpm.models import RuntimeTarget
from qpm.sources import (
    DiscoveryOptions,
    discover_docker,
    discover_inventory,
    select_targets,
)

_LABELS = {
    "io.qwenpaw.hub.managed": "true",
    "io.qwenpaw.hub.instance": "abc123",
    "io.qwenpaw.hub.runtime-id": "rt-1",
    "io.qwenpaw.hub.owner-id": "user-1",
}

_INSPECT = [
    {
        "Id": "deadbeef",
        "Name": "/qwenpaw-hub-abc123-rt-1",
        "Config": {
            "Image": "agentscope/qwenpaw:latest",
            "Labels": _LABELS,
            "Env": [
                "QWENPAW_TENANT_ID=tenant-a",
                "QWENPAW_WORKING_DIR=/workspace",
                "QWENPAW_RUNTIME_INTERNAL_TOKEN=token-a",
            ],
        },
        "State": {"Status": "running"},
        "NetworkSettings": {
            "Ports": {"8088/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32100"}]}
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/srv/hub/runtimes/rt-1/working",
                "Destination": "/workspace",
            }
        ],
    },
    {
        "Id": "cafebabe",
        "Name": "/qwenpaw-hub-abc123-rt-2",
        "Config": {
            "Image": "agentscope/qwenpaw:latest",
            "Labels": {**_LABELS, "io.qwenpaw.hub.runtime-id": "rt-2"},
            "Env": ["QWENPAW_WORKING_DIR=/workspace"],
        },
        "State": {"Status": "exited"},
        "NetworkSettings": {"Ports": {}},
        "Mounts": [],
    },
]


def _docker_script(tmp_path: Path, payload: object) -> Path:
    script = tmp_path / "docker"
    body = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'ps':\n"
        "    print('deadbeef')\n"
        "    print('cafebabe')\n"
        "elif args[0] == 'inspect':\n"
        f"    print(json.dumps({payload!r}))\n"
    )
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_discover_docker_parses_labels_and_ports(tmp_path: Path) -> None:
    binary = _docker_script(tmp_path, _INSPECT)
    result = discover_docker(DiscoveryOptions(docker_bin=str(binary)))
    assert len(result.targets) == 1
    target = result.targets[0]
    assert target.runtime_id == "rt-1"
    assert target.tenant_id == "tenant-a"
    assert target.owner_id == "user-1"
    assert target.base_url == "http://127.0.0.1:32100"
    assert target.token == "token-a"
    assert target.plugins_dir_host == "/srv/hub/runtimes/rt-1/working/plugins"
    assert target.container_plugins_dir == "/workspace/plugins"
    assert target.state == "running"
    assert target.instance == "abc123"
    # The second container is missing the token and has no port: it is
    # reported, never silently ignored.
    assert any("QWENPAW_RUNTIME_INTERNAL_TOKEN" in item for item in result.warnings)


def test_discover_docker_missing_binary() -> None:
    with pytest.raises(DiscoveryError, match="docker CLI not found"):
        discover_docker(DiscoveryOptions(docker_bin="/nonexistent/docker"))


def test_discover_docker_failure(tmp_path: Path) -> None:
    binary = tmp_path / "docker"
    binary.write_text(
        "#!/bin/sh\n"
        "echo 'permission denied' >&2\n"
        "exit 1\n",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(DiscoveryError, match="permission denied"):
        discover_docker(DiscoveryOptions(docker_bin=str(binary)))


def test_inventory_parsing(tmp_path: Path) -> None:
    token = "inventory-token"
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps(
            [
                {
                    "runtime_id": "rt-a",
                    "tenant_id": "tenant-a",
                    "url": "http://127.0.0.1:9001/",
                    "token_env": "QPM_TEST_TOKEN",
                    "plugins_dir": "relative/plugins",
                    "state": "running",
                },
                {
                    "runtime_id": "rt-b",
                    "url": "http://127.0.0.1:9002",
                    "token": token,
                    "state": "stopped",
                },
            ],
        ),
    )
    os.environ["QPM_TEST_TOKEN"] = token
    try:
        result = discover_inventory(path)
    finally:
        os.environ.pop("QPM_TEST_TOKEN", None)
    assert [target.runtime_id for target in result.targets] == ["rt-a", "rt-b"]
    assert result.targets[0].base_url == "http://127.0.0.1:9001"
    assert result.targets[0].plugins_dir_host == str(
        (tmp_path / "relative" / "plugins"),
    )
    assert result.targets[1].state == "stopped"


def test_inventory_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps(
            [
                {"runtime_id": "x", "url": "http://127.0.0.1:1", "token": "t"},
                {"runtime_id": "x", "url": "http://127.0.0.1:2", "token": "t"},
            ],
        ),
    )
    with pytest.raises(DiscoveryError, match="duplicate"):
        discover_inventory(path)


def test_inventory_requires_token(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps([{"runtime_id": "x", "url": "http://127.0.0.1:1"}]),
    )
    with pytest.raises(DiscoveryError, match="token"):
        discover_inventory(path)


def test_select_targets_filters() -> None:
    options = DiscoveryOptions()
    assert options.container_port == 8088
    targets = [
        RuntimeTarget(
            runtime_id="rt-alpha",
            base_url="http://127.0.0.1:9001",
            token="t",
            tenant_id="tenant-a",
            owner_id="owner-a",
            container_name="qwenpaw-hub-1-rt-alpha",
            state="running",
        ),
        RuntimeTarget(
            runtime_id="rt-beta",
            base_url="http://127.0.0.1:9002",
            token="t",
            tenant_id="tenant-b",
            owner_id="owner-b",
            container_name="qwenpaw-hub-1-rt-beta",
            state="stopped",
        ),
    ]
    selected, warnings = select_targets(targets)
    assert [item.runtime_id for item in selected] == ["rt-alpha"]
    assert any("not running" in item for item in warnings)

    selected, _ = select_targets(
        targets,
        include_stopped=True,
        owners=["owner-b"],
    )
    assert [item.runtime_id for item in selected] == ["rt-beta"]

    selected, _ = select_targets(
        targets,
        patterns=["qwenpaw-hub-1-rt-alpha"],
        include_stopped=True,
    )
    assert [item.runtime_id for item in selected] == ["rt-alpha"]

    selected, _ = select_targets(targets, patterns=["rt-*"], include_stopped=True)
    assert len(selected) == 2
