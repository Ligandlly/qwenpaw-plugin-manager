# -*- coding: utf-8 -*-
"""Minimal HTTP client for the QwenPaw runtime plugin API.

Every request carries ``X-QwenPaw-Runtime-Token``: when the Hub starts a
runtime in container mode, that boundary token is the only credential the
runtime's auth middleware accepts, and it grants full API access (the same
mechanism the Hub uses for its readiness probe and reverse proxy).

Endpoints used (all verified against QwenPaw 2.2.x
``src/qwenpaw/app/routers/plugins.py``):

* ``GET  /api/version``                     runtime version
* ``GET  /api/settings/upload-limit``       configured upload cap
* ``GET  /api/plugins``                     installed plugins
* ``GET  /api/plugins/{id}``                one plugin
* ``POST /api/plugins/upload``              install/replace from ZIP
* ``POST /api/plugins/install``             install from a container path
* ``DELETE /api/plugins/{id}``              uninstall
* ``POST /api/plugins/{id}/unload``         unload, keep files/config
* ``POST /api/plugins/{id}/enabled``        enable/disable
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .errors import (
    PluginAlreadyInstalled,
    PluginNotFound,
    RuntimeApiError,
    RuntimeAuthError,
    RuntimeUnreachable,
)
from .models import InstalledPlugin, PluginBundle, RuntimeTarget

__all__ = ["RuntimeClient"]

_TOKEN_HEADER = "X-QwenPaw-Runtime-Token"
#: Statuses worth retrying: the runtime is starting, restarting, or a proxy
#: in between had a hiccup.  Everything else is a real answer.
_RETRY_STATUSES = frozenset({502, 503, 504})

#: Hub endpoints are loopback-only by design; never route them through a
#: corporate HTTP proxy picked up from the environment.
_LOOPBACK_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
)


def _opener_for(url: str) -> urllib.request.OpenerDirector:
    """Return a proxy-free opener for loopback, the default one otherwise."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return _LOOPBACK_OPENER
    try:
        if ipaddress.ip_address(host).is_loopback:
            return _LOOPBACK_OPENER
    except ValueError:
        pass
    return urllib.request.build_opener()


def _detail_text(payload: object) -> tuple[str, object]:
    detail: object = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
    if isinstance(detail, str):
        return detail, detail
    if isinstance(detail, dict):
        message = detail.get("message")
        if not message:
            message = ", ".join(
                f"{key}={value}" for key, value in detail.items()
            ) or "request rejected"
        return str(message), detail
    if detail is not None:
        return str(detail), detail
    return "", None


