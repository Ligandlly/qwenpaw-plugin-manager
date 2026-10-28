# -*- coding: utf-8 -*-
"""Command line interface for the QwenPaw fleet plugin manager."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, logs
from .commands import (
    cmd_apply,
    cmd_doctor,
    cmd_freeze,
    cmd_install,
    cmd_plugins,
    cmd_runtimes,
    cmd_serve,
    cmd_toggle,
    cmd_uninstall,
)
from .errors import QpmError, UsageError
from .report import Reporter

__all__ = ["build_parser", "main"]

_ENV = os.environ

#: Defaults for the options shared by every subcommand.  They use
#: ``argparse.SUPPRESS`` in the parser so that ``qpm --inventory x plugins``
#: is not silently overwritten by the subparser's defaults; ``main`` fills
#: whatever the user did not provide.
_COMMON_DEFAULTS: dict[str, object] = {
    "inventory": None,
    "docker_bin": _ENV.get("QPM_DOCKER_BIN", "docker"),
    "docker_instance": _ENV.get("QPM_DOCKER_INSTANCE", ""),
    "container_port": int(_ENV.get("QPM_CONTAINER_PORT", "8088") or 8088),
    "hub_db": None,
    "runtime": [],
    "tenant": [],
    "owner": [],
    "include_stopped": False,
    "timeout": float(_ENV.get("QPM_TIMEOUT", "30") or 30),
    "operation_timeout": float(_ENV.get("QPM_OPERATION_TIMEOUT", "180") or 180),
    "retries": int(_ENV.get("QPM_RETRIES", "2") or 2),
    "concurrency": int(_ENV.get("QPM_CONCURRENCY", "4") or 4),
    "json": False,
    "quiet": False,
    "verbose": False,
    "dry_run": False,
    "yes": False,
    "fail_fast": False,
}

_DESCRIPTION = """\
Manage QwenPaw plugins across every runtime a QwenPaw Hub started.

Runtimes are discovered from the Docker containers the Hub labels with
io.qwenpaw.hub.managed=true, authenticated with the same boundary token the
Hub injects, and driven through the runtime's native plugin API. Nothing is
installed twice: every action is planned against the live inventory, printed
for review, verified after the fact, and rolled back from a host-side snapshot
when verification fails.

