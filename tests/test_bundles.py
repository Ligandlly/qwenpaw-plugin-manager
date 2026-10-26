# -*- coding: utf-8 -*-
"""Bundle loading: directories, ZIPs, URLs, and hostile archives."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from qpm.bundles import (
    build_bundle_from_dir,
    bundle_from_zip_bytes,
    load_bundle_source,
)
from qpm.errors import SourceError


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _manifest(plugin_id: str = "demo", version: str = "1.0.0") -> bytes:
    return json.dumps(
        {
            "id": plugin_id,
            "name": "Demo",
            "version": version,
            "type": "hub",
            "sdk": {"version": "1.0.0", "capabilities": []},
            "qwenpaw_version": {"min": "2.2.2"},
        },
    ).encode()


def test_build_from_dir_is_reproducible_and_prefixed(plugin_factory) -> None:
    directory = plugin_factory("demo", version="1.2.3")
    (directory / "__pycache__").mkdir()
    (directory / "__pycache__" / "junk.pyc").write_bytes(b"junk")
    (directory / "notes.log").write_text("noise")
    (directory / "sub").mkdir()
    (directory / "sub" / "helper.py").write_text("X = 1\n")

    first = build_bundle_from_dir(directory)
    second = build_bundle_from_dir(directory)

    assert first.plugin_id == "demo"
    assert first.version == "1.2.3"
    assert first.sha256 == second.sha256
    assert first.data == second.data
    with zipfile.ZipFile(io.BytesIO(first.data)) as archive:
        names = set(archive.namelist())
    assert names == {
        "demo/plugin.json",
        "demo/backend.py",
        "demo/sub/helper.py",
    }


def test_build_from_dir_rejects_symlink(plugin_factory, tmp_path: Path) -> None:
    directory = plugin_factory("demo")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (directory / "link.txt").symlink_to(outside)
    with pytest.raises(SourceError):
        build_bundle_from_dir(directory)


def test_zip_with_manifest_at_root() -> None:
    bundle = bundle_from_zip_bytes(
        _zip_bytes({"plugin.json": _manifest(), "backend.py": b"x"}),
        origin="root.zip",
    )
    assert bundle.plugin_id == "demo"


def test_zip_with_single_top_level_directory() -> None:
    bundle = bundle_from_zip_bytes(
        _zip_bytes({"demo/plugin.json": _manifest(), "demo/a.py": b"x"}),
        origin="wrapped.zip",
    )
    assert bundle.plugin_id == "demo"


@pytest.mark.parametrize(
    "members",
    [
        {"a/plugin.json": _manifest("a"), "b/plugin.json": _manifest("b")},
        {"a/b/plugin.json": _manifest()},
        {"../plugin.json": _manifest()},
        {"/abs/plugin.json": _manifest()},
        {"demo/plugin.json": b"not json"},
        {"demo/plugin.json": json.dumps({"id": "demo"}).encode()},
        {"demo/plugin.json": json.dumps(
            {"id": "bad id", "version": "1.0.0"},
        ).encode()},
    ],
)
def test_zip_rejections(members: dict[str, bytes]) -> None:
    with pytest.raises(SourceError):
        bundle_from_zip_bytes(_zip_bytes(members), origin="bad.zip")


def test_zip_size_limit() -> None:
    payload = _zip_bytes({"plugin.json": _manifest()})
    with pytest.raises(SourceError):
        bundle_from_zip_bytes(payload, origin="big.zip", max_bytes=10)


def test_load_bundle_source_directory_of_plugins(
    plugin_factory,
    tmp_path: Path,
) -> None:
    plugin_factory("one", directory=tmp_path / "dist" / "one")
    plugin_factory("two", directory=tmp_path / "dist" / "two")
    (tmp_path / "dist" / "README.md").write_text("ignored")
    bundles = load_bundle_source(str(tmp_path / "dist"))
    assert sorted(bundle.plugin_id for bundle in bundles) == ["one", "two"]


def test_load_bundle_source_duplicate_ids(
    plugin_factory,
    tmp_path: Path,
) -> None:
    plugin_factory("dup", version="1.0.0", directory=tmp_path / "dist" / "a")
    plugin_factory("dup", version="2.0.0", directory=tmp_path / "dist" / "b")
    with pytest.raises(SourceError, match="provide plugin"):
        load_bundle_source(str(tmp_path / "dist"))


def test_load_bundle_source_rejects_unknown_file(tmp_path: Path) -> None:
    target = tmp_path / "plugin.tar.gz"
    target.write_bytes(b"not a zip")
    with pytest.raises(SourceError):
        load_bundle_source(str(target))


def test_load_bundle_source_missing_path() -> None:
    with pytest.raises(SourceError):
        load_bundle_source("/nonexistent/path/plugin")


def test_load_bundle_source_url(file_server, tmp_path: Path) -> None:
    payload = _zip_bytes({"plugin.json": _manifest("remote")})
    (tmp_path / "remote.zip").write_bytes(payload)
    bundles = load_bundle_source(
        f"{file_server}/remote.zip",
        timeout=5,
    )
    assert [bundle.plugin_id for bundle in bundles] == ["remote"]


def test_zip_with_redundant_separators() -> None:
    # Normalisation must not break reading the manifest at its archive name.
    bundle = bundle_from_zip_bytes(
        _zip_bytes({"demo//plugin.json": _manifest(), "demo//a.py": b"x"}),
        origin="odd.zip",
    )
    assert bundle.plugin_id == "demo"