class RuntimeClient:
    """Talk to one runtime's plugin API over its loopback endpoint."""

    def __init__(
        self,
        target: RuntimeTarget,
        *,
        timeout: float = 30.0,
        operation_timeout: float = 180.0,
        retries: int = 2,
        backoff: float = 0.5,
    ) -> None:
        self.target = target
        self.timeout = timeout
        self.operation_timeout = max(operation_timeout, timeout)
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self._base = target.base_url.rstrip("/")

    # ── transport ────────────────────────────────────────────────────────

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self._base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def _header_map(self) -> dict[str, str]:
        return {
            _TOKEN_HEADER: self.target.token,
            "Accept": "application/json",
            "User-Agent": "qwenpaw-plugin-manager/0.1",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        timeout: float | None = None,
        retry: bool = True,
        allow_statuses: frozenset[int] = frozenset(),
    ) -> tuple[int, object]:
        """Perform one request, retrying transient failures.

        Returns ``(status, parsed JSON or None)``.  ``allow_statuses`` keeps
        the listed error statuses from raising, which callers use for
        ``404``/``409`` control flow.
        """
        body = data
        request_headers = self._header_map()
        if headers:
            request_headers.update(headers)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        url = self._url(path, query)
        attempts = self.retries + 1 if retry else 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                url,
                data=body,
                headers=request_headers,
                method=method,
            )
            try:
                with _opener_for(url).open(
                    request,
                    timeout=timeout or self.timeout,
                ) as response:
                    return response.status, self._decode(response.read())
            except urllib.error.HTTPError as exc:
                status = exc.code
                raw = self._read_error_body(exc)
                parsed = self._decode(raw)
                if status in allow_statuses:
                    return status, parsed
                if status == 401:
                    raise RuntimeAuthError(
                        "runtime rejected the boundary token (401); the "
                        "container may have been recreated — re-run discovery",
                        status=status,
                        detail=parsed,
                        body=raw,
                    ) from exc
                message, detail = _detail_text(parsed)
                summary = f"HTTP {status}"
                if message:
                    summary = f"{summary}: {message}"
                elif raw:
                    summary = f"{summary} (body: {raw[:300]!r})"
                error: Exception = RuntimeApiError(
                    summary,
                    status=status,
                    detail=detail,
                    body=raw,
                )
                if status in _RETRY_STATUSES and attempt < attempts:
                    last_error = error
                    self._sleep(attempt)
                    continue
                raise error from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = RuntimeUnreachable(
                    f"{self.target.runtime_id}: cannot reach "
                    f"{self.target.base_url} ({reason})",
                )
                if attempt < attempts:
                    self._sleep(attempt)
                    continue
                raise last_error from exc
            except OSError as exc:
                last_error = RuntimeUnreachable(
                    f"{self.target.runtime_id}: transport error talking to "
                    f"{self.target.base_url} ({exc})",
                )
                if attempt < attempts:
                    self._sleep(attempt)
                    continue
                raise last_error from exc
        assert last_error is not None  # pragma: no cover - loop always raises
        raise last_error

    def _sleep(self, attempt: int) -> None:
        if self.backoff:
            time.sleep(self.backoff * attempt)

    @staticmethod
    def _read_error_body(exc: urllib.error.HTTPError) -> str:
        try:
            return exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            return ""

    @staticmethod
    def _decode(raw: bytes | str) -> object:
        if not raw:
            return None
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        else:
            text = raw
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    # ── read-only API ────────────────────────────────────────────────────

    def app_version(self) -> str | None:
        """Return the runtime's QwenPaw version (``None`` when unknown)."""
        status, payload = self._request("GET", "/api/version")
        if status != 200 or not isinstance(payload, dict):
            return None
        return str(payload.get("version") or "") or None

    def upload_limit_mb(self) -> int | None:
        """Return the runtime's plugin upload cap in MiB, if configured."""
        try:
            status, payload = self._request(
                "GET",
                "/api/settings/upload-limit",
                retry=False,
            )
        except RuntimeApiError:
            return None
        if status != 200 or not isinstance(payload, dict):
            return None
        value = payload.get("upload_max_size_mb")
        return int(value) if isinstance(value, (int, float)) else None

    def healthz(self) -> tuple[bool, str]:
        """Return whether the runtime has finished starting up."""
        status, payload = self._request(
            "GET",
            "/api/healthz",
            retry=False,
            allow_statuses=frozenset({503}),
        )
        if status == 200:
            return True, ""
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("status") or ""
            return False, str(detail)
        return False, f"HTTP {status}"

    def list_plugins(self) -> dict[str, InstalledPlugin]:
        """Return installed plugins keyed by plugin id."""
        status, payload = self._request("GET", "/api/plugins")
        if status != 200 or not isinstance(payload, list):
            raise RuntimeApiError(
                f"unexpected /api/plugins payload: {type(payload).__name__}",
                status=status,
            )
        plugins: dict[str, InstalledPlugin] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            plugin_id = str(item.get("id") or "")
            if not plugin_id:
                continue
            plugins[plugin_id] = InstalledPlugin(
                plugin_id=plugin_id,
                version=str(item.get("version") or ""),
                enabled=bool(item.get("enabled", True)),
                loaded=bool(item.get("loaded", False)),
                status=str(item.get("status") or ""),
                plugin_type=str(item.get("plugin_type") or ""),
            )
        return plugins

    def get_plugin(self, plugin_id: str) -> InstalledPlugin | None:
        """Return one installed plugin, or ``None`` when it is absent."""
        status, payload = self._request(
            "GET",
            f"/api/plugins/{urllib.parse.quote(plugin_id, safe='')}",
            allow_statuses=frozenset({404}),
        )
        if status == 404 or not isinstance(payload, dict):
            return None
        return InstalledPlugin(
            plugin_id=str(payload.get("id") or plugin_id),
            version=str(payload.get("version") or ""),
            enabled=bool(payload.get("enabled", True)),
            loaded=bool(payload.get("loaded", False)),
            status=str(payload.get("status") or ""),
            plugin_type=str(payload.get("plugin_type") or ""),
        )

    # ── mutating API ─────────────────────────────────────────────────────

    def upload_plugin(
        self,
        bundle: PluginBundle,
        *,
        force: bool,
        interrupt_tasks: bool = False,
    ) -> dict[str, object]:
        """Install or replace a plugin from a ZIP payload."""
        boundary = f"----qpm{uuid.uuid4().hex}"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{bundle.filename}"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = head + bundle.data + tail
        query = {"force": "true" if force else "false"}
        if interrupt_tasks:
            query["interrupt_tasks"] = "true"
        try:
            status, payload = self._request(
                "POST",
                "/api/plugins/upload",
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                query=query,
                timeout=self.operation_timeout,
            )
        except RuntimeApiError as exc:
            if exc.status == 409:
                raise PluginAlreadyInstalled(
                    str(exc),
                    status=exc.status,
                    detail=exc.detail,
                    body=exc.body,
                ) from exc
            raise
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeApiError(
                f"unexpected install response: HTTP {status}",
                status=status,
            )
        return payload

    def install_path(
        self,
        container_path: str,
        *,
        force: bool = True,
        interrupt_tasks: bool = False,
    ) -> dict[str, object]:
        """Install from a path that exists *inside* the runtime container."""
        status, payload = self._request(
            "POST",
            "/api/plugins/install",
            payload={
                "source": container_path,
                "force": force,
                "interrupt_tasks": interrupt_tasks,
            },
            timeout=self.operation_timeout,
        )
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeApiError(
                f"unexpected install response: HTTP {status}",
                status=status,
            )
        return payload

    def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        interrupt_tasks: bool = False,
        keep_files: bool = False,
    ) -> dict[str, object] | None:
        """Uninstall (or only unload) one plugin."""
        quoted = urllib.parse.quote(plugin_id, safe="")
        query = {"interrupt_tasks": "true"} if interrupt_tasks else None
        try:
            if keep_files:
                status, payload = self._request(
                    "POST",
                    f"/api/plugins/{quoted}/unload",
                    query=query,
                    timeout=self.operation_timeout,
                    allow_statuses=frozenset({404}),
                )
            else:
                status, payload = self._request(
                    "DELETE",
                    f"/api/plugins/{quoted}",
                    query=query,
                    timeout=self.operation_timeout,
                    allow_statuses=frozenset({404}),
                )
        except RuntimeApiError as exc:
            if exc.status == 404:
                raise PluginNotFound(
                    str(exc),
                    status=exc.status,
                    detail=exc.detail,
                    body=exc.body,
                ) from exc
            raise
        if status == 404:
            return None
        if status != 200:
            raise RuntimeApiError(
                f"unexpected uninstall response: HTTP {status}",
                status=status,
            )
        return payload if isinstance(payload, dict) else {}

    def set_enabled(
        self,
        plugin_id: str,
        enabled: bool,
        *,
        interrupt_tasks: bool = False,
    ) -> dict[str, object]:
        """Enable or disable one installed plugin."""
        quoted = urllib.parse.quote(plugin_id, safe="")
        status, payload = self._request(
            "POST",
            f"/api/plugins/{quoted}/enabled",
            payload={
                "enabled": bool(enabled),
                "interrupt_tasks": bool(interrupt_tasks),
            },
            timeout=self.operation_timeout,
            allow_statuses=frozenset({404}),
        )
        if status == 404:
            raise PluginNotFound(
                f"plugin {plugin_id!r} not found",
                status=status,
                detail=payload,
            )
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeApiError(
                f"unexpected enable/disable response: HTTP {status}",
                status=status,
            )
        return payload
