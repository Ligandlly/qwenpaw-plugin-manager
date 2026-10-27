# -*- coding: utf-8 -*-
"""Terminal and JSON reporting."""

from __future__ import annotations

import json
import sys
import threading
import unicodedata
from typing import Iterable, Sequence

from . import logs
from .models import ActionResult, ActionState, RuntimeSnapshot

__all__ = ["Reporter", "display_width"]


def display_width(text: str) -> int:
    """Return the terminal cell width of ``text`` (CJK-aware)."""
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _pad(text: str, width: int) -> str:
    padding = max(0, width - display_width(text))
    return text + " " * padding


def _cell(value: object) -> str:
    """Flatten a value into one table-safe line."""
    text = str(value)
    return " ".join(text.replace("\t", " ").splitlines() or [""])


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    header_cells = [_cell(header) for header in headers]
    body = [[_cell(cell) for cell in row] for row in rows]
    widths = [display_width(header) for header in header_cells]
    for row in body:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], display_width(cell))
    lines = [
        "  ".join(
            _pad(header, widths[index])
            for index, header in enumerate(header_cells)
        ).rstrip(),
        "  ".join("-" * widths[index] for index in range(len(headers))),
    ]
    for row in body:
        lines.append(
            "  ".join(
                _pad(cell, widths[index]) for index, cell in enumerate(row)
            ).rstrip(),
        )
    return "\n".join(lines)


class Reporter:
    """Write progress to stderr and results to stdout."""

    def __init__(
        self,
        *,
        json_output: bool = False,
        quiet: bool = False,
        verbose: bool = False,
        stdout=None,
        stderr=None,
    ) -> None:
        self.json_output = json_output
        self.quiet = quiet
        self.verbose = verbose
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr
        self._lock = threading.Lock()

    # ── plain messages ───────────────────────────────────────────────────

    def _write(self, stream, message: str) -> None:
        with self._lock:
            print(message, file=stream, flush=True)

    def info(self, message: str) -> None:
        """Progress chatter: suppressed by --quiet and in JSON mode."""
        if self.quiet or self.json_output:
            return
        self._write(self._stderr, message)

    def debug(self, message: str) -> None:
        """Extra detail shown only with --verbose."""
        if not self.verbose or self.quiet or self.json_output:
            return
        logs.set_enabled(True)
        logs.debug(message)

    def warning(self, message: str) -> None:
        """Non-fatal problem, always visible on stderr."""
        if self.quiet and not self.json_output:
            return
        self._write(self._stderr, f"warning: {message}")

    def error(self, message: str) -> None:
        """Fatal problem, always visible on stderr."""
        self._write(self._stderr, f"error: {message}")

    # ── structured output ────────────────────────────────────────────────

    def out(self, message: str = "") -> None:
        """Write a line to stdout (tables, summaries)."""
        self._write(self._stdout, message)

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
        """Print an aligned table to stdout."""
        self.out(_render_table(headers, list(rows)))

    def emit_json(self, payload: object) -> None:
        """Print one JSON document to stdout."""
        with self._lock:
            print(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False),
                file=self._stdout,
                flush=True,
            )

    def result_line(self, result: ActionResult) -> None:
        """Print one action result as it completes (progress stream)."""
        action = result.action
        line = "  ".join(
            [
                _pad(result.state.value, 11),
                _pad(action.target.runtime_id, 20),
                _pad(action.kind.value, 10),
                _pad(action.plugin_id, 24),
                _pad(action.transition, 22),
            ],
        ).rstrip()
        if result.message:
            line = f"{line}  {result.message}"
        self.info(line)

    def print_snapshots(self, snapshots: list[RuntimeSnapshot]) -> None:
        """Render the inventory overview."""
        rows = []
        for snapshot in snapshots:
            rows.append(
                [
                    snapshot.target.runtime_id,
                    snapshot.target.tenant_id or "-",
                    snapshot.target.owner_id[-8:] if snapshot.target.owner_id else "-",
                    snapshot.target.state,
                    snapshot.app_version or "-",
                    str(len(snapshot.plugins)),
                    snapshot.error or "ok",
                ],
            )
        self.table(
            [
                "runtime",
                "tenant",
                "owner",
                "state",
                "qwenpaw",
                "plugins",
                "inventory",
            ],
            rows,
        )

    def print_plugin_matrix(self, snapshots: list[RuntimeSnapshot]) -> None:
        """Render runtime x plugin versions."""
        rows = []
        for snapshot in snapshots:
            if not snapshot.plugins:
                rows.append([snapshot.target.runtime_id, "-", "-", "-", "-"])
                continue
            for plugin in sorted(
                snapshot.plugins.values(),
                key=lambda item: item.plugin_id,
            ):
                rows.append(
                    [
                        snapshot.target.runtime_id,
                        plugin.plugin_id,
                        plugin.version,
                        "enabled" if plugin.enabled else "disabled",
                        plugin.status or ("loaded" if plugin.loaded else "not-loaded"),
                    ],
                )
        self.table(
            ["runtime", "plugin", "version", "enabled", "status"],
            rows,
        )

    def print_results(self, results: list[ActionResult]) -> None:
        """Render the final per-action table plus a summary."""
        rows = []
        for result in results:
            action = result.action
            rows.append(
                [
                    result.state.value,
                    action.target.runtime_id,
                    action.kind.value,
                    action.plugin_id,
                    action.transition,
                    _format_duration(result.duration_ms),
                    result.message or action.reason,
                ],
            )
        self.table(
            [
                "result",
                "runtime",
                "action",
                "plugin",
                "version",
                "time",
                "detail",
            ],
            rows,
        )
        self.print_summary(results)

    def print_summary(self, results: list[ActionResult]) -> None:
        """Print the one-line summary and the failure list."""
        counts: dict[str, int] = {}
        for result in results:
            counts[result.state.value] = counts.get(result.state.value, 0) + 1
        runtimes = {result.action.target.key for result in results}
        parts = [
            f"{count} {state}"
            for state, count in sorted(counts.items())
        ] or ["nothing to do"]
        self.out("")
        self.out(
            f"summary: {', '.join(parts)} "
            f"({len(results)} action(s), {len(runtimes)} runtime(s))",
        )
        failures = [
            result for result in results if result.state == ActionState.FAILED
        ]
        if failures:
            self.out("")
            self.out("failures:")
            for result in failures:
                self.out(
                    f"  - {result.action.target.runtime_id}/"
                    f"{result.action.plugin_id}: {result.message}",
                )
        rolled = [
            result
            for result in results
            if result.state == ActionState.ROLLED_BACK
        ]
        if rolled:
            self.out("")
            self.out("rolled back:")
            for result in rolled:
                self.out(
                    f"  - {result.action.target.runtime_id}/"
                    f"{result.action.plugin_id}: {result.rollback}",
                )


def _format_duration(milliseconds: int) -> str:
    if milliseconds <= 0:
        return "-"
    if milliseconds < 1000:
        return f"{milliseconds}ms"
    return f"{milliseconds / 1000:.1f}s"
