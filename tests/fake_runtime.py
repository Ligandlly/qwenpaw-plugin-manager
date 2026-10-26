# -*- coding: utf-8 -*-
"""A small in-process stand-in for the QwenPaw runtime plugin API.

It implements the subset of ``/api/plugins`` the manager drives, keeps the
plugin files in a host directory (so backup/rollback can be exercised), and
enforces ``X-QwenPaw-Runtime-Token`` exactly like the real boundary middleware.
"""

from __future__ import annotations

import io
import json
import threading
import zipfile
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TOKEN_HEADER = "X-QwenPaw-Runtime-Token"

GENERAL = "general"


class FakeRuntime:
    """One fake Hub runtime."""

    def __init__(
        self,
        *,
        runtime_id: str = "rt-1",
        token: str = "secret-token",
        qwenpaw_version: str = "2.2.2",
        plugins_dir: Path | None = None,
        container_workspace: str = "/workspace",
        upload_limit_mb: int | None = None,
        healthy: bool = True,
    ) -> None:
        self.runtime_id = runtime_id
        self.token = token
        self.qwenpaw_version = qwenpaw_version
        self.plugins_dir = plugins_dir
        self.host_plugins_dir = plugins_dir
        self.host_working_dir = (
            plugins_dir.parent if plugins_dir is not None else None
        )
        self.container_workspace = container_workspace
        self.upload_limit_mb = upload_limit_mb
        self.healthy = healthy
        self.plugins: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_next_upload = 0
        self.fail_next_delete = 0
        self.corrupt_next_upload_version: str | None = None
        self.reject_next_upload_status: int | None = None
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> "FakeRuntime":
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._server.runtime = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # ── helpers used by the handler ──────────────────────────────────────

    def list_plugins(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self.plugins.values()]

    def get_plugin(self, plugin_id: str) -> dict | None:
        with self._lock:
            item = self.plugins.get(plugin_id)
            return dict(item) if item is not None else None

    def _install(
        self,
        plugin_id: str,
        version: str,
        name: str,
        *,
        enabled: bool = True,
    ) -> dict:
        with self._lock:
            self.plugins[plugin_id] = {
                "id": plugin_id,
                "name": name or plugin_id,
                "version": version,
                "description": "",
                "author": "test",
                "enabled": enabled,
                "loaded": True,
                "status": "active",
                "plugin_type": GENERAL,
                "frontend_entry": None,
            }
            return dict(self.plugins[plugin_id])

    def _remove_files(self, plugin_id: str) -> None:
        if self.plugins_dir is None:
            return
        target = self.plugins_dir / plugin_id
        if not target.exists():
            return
        for child in sorted(target.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()
        target.rmdir()

    def _extract(self, data: bytes, plugin_id: str) -> None:
        if self.plugins_dir is None:
            return
        destination = self.plugins_dir / plugin_id
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            prefix = ""
            if names:
                first = names[0].split("/")[0]
                if all(name.startswith(f"{first}/") for name in names):
                    prefix = f"{first}/"
            for name in names:
                relative = name[len(prefix) :]
                if not relative:
                    continue
                target = (destination / relative).resolve()
                if not target.is_relative_to(root):
                    raise ValueError(f"unsafe archive member: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))

    def _manifest_from_zip(self, data: bytes) -> dict:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            candidates = sorted(
                (
                    name
                    for name in archive.namelist()
                    if name == "plugin.json" or name.endswith("/plugin.json")
                ),
                key=lambda name: (name.count("/"), name),
            )
            if not candidates:
                raise ValueError("no plugin.json")
            return json.loads(archive.read(candidates[0]).decode("utf-8"))

    def _manifest_from_dir(self, directory: Path) -> dict:
        return json.loads((directory / "plugin.json").read_text(encoding="utf-8"))

    def _container_plugin_dir(self, source: str) -> Path | None:
        prefix = f"{self.container_workspace.rstrip('/')}/plugins/"
        if not source.startswith(prefix):
            return None
        plugin_id = source[len(prefix) :].strip("/")
        if not plugin_id or "/" in plugin_id or self.plugins_dir is None:
            return None
        return self.plugins_dir / plugin_id


def _make_handler(runtime: FakeRuntime):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # noqa: A003 - silence test output
            return

        # ── plumbing ────────────────────────────────────────────────────

        def _json(self, status: int, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _authorized(self) -> bool:
            supplied = self.headers.get(TOKEN_HEADER, "")
            if supplied != runtime.token:
                self._json(401, {"detail": "Invalid runtime boundary token"})
                return False
            return True

        def _plugin_from_multipart(self, body: bytes) -> bytes | None:
            content_type = self.headers.get("Content-Type", "")
            raw = (
                b"Content-Type: "
                + content_type.encode("latin-1")
                + b"\r\nMIME-Version: 1.0\r\n\r\n"
                + body
            )
            message = BytesParser(policy=email_policy).parsebytes(raw)
            for part in message.iter_parts():
                if part.get_param("name", header="content-disposition") == "file":
                    payload = part.get_payload(decode=True)
                    return payload if isinstance(payload, bytes) else None
            return None

        # ── routes ──────────────────────────────────────────────────────

        def do_GET(self):  # noqa: N802 - http.server API
            if not self._authorized():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            runtime.requests.append(("GET", path))
            if path == "/api/version":
                return self._json(200, {"version": runtime.qwenpaw_version})
            if path == "/api/settings/upload-limit":
                return self._json(
                    200,
                    {"upload_max_size_mb": runtime.upload_limit_mb},
                )
            if path == "/api/healthz":
                if runtime.healthy:
                    return self._json(200, {"status": "ok"})
                return self._json(503, {"status": "starting", "detail": "booting"})
            if path == "/api/plugins":
                return self._json(200, runtime.list_plugins())
            if path.startswith("/api/plugins/"):
                plugin_id = path[len("/api/plugins/") :]
                plugin = runtime.get_plugin(plugin_id)
                if plugin is None:
                    return self._json(
                        404,
                        {"detail": f"Plugin '{plugin_id}' not found."},
                    )
                return self._json(200, plugin)
            return self._json(404, {"detail": "not found"})

        def do_DELETE(self):  # noqa: N802 - http.server API
            if not self._authorized():
                return
            path = urlsplit(self.path).path
            runtime.requests.append(("DELETE", path))
            plugin_id = path[len("/api/plugins/") :] if path.startswith("/api/plugins/") else ""
            with runtime._lock:
                if runtime.fail_next_delete:
                    runtime.fail_next_delete -= 1
                    return self._json(500, {"detail": "simulated delete failure"})
                if plugin_id not in runtime.plugins:
                    return self._json(
                        404,
                        {"detail": f"Plugin '{plugin_id}' not found."},
                    )
                runtime.plugins.pop(plugin_id, None)
            runtime._remove_files(plugin_id)
            return self._json(
                200,
                {"id": plugin_id, "message": f"Plugin '{plugin_id}' removed."},
            )

        def do_POST(self):  # noqa: N802 - http.server API
            if not self._authorized():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            runtime.requests.append(("POST", path))
            body = self._read_body()

            if path == "/api/plugins/upload":
                return self._upload(body, query)
            if path == "/api/plugins/install":
                return self._install_path(body)
            if path.endswith("/unload") and path.startswith("/api/plugins/"):
                plugin_id = path[len("/api/plugins/") : -len("/unload")]
                with runtime._lock:
                    item = runtime.plugins.get(plugin_id)
                    if item is None:
                        return self._json(
                            404,
                            {"detail": f"Plugin '{plugin_id}' not found."},
                        )
                    item["loaded"] = False
                    item["status"] = "inactive"
                    return self._json(200, {"id": plugin_id, "loaded": False})
            if path.endswith("/enabled") and path.startswith("/api/plugins/"):
                plugin_id = path[len("/api/plugins/") : -len("/enabled")]
                payload = json.loads(body or b"{}")
                enabled = bool(payload.get("enabled", True))
                with runtime._lock:
                    item = runtime.plugins.get(plugin_id)
                    if item is None:
                        return self._json(
                            404,
                            {"detail": f"Plugin '{plugin_id}' not found."},
                        )
                    item["enabled"] = enabled
                    item["loaded"] = enabled
                    item["status"] = "active" if enabled else "inactive"
                    return self._json(
                        200,
                        {"id": plugin_id, "enabled": enabled, "loaded": enabled},
                    )
            return self._json(404, {"detail": "not found"})

        # ── endpoint implementations ────────────────────────────────────

        def _upload(self, body: bytes, query: dict[str, list[str]]):
            with runtime._lock:
                if runtime.fail_next_upload:
                    runtime.fail_next_upload -= 1
                    return self._json(500, {"detail": "simulated upload failure"})
                if runtime.reject_next_upload_status is not None:
                    status = runtime.reject_next_upload_status
                    runtime.reject_next_upload_status = None
                    return self._json(status, {"detail": "simulated rejection"})
            data = self._plugin_from_multipart(body)
            if data is None:
                return self._json(400, {"detail": "missing file upload"})
            try:
                manifest = runtime._manifest_from_zip(data)
            except (KeyError, ValueError, zipfile.BadZipFile) as exc:
                return self._json(400, {"detail": f"invalid plugin: {exc}"})
            plugin_id = str(manifest.get("id") or "")
            version = str(manifest.get("version") or "")
            name = str(manifest.get("name") or plugin_id)
            force = (query.get("force") or ["false"])[0].lower() == "true"
            with runtime._lock:
                exists = plugin_id in runtime.plugins
            if exists and not force:
                return self._json(
                    409,
                    {
                        "detail": {
                            "code": "plugin_busy",
                            "message": (
                                f"Plugin '{plugin_id}' is already loaded. "
                                "Use --force to reinstall."
                            ),
                        },
                    },
                )
            runtime._extract(data, plugin_id)
            stored_version = version
            with runtime._lock:
                corrupt = runtime.corrupt_next_upload_version
                if corrupt is not None:
                    stored_version = corrupt
                    runtime.corrupt_next_upload_version = None
            record = runtime._install(
                plugin_id,
                stored_version,
                name,
            )
            return self._json(
                200,
                {**record, "message": f"Plugin '{name}' installed."},
            )

        def _install_path(self, body: bytes):
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"detail": "invalid JSON body"})
            source = str(payload.get("source") or "")
            directory = runtime._container_plugin_dir(source)
            if directory is None:
                return self._json(
                    400,
                    {"detail": f"Path not found: {source}"},
                )
            if not (directory / "plugin.json").is_file():
                return self._json(
                    400,
                    {"detail": f"Path not found: {source}"},
                )
            manifest = runtime._manifest_from_dir(directory)
            plugin_id = str(manifest.get("id") or directory.name)
            record = runtime._install(
                plugin_id,
                str(manifest.get("version") or ""),
                str(manifest.get("name") or plugin_id),
            )
            return self._json(
                200,
                {**record, "message": f"Plugin '{plugin_id}' installed."},
            )

    return Handler
