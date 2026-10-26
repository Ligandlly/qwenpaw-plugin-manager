# -*- coding: utf-8 -*-
"""Plan decisions: install, upgrade, downgrade, skip, toggle, uninstall."""

from __future__ import annotations

import pytest

from qpm.models import (
    ActionKind,
    InstalledPlugin,
    PluginBundle,
    RuntimeSnapshot,
    RuntimeTarget,
)
from qpm.planner import (
    SyncOptions,
    plan_bundle,
    plan_check,
    plan_toggle,
    plan_uninstall,
)

TARGET = RuntimeTarget(
    runtime_id="rt-1",
    base_url="http://127.0.0.1:9001",
    token="t",
    tenant_id="tenant-a",
    state="running",
)


def bundle(
    plugin_id: str = "demo",
    version: str = "1.0.0",
    *,
    sdk: str = "1.0.0",
    minimum: str = "2.2.2",
    maximum: str = "",
) -> PluginBundle:
    return PluginBundle(
        plugin_id=plugin_id,
        version=version,
        data=b"zip",
        origin="test",
        sdk_version=sdk,
        qwenpaw_min=minimum,
        qwenpaw_max=maximum,
    )


def snapshot(
    *plugins: InstalledPlugin,
    version: str | None = "2.2.2",
    target: RuntimeTarget = TARGET,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        target=target,
        app_version=version,
        plugins={plugin.plugin_id: plugin for plugin in plugins},
    )


def installed(
    plugin_id: str = "demo",
    version: str = "1.0.0",
    *,
    enabled: bool = True,
) -> InstalledPlugin:
    return InstalledPlugin(
        plugin_id=plugin_id,
        version=version,
        enabled=enabled,
        loaded=enabled,
    )


def test_install_when_missing() -> None:
    actions = plan_bundle(snapshot(), bundle(), SyncOptions())
    assert [action.kind for action in actions] == [ActionKind.INSTALL]
    assert actions[0].force is False


def test_same_version_is_skipped() -> None:
    actions = plan_bundle(
        snapshot(installed()),
        bundle(),
        SyncOptions(),
    )
    assert [action.kind for action in actions] == [ActionKind.SKIP]
    assert "already at the target version" in actions[0].reason


def test_reinstall_flag_forces_upload() -> None:
    actions = plan_bundle(
        snapshot(installed()),
        bundle(),
        SyncOptions(reinstall=True),
    )
    assert actions[0].kind == ActionKind.REINSTALL
    assert actions[0].force is True


def test_upgrade_when_bundle_is_newer() -> None:
    actions = plan_bundle(
        snapshot(installed(version="0.9.0")),
        bundle(version="1.0.0"),
        SyncOptions(),
    )
    assert actions[0].kind == ActionKind.UPGRADE
    assert actions[0].force is True
    assert actions[0].current_version == "0.9.0"


def test_install_mode_does_not_upgrade() -> None:
    actions = plan_bundle(
        snapshot(installed(version="0.9.0")),
        bundle(version="1.0.0"),
        SyncOptions(mode="install"),
    )
    assert actions[0].kind == ActionKind.SKIP


def test_upgrade_mode_respects_install_missing() -> None:
    skipped = plan_bundle(
        snapshot(),
        bundle(),
        SyncOptions(mode="upgrade", install_missing=False),
    )
    assert skipped[0].kind == ActionKind.SKIP
    assert "--install-missing" in skipped[0].reason

    installed_missing = plan_bundle(
        snapshot(),
        bundle(),
        SyncOptions(mode="upgrade", install_missing=True),
    )
    assert installed_missing[0].kind == ActionKind.INSTALL


def test_downgrade_requires_explicit_flag() -> None:
    blocked = plan_bundle(
        snapshot(installed(version="2.0.0")),
        bundle(version="1.0.0"),
        SyncOptions(),
    )
    assert blocked[0].kind == ActionKind.SKIP
    assert "--allow-downgrade" in blocked[0].reason

    allowed = plan_bundle(
        snapshot(installed(version="2.0.0")),
        bundle(version="1.0.0"),
        SyncOptions(allow_downgrade=True),
    )
    assert allowed[0].kind == ActionKind.DOWNGRADE
    assert allowed[0].force is True


