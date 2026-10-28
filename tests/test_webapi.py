# -*- coding: utf-8 -*-
"""Web console: auth, discovery, plan preview, execution, uploads, freeze."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from qpm.bundles import build_bundle_from_dir
from qpm.executor import ExecOptions
from qpm.sources import DiscoveryOptions
from qpm.webapi import WebConfig, create_server, resolve_bind_host

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class WebClient:
    """Tiny HTTP client for the console API."""

    def __init__(self, base: str, token: str) -> None:
        self.base = base
        self.token = token

    def _request(self, method, path, *, body=None, headers=None, token=True):
        all_headers = dict(headers or {})
        if token:
            all_headers["X-QPM-Token"] = self.token
        request = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers=all_headers,
        )
        try:
            with _OPENER.open(request, timeout=15) as response:
                raw = response.read()
                return response.status, dict(response.headers), raw
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def get(self, path, *, token=True):
        return self._request("GET", path, token=token)

    def get_json(self, path, *, token=True):
        status, headers, raw = self.get(path, token=token)
        return status, headers, json.loads(raw) if raw else None

    def post_json(self, path, payload, *, headers=None, token=True):
        body = json.dumps(payload).encode("utf-8")
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        status, response_headers, raw = self._request(
            "POST",
            path,
            body=body,
            headers=merged,
            token=token,
        )
        return status, response_headers, json.loads(raw) if raw else None

    def post_raw(self, path, body: bytes, *, headers=None, token=True):
        status, response_headers, raw = self._request(
            "POST",
            path,
            body=body,
            headers=headers or {},
            token=token,
        )
        return status, response_headers, raw

    def upload(self, filename: str, data: bytes):
        boundary = f"----qpmtest{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        status, _, raw = self.post_raw(
            "/api/upload",
            body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        return status, {}, json.loads(raw) if raw else None

    def wait_job(self, job_id: str, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, _, payload = self.get_json(f"/api/jobs/{job_id}")
            assert status == 200, payload
            job = payload["job"]
            if job["status"] != "running":
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish")


class WebFixture:
    def __init__(self, server, token: str, runtime) -> None:
        self.server = server
        self.token = token
        self.runtime = runtime
        host, port = server.server_address[:2]
        self.client = WebClient(f"http://{host}:{port}", token)
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.ctx.cleanup()
        self.thread.join(timeout=5)


def _start(inventory: Path, runtime, *, token_required=True, token="test-token"):
    config = WebConfig(
        discovery=DiscoveryOptions(inventory=inventory),
        exec_options=ExecOptions(concurrency=2, retries=1),
        token=token,
        token_required=token_required,
        workdir=inventory.parent / "web-uploads",
    )
    server = create_server(config)
    return WebFixture(server, config.token, runtime)


@pytest.fixture
def web(runtime_factory, inventory_file):
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    fixture = _start(inventory, runtime)
    try:
        yield fixture
    finally:
        fixture.stop()


def filters(runtime_id="rt-1"):
    return {"runtime": [runtime_id], "include_stopped": False}


def test_static_assets_and_csp(web: WebFixture) -> None:
    status, headers, raw = web.client.get("/", token=False)
    assert status == 200
    assert "Fleet" in raw.decode()
    assert "default-src 'self'" in headers["Content-Security-Policy"]

    status, headers, raw = web.client.get("/app.js", token=False)
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert b"use strict" in raw

    status, headers, raw = web.client.get("/styles.css", token=False)
    assert status == 200
    assert "text/css" in headers["Content-Type"]

    status, headers, raw = web.client.get("/favicon.svg", token=False)
    assert status == 200
    assert headers["Content-Type"].startswith("image/svg")


def test_api_requires_token(web: WebFixture) -> None:
    status, _, payload = web.client.get_json("/api/meta", token=False)
    assert status == 401
    assert "token" in payload["error"]

    status, _, payload = web.client.get_json("/api/meta")
    assert status == 200
    assert payload["token_required"] is True
    assert payload["discovery"]["source"] == "inventory"
    assert payload["execution"]["backup"] is True


def test_inventory_lists_runtime(web: WebFixture) -> None:
    status, _, payload = web.client.get_json("/api/inventory")
    assert status == 200
    assert len(payload["runtimes"]) == 1
    row = payload["runtimes"][0]
    assert row["runtime_id"] == "rt-1"
    assert row["username"] == ""
    assert row["display_name"] == "rt-1"
    assert row["qwenpaw_version"] == "2.2.2"
    assert row["plugins"] == []
    assert row["state"] == "running"


def test_runtimes_endpoint_does_not_probe(web: WebFixture) -> None:
    status, _, payload = web.client.get_json("/api/runtimes")
    assert status == 200
    assert payload["runtimes"][0]["runtime_id"] == "rt-1"
    # The cheap endpoint must not touch the runtime API.
    assert web.runtime.requests == []


def test_runtimes_payload_labels_runtimes_with_the_owner(
    runtime_factory,
    inventory_file,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    document = json.loads(inventory.read_text(encoding="utf-8"))
    document["runtimes"][0]["username"] = "alice"
    inventory.write_text(json.dumps(document), encoding="utf-8")
    fixture = _start(inventory, runtime)
    try:
        status, _, payload = fixture.client.get_json("/api/runtimes")
        assert status == 200
        row = payload["runtimes"][0]
        # The console shows the username ...
        assert row["username"] == "alice"
        assert row["display_name"] == "alice"
        # ... while plans, filters and API calls keep the runtime id.
        assert row["runtime_id"] == "rt-1"
    finally:
        fixture.stop()


def test_plan_install_has_no_side_effects(web: WebFixture, plugin_factory) -> None:
    bundle = plugin_factory(version="1.0.0")
    status, _, plan = web.client.post_json(
        "/api/plan",
        {
            "operation": "install",
            "filters": filters(),
            "params": {"sources": [str(bundle)], "mode": "sync"},
        },
    )
    assert status == 200, plan
    assert plan["summary"]["mutating"] == 1
    assert plan["actions"][0]["action"] == "install"
    assert plan["actions"][0]["target_version"] == "1.0.0"
    assert plan["bundles"][0]["id"] == "offline-market"
    assert len(plan["bundles"][0]["sha256"]) == 64
    assert web.runtime.plugins == {}


def test_run_install_and_poll(web: WebFixture, plugin_factory) -> None:
    bundle = plugin_factory(version="1.0.0")
    status, _, payload = web.client.post_json(
        "/api/run",
        {
            "operation": "install",
            "filters": filters(),
            "params": {"sources": [str(bundle)], "mode": "sync"},
        },
    )
    assert status == 202, payload
    job = web.client.wait_job(payload["job_id"])
    assert job["status"] == "success"
    assert job["summary"]["ok"] == 1
    assert job["results"][0]["plugin"] == "offline-market"
    assert job["results"][0]["state"] == "ok"
    assert web.runtime.plugins["offline-market"]["version"] == "1.0.0"

    status, _, listing = web.client.get_json("/api/jobs")
    assert status == 200
    assert listing["jobs"][0]["job_id"] == job["job_id"]


def test_plan_and_run_uninstall(web: WebFixture, plugin_factory) -> None:
    bundle = plugin_factory(version="1.0.0")
    install_job = web.client.post_json(
        "/api/run",
        {
            "operation": "install",
            "filters": filters(),
            "params": {"sources": [str(bundle)]},
        },
    )[2]["job_id"]
    assert web.client.wait_job(install_job)["status"] == "success"

    status, _, plan = web.client.post_json(
        "/api/plan",
        {
            "operation": "uninstall",
            "filters": filters(),
            "params": {"plugins": ["offline-market"]},
        },
    )
    assert status == 200, plan
    assert plan["actions"][0]["action"] == "uninstall"

    status, _, payload = web.client.post_json(
        "/api/run",
        {
            "operation": "uninstall",
            "filters": filters(),
            "params": {"plugins": ["offline-market"]},
        },
    )
    job = web.client.wait_job(payload["job_id"])
    assert job["status"] == "success"
    assert "offline-market" not in web.runtime.plugins


def test_toggle_plan(web: WebFixture, plugin_factory) -> None:
    bundle = plugin_factory(version="1.0.0")
    install_job = web.client.post_json(
        "/api/run",
        {
            "operation": "install",
            "filters": filters(),
            "params": {"sources": [str(bundle)]},
        },
    )[2]["job_id"]
    assert web.client.wait_job(install_job)["status"] == "success"
    job_id = web.client.post_json(
        "/api/run",
        {
            "operation": "disable",
            "filters": filters(),
            "params": {"plugins": ["offline-market"]},
        },
    )[2]["job_id"]
    assert web.client.wait_job(job_id)["status"] == "success"
    assert web.runtime.plugins["offline-market"]["enabled"] is False


def test_upload_then_install(web: WebFixture, plugin_factory) -> None:
    bundle = build_bundle_from_dir(plugin_factory(version="2.5.0"))
    status, _, payload = web.client.upload("bundle.zip", bundle.data)
    assert status == 200, payload
    upload_id = payload["upload_id"]
    assert payload["size"] == len(bundle.data)

    status, _, plan = web.client.post_json(
        "/api/plan",
        {
            "operation": "install",
            "filters": filters(),
            "params": {"sources": [f"@{upload_id}"]},
        },
    )
    assert status == 200, plan
    assert plan["actions"][0]["target_version"] == "2.5.0"

    job_id = web.client.post_json(
        "/api/run",
        {
            "operation": "install",
            "filters": filters(),
            "params": {"sources": [f"@{upload_id}"]},
        },
    )[2]["job_id"]
    assert web.client.wait_job(job_id)["status"] == "success"
    assert web.runtime.plugins["offline-market"]["version"] == "2.5.0"


def test_upload_rejects_non_zip(web: WebFixture) -> None:
    status, _, payload = web.client.upload("evil.zip", b"not a zip")
    assert status == 400
    assert "not a ZIP" in payload["error"]

    status, _, payload = web.client.upload("evil.txt", b"whatever")
    assert status == 400
    assert ".zip" in payload["error"]


def test_unknown_upload_reference(web: WebFixture) -> None:
    status, _, payload = web.client.post_json(
        "/api/plan",
        {
            "operation": "install",
            "filters": filters(),
            "params": {"sources": ["@up_missing"]},
        },
    )
    assert status == 400
    assert "unknown upload" in payload["error"]


def test_cross_origin_post_is_rejected(web: WebFixture) -> None:
    status, _, payload = web.client.post_json(
        "/api/plan",
        {"operation": "install", "filters": filters(), "params": {}},
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403
    assert "cross-origin" in payload["error"]


def test_json_content_type_required(web: WebFixture) -> None:
    status, _, raw = web.client.post_raw(
        "/api/plan",
        b"operation=install",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 400
    assert "application/json" in json.loads(raw)["error"]

    status, _, raw = web.client.post_raw(
        "/api/plan",
        b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert "invalid JSON" in json.loads(raw)["error"]


def test_invalid_operation_and_empty_selection(web: WebFixture) -> None:
    status, _, payload = web.client.post_json(
        "/api/plan",
        {"operation": "explode", "filters": filters(), "params": {}},
    )
    assert status == 400
    assert "operation must be" in payload["error"]

    status, _, payload = web.client.post_json(
        "/api/plan",
        {
            "operation": "install",
            "filters": {"runtime": ["no-such-runtime"]},
            "params": {"sources": ["."]},
        },
    )
    assert status == 400
    assert "no runtimes selected" in payload["error"]


def test_apply_inline_state(web: WebFixture, plugin_factory) -> None:
    bundle = plugin_factory(version="3.0.0")
    state = {
        "schema_version": 1,
        "plugins": [
            {
                "id": "offline-market",
                "version": "3.0.0",
                "source": str(bundle),
            },
        ],
    }
    status, _, plan = web.client.post_json(
        "/api/plan",
        {
            "operation": "apply",
            "filters": filters(),
            "params": {"state": state},
        },
    )
    assert status == 200, plan
    assert plan["actions"][0]["action"] == "install"

    job_id = web.client.post_json(
        "/api/run",
        {
            "operation": "apply",
            "filters": filters(),
            "params": {"state": state},
        },
    )[2]["job_id"]
    assert web.client.wait_job(job_id)["status"] == "success"
    assert web.runtime.plugins["offline-market"]["version"] == "3.0.0"

    # Drift check without a source: mismatched version fails the run.
    drift = {
        "schema_version": 1,
        "plugins": [{"id": "offline-market", "version": "9.9.9"}],
    }
    job_id = web.client.post_json(
        "/api/run",
        {
            "operation": "apply",
            "filters": filters(),
            "params": {"state": drift},
        },
    )[2]["job_id"]
    job = web.client.wait_job(job_id)
    assert job["status"] == "failed"
    assert "version drift" in job["results"][0]["message"]


def test_freeze_endpoint(web: WebFixture, plugin_factory) -> None:
    bundle = plugin_factory(version="1.1.0")
    job_id = web.client.post_json(
        "/api/run",
        {
            "operation": "install",
            "filters": filters(),
            "params": {"sources": [str(bundle)]},
        },
    )[2]["job_id"]
    web.client.wait_job(job_id)

    status, headers, raw = web.client.get("/api/freeze?runtime=rt-1")
    assert status == 200
    assert "attachment" in headers["Content-Disposition"]
    payload = json.loads(raw)
    assert payload["plugins"][0] == {
        "id": "offline-market",
        "version": "1.1.0",
        "enabled": True,
        "source": "",
    }

    status, _, error = web.client.get_json("/api/freeze?runtime=missing")
    assert status == 400
    assert "matched 0" in error["error"]


def test_second_job_is_rejected(web: WebFixture, plugin_factory) -> None:
    web.server.ctx.active_job_id = "someone-else"
    bundle = plugin_factory(version="1.0.0")
    status, _, payload = web.client.post_json(
        "/api/run",
        {
            "operation": "install",
            "filters": filters(),
            "params": {"sources": [str(bundle)]},
        },
    )
    assert status == 409
    assert "already running" in payload["error"]


def test_unknown_paths(web: WebFixture) -> None:
    status, _, payload = web.client.get_json("/api/nope")
    assert status == 404
    assert payload["error"] == "not found"

    status, _, _ = web.client.get("/nope", token=False)
    assert status == 404

    status, _, payload = web.client.post_json("/api/nope", {})
    assert status == 404


def test_no_token_mode(runtime_factory, inventory_file) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    fixture = _start(inventory, runtime, token_required=False, token="")
    try:
        status, _, payload = fixture.client.get_json("/api/meta", token=False)
        assert status == 200
        assert payload["token_required"] is False
    finally:
        fixture.stop()


def test_plan_warns_when_runtime_is_unreachable(
    runtime_factory,
    inventory_file,
    plugin_factory,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    runtime.stop()
    fixture = _start(inventory, runtime)
    try:
        bundle = plugin_factory(version="1.0.0")
        status, _, plan = fixture.client.post_json(
            "/api/plan",
            {
                "operation": "install",
                "filters": {"runtime": ["rt-1"], "include_stopped": True},
                "params": {"sources": [str(bundle)]},
            },
        )
        assert status == 200, plan
        assert plan["actions"][0]["action"] == "runtime"
        assert plan["summary"]["mutating"] == 0
        assert plan["warnings"]
    finally:
        fixture.stop()


def test_dry_run_console_never_changes_a_runtime(
    runtime_factory,
    inventory_file,
    plugin_factory,
) -> None:
    runtime = runtime_factory("rt-1")
    inventory = inventory_file([runtime])
    config = WebConfig(
        discovery=DiscoveryOptions(inventory=inventory),
        exec_options=ExecOptions(dry_run=True, retries=1),
        token="dry-token",
        workdir=inventory.parent / "dry-uploads",
    )
    fixture = WebFixture(create_server(config), config.token, runtime)
    try:
        status, _, meta = fixture.client.get_json("/api/meta")
        assert status == 200
        assert meta["execution"]["dry_run"] is True

        bundle = plugin_factory(version="1.0.0")
        job_id = fixture.client.post_json(
            "/api/run",
            {
                "operation": "install",
                "filters": filters(),
                "params": {"sources": [str(bundle)]},
            },
        )[2]["job_id"]
        job = fixture.client.wait_job(job_id)
        assert job["status"] == "success"
        assert job["results"][0]["state"] == "planned"
        assert runtime.plugins == {}
    finally:
        fixture.stop()


# ── bind address policy ──────────────────────────────────────────────────


def test_bind_host_defaults_to_loopback_when_token_is_required() -> None:
    assert resolve_bind_host(None, token_required=True) == "127.0.0.1"
    assert resolve_bind_host("", token_required=True) == "127.0.0.1"
    assert resolve_bind_host("   ", token_required=True) == "127.0.0.1"


def test_no_token_listens_on_every_interface() -> None:
    assert resolve_bind_host(None, token_required=False) == "0.0.0.0"
    assert resolve_bind_host("", token_required=False) == "0.0.0.0"


def test_explicit_host_wins_over_the_no_token_default() -> None:
    assert (
        resolve_bind_host("127.0.0.1", token_required=False) == "127.0.0.1"
    )
    assert resolve_bind_host("10.1.2.3", token_required=False) == "10.1.2.3"
    assert resolve_bind_host("10.1.2.3", token_required=True) == "10.1.2.3"
