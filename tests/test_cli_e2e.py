# -*- coding: utf-8 -*-
"""End-to-end CLI runs against fake runtimes over real HTTP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from qpm.cli import main


def run(capsys, argv: Sequence[str]) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _backup(tmp_path: Path) -> list[str]:
    return ["--backup-dir", str(tmp_path / "backups")]


def test_install_dry_run_then_install(
    capsys,
    tmp_path: Path,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    first = runtime_factory("rt-1")
    second = runtime_factory("rt-2")
    inventory = inventory_file([first, second])
    bundle = plugin_factory(version="0.1.0")

    code, out, _ = run(
        capsys,
        ["install", str(bundle), "--inventory", str(inventory), "--dry-run"],
    )
    assert code == 0
    assert "would install" in out
    assert first.plugins == {} and second.plugins == {}

    code, out, _ = run(
        capsys,
        ["install", str(bundle), "--inventory", str(inventory), "--yes"],
    )
    assert code == 0
    assert "installed 0.1.0" in out
    for runtime in (first, second):
        assert runtime.plugins["offline-market"]["version"] == "0.1.0"
        assert (
            Path(runtime.host_plugins_dir) / "offline-market" / "plugin.json"
        ).is_file()

    # Idempotent: the second run plans a no-op.
    code, out, _ = run(
        capsys,
        ["install", str(bundle), "--inventory", str(inventory), "--yes"],
    )
    assert code == 0
    assert "already at the target version" in out
    uploads = [
        path
        for method, path in first.requests
        if method == "POST" and path == "/api/plugins/upload"
    ]
    assert len(uploads) == 1


def test_upgrade_and_downgrade_guards(
    capsys,
    tmp_path: Path,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    old = plugin_factory(version="0.1.0")
    new = plugin_factory(version="0.1.1")

    assert run(capsys, ["install", str(old), "--inventory", str(inventory), "--yes"])[0] == 0
    code, out, _ = run(
        capsys,
        [
            "upgrade",
            str(new),
            "--inventory",
            str(inventory),
            "--yes",
            *_backup(tmp_path),
        ],
    )
    assert code == 0
    assert "upgrade 0.1.0 -> 0.1.1" in out
    assert runtime.plugins["offline-market"]["version"] == "0.1.1"

    # Sync to an older bundle is refused without --allow-downgrade.
    code, out, _ = run(
        capsys,
        ["install", str(old), "--inventory", str(inventory), "--yes"],
    )
    assert code == 0
    assert "newer than the bundle" in out
    assert runtime.plugins["offline-market"]["version"] == "0.1.1"

    code, out, _ = run(
        capsys,
        [
            "install",
            str(old),
            "--inventory",
            str(inventory),
            "--yes",
            "--allow-downgrade",
            *_backup(tmp_path),
        ],
    )
    assert code == 0
    assert "downgrade 0.1.1 -> 0.1.0" in out
    assert runtime.plugins["offline-market"]["version"] == "0.1.0"


def test_install_mode_does_not_upgrade(
    capsys,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    old = plugin_factory(version="0.1.0")
    new = plugin_factory(version="0.1.1")
    run(capsys, ["install", str(old), "--inventory", str(inventory), "--yes"])
    code, out, _ = run(
        capsys,
        [
            "install",
            str(new),
            "--inventory",
            str(inventory),
            "--yes",
            "--mode",
            "install",
        ],
    )
    assert code == 0
    assert "install mode does not upgrade" in out
    assert runtime.plugins["offline-market"]["version"] == "0.1.0"


def test_uninstall_scoped_and_protected(
    capsys,
    tmp_path: Path,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    first = runtime_factory("rt-1")
    second = runtime_factory("rt-2")
    inventory = inventory_file([first, second])
    bundle = plugin_factory(version="0.1.0")
    run(capsys, ["install", str(bundle), "--inventory", str(inventory), "--yes"])

    code, out, _ = run(
        capsys,
        [
            "uninstall",
            "offline-market",
            "--inventory",
            str(inventory),
            "--yes",
            "--runtime",
            "rt-1",
            *_backup(tmp_path),
        ],
    )
    assert code == 0
    assert "offline-market" not in first.plugins
    assert "offline-market" in second.plugins

    code, out, _ = run(
        capsys,
        [
            "uninstall",
            "offline-market",
            "--inventory",
            str(inventory),
            "--yes",
            "--protect",
            "offline-*",
            *_backup(tmp_path),
        ],
    )
    assert code == 0
    assert "protected" in out
    assert "offline-market" in second.plugins

    # A glob removes it once protection is lifted.
    code, _, _ = run(
        capsys,
        [
            "uninstall",
            "offline-*",
            "--inventory",
            str(inventory),
            "--yes",
            *_backup(tmp_path),
        ],
    )
    assert code == 0
    assert "offline-market" not in second.plugins


def test_missing_plugin_uninstall_is_a_noop(
    capsys,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    plugin_factory(version="0.1.0")
    code, out, _ = run(
        capsys,
        ["uninstall", "ghost", "--inventory", str(inventory), "--yes"],
    )
    assert code == 0
    assert "not installed" in out


def test_requires_yes_when_stdin_is_not_a_tty(
    capsys,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    bundle = plugin_factory(version="0.1.0")
    code, _, err = run(
        capsys,
        ["install", str(bundle), "--inventory", str(inventory)],
    )
    assert code == 2
    assert "--yes" in err
    assert runtime.plugins == {}


def test_json_output_shape(
    capsys,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    first = runtime_factory("rt-1")
    second = runtime_factory("rt-2")
    inventory = inventory_file([first, second])
    bundle = plugin_factory(version="0.2.0")
    code, out, _ = run(
        capsys,
        [
            "install",
            str(bundle),
            "--inventory",
            str(inventory),
            "--yes",
            "--json",
        ],
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["command"] == "install"
    assert payload["summary"] == {
        "actions": 2,
        "runtimes": 2,
        "ok": 2,
        "skipped": 0,
        "planned": 0,
        "failed": 0,
        "rolled_back": 0,
    }
    assert payload["bundles"][0]["id"] == "offline-market"
    assert len(payload["bundles"][0]["sha256"]) == 64
    assert {result["runtime_id"] for result in payload["results"]} == {
        "rt-1",
        "rt-2",
    }


def test_unreachable_runtime_fails_the_run(
    capsys,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    runtime.stop()
    bundle = plugin_factory(version="0.1.0")
    code, out, err = run(
        capsys,
        ["install", str(bundle), "--inventory", str(inventory), "--yes"],
    )
    assert code == 1
    assert "inventory failed" in err
    assert "failed" in out


def test_rollback_on_verification_failure(
    capsys,
    tmp_path: Path,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    old = plugin_factory(version="0.1.0")
    new = plugin_factory(version="0.1.1")
    assert run(capsys, ["install", str(old), "--inventory", str(inventory), "--yes"])[0] == 0

    runtime.corrupt_next_upload_version = "9.9.9"
    code, out, _ = run(
        capsys,
        [
            "upgrade",
            str(new),
            "--inventory",
            str(inventory),
            "--yes",
            "--json",
            *_backup(tmp_path),
        ],
    )
    assert code == 1
    payload = json.loads(out)
    result = payload["results"][0]
    assert result["state"] == "rolled_back"
    assert "cannot restore" not in result["message"]
    assert runtime.plugins["offline-market"]["version"] == "0.1.0"
    manifest = json.loads(
        (
            Path(runtime.host_plugins_dir)
            / "offline-market"
            / "plugin.json"
        ).read_text(),
    )
    assert manifest["version"] == "0.1.0"


def test_rollback_on_delete_failure(
    capsys,
    tmp_path: Path,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    bundle = plugin_factory(version="0.1.0")
    run(capsys, ["install", str(bundle), "--inventory", str(inventory), "--yes"])

    runtime.fail_next_delete = 1
    code, out, _ = run(
        capsys,
        [
            "uninstall",
            "offline-market",
            "--inventory",
            str(inventory),
            "--yes",
            "--json",
            *_backup(tmp_path),
        ],
    )
    assert code == 1
    payload = json.loads(out)
    assert payload["results"][0]["state"] == "rolled_back"
    # Files were restored and the plugin reinstalled from the container path.
    assert runtime.plugins["offline-market"]["version"] == "0.1.0"


def test_enable_and_disable(
    capsys,
    tmp_path: Path,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    bundle = plugin_factory(version="0.1.0")
    run(
        capsys,
        ["install", str(bundle), "--inventory", str(inventory), "--yes"],
    )

    code, _, _ = run(
        capsys,
        [
            "disable",
            "offline-market",
            "--inventory",
            str(inventory),
            "--yes",
            *_backup(tmp_path),
        ],
    )
    assert code == 0
    assert runtime.plugins["offline-market"]["enabled"] is False

    code, _, _ = run(
        capsys,
        [
            "enable",
            "offline-market",
            "--inventory",
            str(inventory),
            "--yes",
            *_backup(tmp_path),
        ],
    )
    assert code == 0
    assert runtime.plugins["offline-market"]["enabled"] is True


def test_freeze_then_apply_converges(
    capsys,
    tmp_path: Path,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    first = runtime_factory("rt-1")
    second = runtime_factory("rt-2")
    inventory = inventory_file([first, second])
    old = plugin_factory(version="0.1.0")
    new = plugin_factory(version="0.1.1")
    run(capsys, ["install", str(old), "--inventory", str(inventory), "--yes"])

    state_path = tmp_path / "plugins.lock.json"
    code, _, _ = run(
        capsys,
        [
            "freeze",
            "--inventory",
            str(inventory),
            "--from-runtime",
            "rt-1",
            "-o",
            str(state_path),
        ],
    )
    assert code == 0
    payload = json.loads(state_path.read_text())
    assert payload["plugins"][0]["id"] == "offline-market"

    payload["plugins"][0]["version"] = "0.1.1"
    payload["plugins"][0]["source"] = str(new)
    state_path.write_text(json.dumps(payload))

    code, _, _ = run(
        capsys,
        [
            "apply",
            str(state_path),
            "--inventory",
            str(inventory),
            "--yes",
            *_backup(tmp_path),
        ],
    )
    assert code == 0
    assert first.plugins["offline-market"]["version"] == "0.1.1"
    assert second.plugins["offline-market"]["version"] == "0.1.1"

    # Without a source the entry becomes a drift check.
    payload["plugins"][0]["version"] = "9.9.9"
    payload["plugins"][0]["source"] = ""
    state_path.write_text(json.dumps(payload))
    code, out, _ = run(
        capsys,
        ["apply", str(state_path), "--inventory", str(inventory), "--yes"],
    )
    assert code == 1
    assert "version drift" in out


def test_plugins_inventory_and_filter(
    capsys,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    bundle = plugin_factory(version="0.1.0")
    run(capsys, ["install", str(bundle), "--inventory", str(inventory), "--yes"])

    code, out, _ = run(
        capsys,
        ["plugins", "--inventory", str(inventory), "--json"],
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["runtimes"][0]["plugins"][0]["id"] == "offline-market"

    code, out, _ = run(
        capsys,
        ["plugins", "--inventory", str(inventory), "--plugin", "nope-*"],
    )
    assert code == 0
    assert "offline-market" not in out


def test_runtimes_probe(
    capsys,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    code, out, _ = run(
        capsys,
        ["runtimes", "--inventory", str(inventory), "--probe", "--json"],
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["runtimes"][0]["qwenpaw_version"] == "2.2.2"


def test_doctor_reports_problems(
    capsys,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    code, out, _ = run(capsys, ["doctor", "--inventory", str(inventory)])
    assert code == 0
    assert "writable" in out

    runtime.healthy = False
    code, out, _ = run(capsys, ["doctor", "--inventory", str(inventory)])
    assert code == 1
    assert "starting" in out


def test_stopped_runtime_needs_include_stopped(
    capsys,
    tmp_path: Path,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    payload = json.loads(inventory.read_text())
    payload["runtimes"][0]["state"] = "stopped"
    inventory.write_text(json.dumps(payload))
    bundle = plugin_factory(version="0.1.0")

    code, _, err = run(
        capsys,
        ["install", str(bundle), "--inventory", str(inventory), "--yes"],
    )
    assert code == 2
    assert "no runtimes selected" in err

    code, out, _ = run(
        capsys,
        [
            "install",
            str(bundle),
            "--inventory",
            str(inventory),
            "--yes",
            "--include-stopped",
        ],
    )
    assert code == 0
    assert "not running" in out
    assert runtime.plugins == {}


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["nonsense"])
    assert excinfo.value.code == 2


def test_global_options_work_before_the_subcommand(
    capsys,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    bundle = plugin_factory(version="0.1.0")
    code, out, _ = run(
        capsys,
        ["--inventory", str(inventory), "install", str(bundle), "--yes"],
    )
    assert code == 0
    assert runtime.plugins["offline-market"]["version"] == "0.1.0"


def test_save_inventory_round_trip(
    capsys,
    tmp_path: Path,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    saved = tmp_path / "saved-inventory.json"
    code, _, err = run(
        capsys,
        [
            "runtimes",
            "--inventory",
            str(inventory),
            "--save-inventory",
            str(saved),
        ],
    )
    assert code == 0
    assert "boundary tokens" in err
    payload = json.loads(saved.read_text())
    assert payload["runtimes"][0]["url"] == runtime.url
    assert payload["runtimes"][0]["token"] == runtime.token
    assert saved.stat().st_mode & 0o777 == 0o600

    # The saved file is itself a usable inventory.
    code, _, _ = run(capsys, ["plugins", "--inventory", str(saved)])
    assert code == 0


def test_failure_without_host_copy_explains_missing_rollback(
    capsys,
    plugin_factory,
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    bundle = plugin_factory(version="0.1.0")
    run(capsys, ["install", str(bundle), "--inventory", str(inventory), "--yes"])

    # Without a host plugins_dir the manager cannot snapshot the plugin.
    payload = json.loads(inventory.read_text())
    payload["runtimes"][0].pop("plugins_dir", None)
    inventory.write_text(json.dumps(payload))

    runtime.fail_next_delete = 1
    code, out, _ = run(
        capsys,
        [
            "uninstall",
            "offline-market",
            "--inventory",
            str(inventory),
            "--yes",
            "--json",
        ],
    )
    assert code == 1
    result = json.loads(out)["results"][0]
    assert result["state"] == "failed"
    assert "no rollback available" in result["message"]