def test_protected_plugin_is_never_touched() -> None:
    actions = plan_bundle(
        snapshot(installed()),
        bundle(),
        SyncOptions(protected=frozenset({"demo"}), reinstall=True),
    )
    assert actions[0].kind == ActionKind.SKIP
    assert "protected" in actions[0].reason


def test_plugin_pattern_filter() -> None:
    options = SyncOptions(plugin_patterns=("offline-*",))
    assert plan_bundle(snapshot(), bundle("other"), options) == []
    assert plan_bundle(snapshot(), bundle("offline-market"), options)


def test_incompatible_sdk_is_reported_as_skip() -> None:
    actions = plan_bundle(snapshot(), bundle(sdk="2.0.0"), SyncOptions())
    assert actions[0].kind == ActionKind.SKIP
    assert "SDK" in actions[0].reason


def test_incompatible_qwenpaw_is_reported_as_skip() -> None:
    actions = plan_bundle(
        snapshot(version="2.1.0"),
        bundle(minimum="2.2.2"),
        SyncOptions(),
    )
    assert actions[0].kind == ActionKind.SKIP
    assert "requires QwenPaw" in actions[0].reason


def test_disabled_runtime_is_skipped() -> None:
    stopped = RuntimeTarget(
        runtime_id="rt-2",
        base_url="http://127.0.0.1:9002",
        token="t",
        state="stopped",
    )
    actions = plan_bundle(snapshot(target=stopped), bundle(), SyncOptions())
    assert actions[0].kind == ActionKind.SKIP
    assert "not running" in actions[0].reason


def test_enable_after_install() -> None:
    actions = plan_bundle(snapshot(), bundle(), SyncOptions(enable=True))
    assert [action.kind for action in actions] == [
        ActionKind.INSTALL,
        ActionKind.ENABLE,
    ]
    assert actions[1].depends_on == "demo"


def test_enable_when_already_enabled_is_not_duplicated() -> None:
    actions = plan_bundle(
        snapshot(installed(enabled=True)),
        bundle(),
        SyncOptions(enable=True),
    )
    assert [action.kind for action in actions] == [ActionKind.SKIP]


def test_disable_when_enabled() -> None:
    actions = plan_bundle(
        snapshot(installed(enabled=True)),
        bundle(),
        SyncOptions(enable=False),
    )
    assert [action.kind for action in actions] == [
        ActionKind.SKIP,
        ActionKind.DISABLE,
    ]


def test_uninstall_plan() -> None:
    state = snapshot(installed(), installed("other"))
    actions = plan_uninstall(state, ["demo", "missing", "other"])
    kinds = [action.kind for action in actions]
    assert kinds == [ActionKind.UNINSTALL, ActionKind.SKIP, ActionKind.UNINSTALL]
    assert actions[1].reason == "not installed"

    protected = plan_uninstall(
        state,
        ["demo"],
        protected=frozenset({"demo"}),
    )
    assert protected[0].kind == ActionKind.SKIP

    keep = plan_uninstall(state, ["demo"], keep_files=True)
    assert keep[0].kind == ActionKind.UNLOAD


def test_toggle_plan() -> None:
    state = snapshot(installed(enabled=True), installed("off", enabled=False))
    actions = plan_toggle(state, ["demo", "off"], enabled=False)
    assert [action.kind for action in actions] == [
        ActionKind.DISABLE,
        ActionKind.SKIP,
    ]
    assert actions[1].reason == "already disabled"


def test_check_plan_without_source() -> None:
    same = plan_check(snapshot(installed()), "demo", "1.0.0")
    assert same.kind == ActionKind.SKIP

    missing = plan_check(snapshot(), "demo", "1.0.0", optional=True)
    assert missing.kind == ActionKind.CHECK
    assert missing.optional is True

    drift = plan_check(snapshot(installed(version="0.1.0")), "demo", "1.0.0")
    assert drift.kind == ActionKind.CHECK
    assert "version drift" in drift.reason


def test_sync_options_reject_unknown_mode() -> None:
    with pytest.raises(ValueError):
        SyncOptions(mode="whatever")


def test_enable_is_not_planned_when_upgrade_mode_skips_install() -> None:
    actions = plan_bundle(
        snapshot(),
        bundle(),
        SyncOptions(mode="upgrade", install_missing=False, enable=True),
    )
    assert [action.kind for action in actions] == [ActionKind.SKIP]
