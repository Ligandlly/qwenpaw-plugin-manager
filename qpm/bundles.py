# -*- coding: utf-8 -*-
"""Read plugin bundles from directories, ZIP files, or HTTP(S) URLs.

The runtime's hot-install endpoint accepts either a ZIP upload or a *path
inside the container*.  A fleet manager running on the Hub host cannot assume
any shared path with the containers, so everything is normalised into a ZIP
payload and pushed through ``POST /api/plugins/upload``.

Two rules keep this safe:

* archives are never extracted locally, but every member name is validated so
  a hostile archive cannot hide ``../`` or absolute paths;
* bundles built from a directory are reproducible (fixed timestamps, sorted
  entries) so the same tree always yields the same SHA-256.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

from . import logs
from .errors import SourceError
from .models import PluginBundle

__all__ = [
    "DEFAULT_MAX_BUNDLE_BYTES",
    "load_bundle_source",
    "build_bundle_from_dir",
    "bundle_from_zip_bytes",
    "read_manifest",
]

DEFAULT_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_ENTRIES = 5000
_DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024

_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
    },
)
_SKIP_FILE_SUFFIXES = frozenset({".pyc", ".pyo", ".log", ".swp"})
_SKIP_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o644

_LOG = logs.get_logger("bundles")


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


# ── Manifest parsing ─────────────────────────────────────────────────────


def read_manifest(payload: object, *, origin: str) -> dict[str, str]:
    """Validate the fields the fleet manager needs and normalise them."""
    if not isinstance(payload, dict):
        raise SourceError(f"{origin}: plugin.json must be a JSON object")
    plugin_id = str(payload.get("id") or "").strip()
    if not plugin_id:
        raise SourceError(f"{origin}: plugin.json has no 'id'")
    if not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise SourceError(
            f"{origin}: invalid plugin id {plugin_id!r} (letters, digits, "
            "underscore and hyphen only)",
        )
    version = str(payload.get("version") or "").strip()
    if not version:
        raise SourceError(f"{origin}: plugin.json has no 'version'")

    sdk_version = ""
    sdk = payload.get("sdk")
    if isinstance(sdk, dict):
        sdk_version = str(sdk.get("version") or "").strip()

    qwenpaw = payload.get("qwenpaw_version")
    qwenpaw_min = ""
    qwenpaw_max = ""
    if isinstance(qwenpaw, dict):
        qwenpaw_min = str(qwenpaw.get("min") or "").strip()
        qwenpaw_max = str(qwenpaw.get("max") or "").strip()
    if not qwenpaw_min:
        qwenpaw_min = str(payload.get("min_version") or "").strip()
    if not qwenpaw_max:
        qwenpaw_max = str(payload.get("max_version") or "").strip()

    return {
        "id": plugin_id,
        "version": version,
        "name": str(payload.get("name") or plugin_id),
        "description": str(payload.get("description") or ""),
        "type": str(payload.get("type") or "general"),
        "sdk_version": sdk_version,
        "qwenpaw_min": qwenpaw_min,
        "qwenpaw_max": qwenpaw_max,
    }


def _json_from_bytes(data: bytes, *, origin: str) -> object:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"{origin}: plugin.json is not valid JSON ({exc})")


# ── ZIP handling ─────────────────────────────────────────────────────────


def _safe_member_name(name: str, *, origin: str) -> str:
    """Reject archive members that could escape an extraction root."""
    if not name or "\x00" in name or "\\" in name:
        raise SourceError(f"{origin}: unsafe archive member {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise SourceError(f"{origin}: absolute archive member {name!r}")
    parts = [part for part in name.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise SourceError(f"{origin}: archive member escapes the root {name!r}")
    return "/".join(parts)


def bundle_from_zip_bytes(
    data: bytes,
    *,
    origin: str,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> PluginBundle:
    """Validate a ZIP payload and turn it into a :class:`PluginBundle`."""
    if len(data) > max_bytes:
        raise SourceError(
            f"{origin}: bundle is {len(data)} bytes, above the configured "
            f"{max_bytes} byte limit",
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SourceError(f"{origin}: not a ZIP archive ({exc})") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > _DEFAULT_MAX_ENTRIES:
            raise SourceError(
                f"{origin}: archive has {len(infos)} entries "
                f"(limit {_DEFAULT_MAX_ENTRIES})",
            )
        # Keep (normalised name, archive name) pairs: the normalised name is
        # what we validate and report, the archive name is what we must read.
        manifest_members: list[tuple[str, str]] = []
        for info in infos:
            name = _safe_member_name(info.filename, origin=origin)
            if info.file_size > _DEFAULT_MAX_FILE_BYTES:
                raise SourceError(
                    f"{origin}: archive member {name!r} is larger than "
                    f"{_DEFAULT_MAX_FILE_BYTES} bytes",
                )
            if name.split("/")[-1] == "plugin.json":
                manifest_members.append((name, info.filename))
        if not manifest_members:
            raise SourceError(
                f"{origin}: no plugin.json in the archive root or a "
                "top-level directory",
            )
        candidates = sorted(
            manifest_members,
            key=lambda item: (item[0].count("/"), item[0]),
        )
        manifest_path, archive_member = candidates[0]
        depth = manifest_path.count("/")
        if depth > 1:
            raise SourceError(
                f"{origin}: plugin.json must sit in the archive root or in "
                "exactly one top-level directory, found "
                f"{manifest_path!r}",
            )
        # A second manifest at the same (or shallower) depth is ambiguous:
        # the runtime would pick one and the operator would not know which.
        same_depth = [
            name for name, _ in candidates if name.count("/") == depth
        ]
        if len(same_depth) > 1:
            raise SourceError(
                f"{origin}: more than one plugin.json at the same level "
                f"({', '.join(same_depth)}); package plugins individually",
            )
        try:
            raw_manifest = archive.read(archive_member)
        except KeyError as exc:  # pragma: no cover - infolist/read mismatch
            raise SourceError(f"{origin}: cannot read {manifest_path!r}") from exc

    manifest = read_manifest(
        _json_from_bytes(raw_manifest, origin=f"{origin}:{manifest_path}"),
        origin=f"{origin}:{manifest_path}",
    )
    return PluginBundle(
        plugin_id=manifest["id"],
        version=manifest["version"],
        data=data,
        origin=origin,
        name=manifest["name"],
        plugin_type=manifest["type"],
        sdk_version=manifest["sdk_version"],
        qwenpaw_min=manifest["qwenpaw_min"],
        qwenpaw_max=manifest["qwenpaw_max"],
        sha256=sha256_hex(data),
        description=manifest["description"],
    )


def _relative_skipped(relative: PurePosixPath) -> bool:
    """Return whether one relative path should be excluded from a bundle."""
    if relative.name in _SKIP_FILE_NAMES:
        return True
    if relative.suffix.lower() in _SKIP_FILE_SUFFIXES:
        return True
    return any(part in _SKIP_DIR_NAMES for part in relative.parts[:-1])


def _iter_plugin_files(plugin_dir: Path) -> Iterable[tuple[str, Path]]:
    """Yield ``(relative posix path, file path)`` in stable order."""
    root = plugin_dir.resolve()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if _relative_skipped(relative):
            continue
        if path.is_symlink():
            raise SourceError(
                f"{plugin_dir}: symbolic links are not allowed in a plugin "
                f"bundle ({relative.as_posix()})",
            )
        if path.is_dir():
            continue
        yield relative.as_posix(), path


def build_bundle_from_dir(
    plugin_dir: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> PluginBundle:
    """Package a plugin directory into a reproducible ZIP bundle."""
    plugin_dir = Path(plugin_dir)
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.is_file():
        raise SourceError(f"{plugin_dir}: no plugin.json in the directory")
    manifest = read_manifest(
        _json_from_bytes(
            manifest_path.read_bytes(),
            origin=str(manifest_path),
        ),
        origin=str(manifest_path),
    )
    prefix = manifest["id"]

    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(
        buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for relative, path in _iter_plugin_files(plugin_dir):
            payload = path.read_bytes()
            total += len(payload)
            if total > max_bytes:
                raise SourceError(
                    f"{plugin_dir}: plugin exceeds the "
                    f"{max_bytes} byte bundle limit",
                )
            info = zipfile.ZipInfo(
                filename=f"{prefix}/{relative}",
                date_time=_FIXED_DATE,
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = _FILE_MODE << 16
            info.create_system = 3
            archive.writestr(info, payload, compresslevel=6)

    data = buffer.getvalue()
    return PluginBundle(
        plugin_id=manifest["id"],
        version=manifest["version"],
        data=data,
        origin=str(plugin_dir),
        name=manifest["name"],
        plugin_type=manifest["type"],
        sdk_version=manifest["sdk_version"],
        qwenpaw_min=manifest["qwenpaw_min"],
        qwenpaw_max=manifest["qwenpaw_max"],
        sha256=sha256_hex(data),
        description=manifest["description"],
    )


# ── Source resolution ────────────────────────────────────────────────────


def _download(url: str, *, timeout: float, max_bytes: int) -> bytes:
    _LOG.debug(
        "downloading %s (timeout %.0fs, cap %d byte(s))",
        url,
        timeout,
        max_bytes,
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/zip, application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise SourceError(
                    f"{url}: remote bundle is {declared} bytes, above the "
                    f"{max_bytes} byte limit",
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SourceError(
                        f"{url}: remote bundle exceeds the "
                        f"{max_bytes} byte limit",
                    )
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"{url}: download failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"{url}: download failed: {exc}") from exc
    _LOG.debug("downloaded %d byte(s) from %s", len(b"".join(chunks)), url)
    return b"".join(chunks)


def load_bundle_source(
    source: str,
    *,
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> list[PluginBundle]:
    """Load one or more bundles from a directory, ZIP file, or URL.

    A directory may either be a single plugin (it has ``plugin.json``) or a
    collection of plugins (sub-directories with ``plugin.json`` plus ``.zip``
    files), which is what a release pipeline usually produces.
    """
    text = str(source or "").strip()
    if not text:
        raise SourceError("empty bundle source")
    _LOG.debug("loading bundle source %s", text)
    if text.startswith(("http://", "https://")):
        return [
            bundle_from_zip_bytes(
                _download(text, timeout=timeout, max_bytes=max_bytes),
                origin=text,
                max_bytes=max_bytes,
            ),
        ]

    path = Path(text).expanduser()
    if not path.exists():
        raise SourceError(f"{text}: no such file or directory")
    if path.is_file():
        if path.suffix.lower() != ".zip":
            raise SourceError(
                f"{text}: only directories and .zip bundles are supported",
            )
        return [
            bundle_from_zip_bytes(
                path.read_bytes(),
                origin=str(path),
                max_bytes=max_bytes,
            ),
        ]

    if (path / "plugin.json").is_file():
        return [build_bundle_from_dir(path, max_bytes=max_bytes)]

    bundles: list[PluginBundle] = []
    skipped: list[str] = []
    _LOG.debug("scanning release directory %s", path)
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        if child.is_dir() and (child / "plugin.json").is_file():
            bundles.append(build_bundle_from_dir(child, max_bytes=max_bytes))
        elif child.is_file() and child.suffix.lower() == ".zip":
            bundles.append(
                bundle_from_zip_bytes(
                    child.read_bytes(),
                    origin=str(child),
                    max_bytes=max_bytes,
                ),
            )
        elif child.name.startswith(".") or child.name in _SKIP_DIR_NAMES:
            continue
        else:
            skipped.append(child.name)
    if not bundles:
        raise SourceError(
            f"{path}: no plugin.json found; expected a plugin directory, a "
            "single .zip, or a directory of plugins/zips"
            + (f" (ignored: {', '.join(skipped[:5])})" if skipped else ""),
        )
    _reject_duplicate_ids(bundles)
    _LOG.debug(
        "%s: %d bundle(s)%s",
        path,
        len(bundles),
        f", ignored {len(skipped)} entr(ies)" if skipped else "",
    )
    return bundles


def _reject_duplicate_ids(bundles: list[PluginBundle]) -> None:
    seen: dict[str, PluginBundle] = {}
    for bundle in bundles:
        previous = seen.get(bundle.plugin_id)
        if previous is not None:
            raise SourceError(
                f"two bundles provide plugin {bundle.plugin_id!r}: "
                f"{previous.origin} and {bundle.origin}",
            )
        seen[bundle.plugin_id] = bundle
