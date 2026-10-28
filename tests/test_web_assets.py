# -*- coding: utf-8 -*-
"""Static checks for the console assets (CSP compatibility, no dead paths)."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "qpm" / "web"


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_assets_exist() -> None:
    for name in ("index.html", "app.js", "styles.css", "favicon.svg"):
        assert (WEB / name).is_file(), name


def test_html_loads_only_external_assets() -> None:
    html = _read("index.html")
    assert "<script>" not in html.lower()
    assert re.search(r"<script[^>]*src=\"/app\.js\"", html)
    assert 'href="/styles.css"' in html
    assert 'href="/favicon.svg"' in html
    # Strict CSP forbids inline style attributes and inline handlers.
    assert "style=" not in html.lower()
    assert not re.search(r"\son[a-z]+=", html)
    assert "javascript:" not in html.lower()


def test_javascript_avoids_html_sinks() -> None:
    script = _read("app.js")
    assert "innerHTML" not in script
    assert "outerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script


def test_console_displays_the_owner_not_the_runtime_id() -> None:
    script = _read("app.js")
    # The unit/target labels prefer the username ...
    assert "runtime.username || runtime.runtime_id" in script
    # ... while selection, filters and API payloads keep the runtime id.
    assert "state.selected.has(runtime.runtime_id)" in script
    assert "runtime: [runtime.runtime_id]" in script


def test_styles_use_the_documented_tokens() -> None:
    css = _read("styles.css")
    for token in ("--ink", "--paper", "--signal", "--live", "--alarm", "--mono"):
        assert token in css
    # The console ships with the browser's dark form controls on purpose.
    assert "color-scheme: dark" in css


def test_every_static_route_has_a_file() -> None:
    from qpm.webapi import _STATIC_FILES

    for path, (filename, content_type) in _STATIC_FILES.items():
        assert (WEB / filename).is_file(), path
        assert content_type