Examples:
  qpm runtimes --probe
  qpm plugins --plugin 'offline-*'
  qpm install ./dist/offline-market-0.1.3.zip --dry-run
  qpm install ./dist/offline-market-0.1.3.zip --yes
  qpm upgrade ./dist/*.zip --install-missing --yes
  qpm uninstall offline-market --yes
  qpm enable offline-market --yes
  qpm freeze --from-runtime rt-a -o plugins.lock.json
  qpm apply plugins.lock.json --yes
  qpm doctor
  qpm serve --open          # browser console on 127.0.0.1:8787
"""


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    group = parser.add_argument_group("discovery")
    group.add_argument(
        "--inventory",
        type=Path,
        default=argparse.SUPPRESS,
        metavar="FILE",
        help="read runtimes from a JSON inventory instead of Docker",
    )
    group.add_argument(
        "--docker-bin",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="docker CLI to use (default: docker)",
    )
    group.add_argument(
        "--docker-instance",
        default=argparse.SUPPRESS,
        metavar="ID",
        help="only containers of this Hub instance fingerprint",
    )
    group.add_argument(
        "--container-port",
        type=int,
        default=argparse.SUPPRESS,
        metavar="PORT",
        help="runtime API port inside the container (default: 8088)",
    )
    group.add_argument(
        "--hub-db",
        type=Path,
        default=argparse.SUPPRESS,
        metavar="FILE",
        help=(
            "Hub control.db used to show owner usernames in the console "
            "(default: auto-detect; only for display)"
        ),
    )
    filters = parser.add_argument_group("filters")
    filters.add_argument(
        "--runtime",
        action="append",
        default=argparse.SUPPRESS,
        metavar="GLOB",
        help="runtime id or container name glob (repeatable)",
    )
    filters.add_argument(
        "--tenant",
        action="append",
        default=argparse.SUPPRESS,
        metavar="GLOB",
        help="tenant id glob (repeatable)",
    )
    filters.add_argument(
        "--owner",
        action="append",
        default=argparse.SUPPRESS,
        metavar="GLOB",
        help="hub user id glob (repeatable)",
    )
    filters.add_argument(
        "--include-stopped",
        action="store_true",
        default=argparse.SUPPRESS,
        help="include runtimes that are not running (their API calls fail)",
    )
    limits = parser.add_argument_group("transport")
    limits.add_argument(
        "--timeout",
        type=float,
        default=argparse.SUPPRESS,
        metavar="SECONDS",
        help="per-request timeout for read calls (default: 30)",
    )
    limits.add_argument(
        "--operation-timeout",
        type=float,
        default=argparse.SUPPRESS,
        metavar="SECONDS",
        help="per-request timeout for install/uninstall calls (default: 180)",
    )
    limits.add_argument(
        "--retries",
        type=int,
        default=argparse.SUPPRESS,
        metavar="N",
        help="retries for transient failures (default: 2)",
    )
    limits.add_argument(
        "--concurrency",
        type=int,
        default=argparse.SUPPRESS,
        metavar="N",
        help="runtimes handled in parallel (default: 4)",
    )
    output = parser.add_argument_group("output")
    output.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit JSON results",
    )
    output.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="only print the final table and errors",
    )
    output.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "print debug detail (discovery, HTTP calls, planning) to stderr"
        ),
    )
    safety = parser.add_argument_group("safety")
    safety.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the plan without changing anything",
    )
    safety.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=argparse.SUPPRESS,
        help="do not ask for confirmation (required when stdin is not a TTY)",
    )
    safety.add_argument(
        "--fail-fast",
        action="store_true",
        default=argparse.SUPPRESS,
        help="stop handling a runtime after its first failure",
    )
    return parser


def _exec_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    group = parser.add_argument_group("rollback")
    group.add_argument(
        "--no-backup",
        action="store_true",
        help="do not snapshot the plugin directory before replacing it",
    )
    group.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="where to store snapshots (default: ~/.qpm/backups)",
    )
    group.add_argument(
        "--no-rollback",
        action="store_true",
        help="do not restore the snapshot when an action fails",
    )
    group.add_argument(
        "--no-verify",
        action="store_true",
        help="skip reading the plugin back after a change",
    )
    group.add_argument(
        "--interrupt-tasks",
        action="store_true",
        help="let the runtime interrupt running tasks that hold the plugin",
    )
    group.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="ID[,ID]",
        help="never touch these plugin ids or globs (repeatable)",
    )
    return parser


def _add_sync_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        choices=("sync", "install", "upgrade"),
        default="sync",
        help=(
            "sync: converge versions; install: only fill gaps; upgrade: only "
            "upgrade installed plugins (default: sync)"
        ),
    )
    parser.add_argument(
        "--reinstall",
        action="store_true",
        help="re-upload bundles that are already at the target version",
    )
    parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="allow replacing a newer installed version with an older bundle",
    )
    parser.add_argument(
        "--install-missing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="in upgrade mode, also install plugins that are absent",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        metavar="GLOB",
        help="only act on bundle/plugin ids matching this glob (repeatable)",
    )
    parser.add_argument(
        "--host-sdk",
        default=_ENV.get("QPM_HOST_SDK_VERSION", "1.0.0"),
        metavar="VERSION",
        help="plugin SDK version the runtimes provide (default: 1.0.0)",
    )
    toggle = parser.add_mutually_exclusive_group()
    toggle.add_argument(
        "--enable",
        dest="enable",
        action="store_true",
        default=None,
        help="ensure the plugins are enabled after syncing",
    )
    toggle.add_argument(
        "--disable",
        dest="enable",
        action="store_false",
        help="ensure the plugins are disabled after syncing",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the full argparse tree."""
    common = _common_parser()
    exec_common = _exec_parser()
    parser = argparse.ArgumentParser(
        prog="qpm",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"qwenpaw-plugin-manager {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    runtimes = subparsers.add_parser(
        "runtimes",
        parents=[common],
        help="list discovered runtimes",
        description="List the Hub runtimes this tool can reach.",
    )
    runtimes.add_argument(
        "--probe",
        action="store_true",
        help="also call each runtime API for its version and plugin count",
    )
    runtimes.add_argument(
        "--save-inventory",
        type=Path,
        metavar="FILE",
        help=(
            "write the discovered runtimes to a JSON inventory file "
            "(includes boundary tokens; chmod 600)"
        ),
    )
    runtimes.set_defaults(handler=cmd_runtimes)

    plugins = subparsers.add_parser(
        "plugins",
        parents=[common],
        help="show the plugin inventory",
        description="Show installed plugins per runtime.",
    )
    plugins.add_argument(
        "--plugin",
        action="append",
        default=[],
        metavar="GLOB",
        help="only show plugin ids matching this glob (repeatable)",
    )
    plugins.set_defaults(handler=cmd_plugins)

    install = subparsers.add_parser(
        "install",
        parents=[common, exec_common],
        help="install/upgrade plugins from directories, zips, or URLs",
        description=(
            "Install or upgrade every bundle in SOURCE onto the selected "
            "runtimes. SOURCE may be a plugin directory, a .zip, a URL, or a "
            "directory containing several plugins/zips."
        ),
    )
    install.add_argument("source", nargs="+", metavar="SOURCE")
    _add_sync_arguments(install)
    install.set_defaults(handler=cmd_install)

    upgrade = subparsers.add_parser(
        "upgrade",
        parents=[common, exec_common],
        help="upgrade installed plugins only",
        description=(
            "Upgrade only plugins that are already installed and older than "
            "the given bundles. Absent plugins are skipped unless "
            "--install-missing is set."
        ),
    )
    upgrade.add_argument("source", nargs="+", metavar="SOURCE")
    _add_sync_arguments(upgrade)
    upgrade.set_defaults(
        handler=cmd_install,
        mode="upgrade",
        install_missing=False,
    )

    uninstall = subparsers.add_parser(
        "uninstall",
        parents=[common, exec_common],
        help="uninstall plugins from the selected runtimes",
        description="Uninstall (or --keep-files: unload) plugin ids or globs.",
    )
    uninstall.add_argument("plugins", nargs="+", metavar="PLUGIN")
    uninstall.add_argument(
        "--keep-files",
        action="store_true",
        help="unload but keep files and configuration on the runtime",
    )
    uninstall.set_defaults(handler=cmd_uninstall)

    for name, handler, help_text in (
        ("enable", cmd_toggle, "enable plugins on the selected runtimes"),
        ("disable", cmd_toggle, "disable plugins on the selected runtimes"),
    ):
        toggle = subparsers.add_parser(
            name,
            parents=[common, exec_common],
            help=help_text,
            description=help_text.capitalize() + ".",
        )
        toggle.add_argument("plugins", nargs="+", metavar="PLUGIN")
        toggle.set_defaults(
            handler=handler,
            _toggle_enabled=(name == "enable"),
        )

    apply_cmd = subparsers.add_parser(
        "apply",
        parents=[common, exec_common],
        help="converge runtimes to a declarative state file",
        description=(
            "Converge every selected runtime to STATE_FILE. Entries with a "
            "'source' are installed/upgraded; entries with only a 'version' "
            "are drift-checked."
        ),
    )
    apply_cmd.add_argument("state_file", metavar="STATE_FILE")
    _add_sync_arguments(apply_cmd)
    apply_cmd.set_defaults(handler=cmd_apply, mode="sync")

    freeze = subparsers.add_parser(
        "freeze",
        parents=[common],
        help="write a state file from one runtime's plugins",
        description=(
            "Snapshot one runtime's plugin set into a state file. Fill in the "
            "'source' fields, then converge the fleet with 'qpm apply'."
        ),
    )
    freeze.add_argument(
        "--from-runtime",
        action="append",
        default=[],
        metavar="GLOB",
        help="runtime id/container glob to freeze (defaults to --runtime)",
    )
    freeze.add_argument(
        "-o",
        "--output",
        default="plugins.lock.json",
        metavar="FILE",
        help="output file, or '-' for stdout (default: plugins.lock.json)",
    )
    freeze.set_defaults(handler=cmd_freeze)

    serve = subparsers.add_parser(
        "serve",
        parents=[common, exec_common],
        help="run the local web console",
        description=(
            "Serve a web console over the same discovery, planning, and "
            "execution code as the CLI. Every /api call needs the printed "
            "token unless --no-token is set (which also listens on all "
            "interfaces)."
        ),
    )
    serve.add_argument(
        "--host",
        default=None,
        metavar="HOST",
        help=(
            "bind address (default: 127.0.0.1, or 0.0.0.0 with --no-token)"
        ),
    )
    serve.add_argument(
        "--port",
        type=int,
        default=8787,
        metavar="PORT",
        help="bind port, 0 picks a free one (default: 8787)",
    )
    serve.add_argument(
        "--no-token",
        action="store_true",
        help=(
            "disable the console token and listen on all interfaces "
            "(0.0.0.0) unless --host says otherwise"
        ),
    )
    serve.add_argument(
        "--open",
        action="store_true",
        help="open the console URL in the default browser",
    )
    serve.add_argument(
        "--host-sdk",
        default=_ENV.get("QPM_HOST_SDK_VERSION", "1.0.0"),
        metavar="VERSION",
        help="plugin SDK version the runtimes provide (default: 1.0.0)",
    )
    serve.set_defaults(handler=cmd_serve)

    doctor = subparsers.add_parser(
        "doctor",
        parents=[common],
        help="check discovery, tokens, and rollback capability",
        description=(
            "Verify Docker discovery, the runtime boundary token, the plugin "
            "API, and whether host-side snapshots are possible."
        ),
    )
    doctor.set_defaults(handler=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, value in _COMMON_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    # -q wins over -v: quiet promises "only the final table and errors".
    logs.set_enabled(
        bool(getattr(args, "verbose", False))
        and not bool(getattr(args, "quiet", False)),
    )
    logs.debug(
        "qpm %s",
        " ".join(argv if argv is not None else sys.argv[1:]),
    )
    if not getattr(args, "handler", None):
        parser.print_help()
        return 0
    handler = args.handler
    try:
        if handler is cmd_toggle:
            return int(handler(args, enabled=bool(args._toggle_enabled)))
        return int(handler(args))
    except UsageError as exc:
        Reporter().error(str(exc))
        parser.print_usage(sys.stderr)
        return exc.exit_code
    except QpmError as exc:
        Reporter().error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        Reporter().error("interrupted")
        return 130
    except BrokenPipeError:  # pragma: no cover - depends on the terminal
        return 0


def entrypoint() -> None:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
