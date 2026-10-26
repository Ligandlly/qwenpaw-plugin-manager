# -*- coding: utf-8 -*-
"""Declarative state files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qpm.errors import UsageError
from qpm.models import InstalledPlugin, RuntimeSnapshot, RuntimeTarget
from qpm.state import freeze_payload, load_state, write_state


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload))
    return path


def test_load_state_full(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "schema_version": 1,
            "plugins": [
                {
                    "id": "demo",
                    "version": "1.0.0",
                    "source": "./demo",
                    "enabled": False,
                    "runtimes": ["rt-1"],
                    "optional": True,
                },
                {"plugin_id": "other"},
            ],
            "removed": ["legacy"],
        },
    )
    state = load_state(path)
    assert [plugin.plugin_id for plugin in state.plugins] == ["demo", "other"]
    assert state.plugins[0].enabled is False
    assert state.plugins[0].runtimes == ("rt-1",)
    assert state.plugins[0].optional is True
    assert state.plugins[1].version == ""
    assert state.removed == ("legacy",)


@pytest.mark.parametrize(
    "payload",
    [
        {"plugins": [{"version": "1.0.0"}]},
        {"plugins": [{"id": "a"}, {"id": "a"}]},
        {"plugins": [{"id": "a"}], "removed": ["a"]},
        {"schema_version": 2, "plugins": []},
        {"plugins": [{"id": "a", "enabled": "yes"}]},
        {"plugins": [{"id": "a", "runtimes": 1}]},
    ],
)
def test_load_state_rejects_bad_files(tmp_path: Path, payload: object) -> None:
    with pytest.raises(UsageError):
        load_state(_write(tmp_path, payload))


def test_freeze_payload_shape() -> None:
    target = RuntimeTarget(
        runtime_id="rt-1",
        base_url="http://127.0.0.1:9001",
        token="t",
        tenant_id="tenant-a",
    )
    snapshot = RuntimeSnapshot(
        target=target,
        app_version="2.2.2",
        plugins={
            "demo": InstalledPlugin(
                plugin_id="demo",
                version="1.0.0",
                enabled=True,
                loaded=True,
            ),
            "another": InstalledPlugin(
                plugin_id="another",
                version="2.0.0",
                enabled=False,
                loaded=False,
            ),
        },
    )
    payload = freeze_payload(snapshot)
    assert payload["schema_version"] == 1
    assert payload["generated_from"]["runtime_id"] == "rt-1"
    assert [item["id"] for item in payload["plugins"]] == ["another", "demo"]
    assert payload["plugins"][1] == {
        "id": "demo",
        "version": "1.0.0",
        "enabled": True,
        "source": "",
    }


def test_write_state_creates_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    write_state(path, {"schema_version": 1, "plugins": []})
    assert json.loads(path.read_text()) == {"schema_version": 1, "plugins": []}
