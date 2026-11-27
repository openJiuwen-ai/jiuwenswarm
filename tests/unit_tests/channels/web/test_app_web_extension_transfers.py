"""The Web UI server forwards Gateway file import and export, streaming upload bodies (Gateway F_13)."""

from __future__ import annotations

import base64
import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.common import system_plugin_recovery
from jiuwenswarm.common.system_plugin_recovery import SystemPluginRecovery
from jiuwenswarm.common.system_plugin_recovery_sidecar import _RecoveryServer

UPLOAD_PATH = "/extension-uploads/" + "a" * 32 + "/theme.zip"
DOWNLOAD_PATH = "/extension-downloads/" + "b" * 32
HALF = 256 * 1024


@contextmanager
def _serve(handler) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _upstream(seen: list[dict], first_bytes: threading.Event):
    class _Upstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

        def do_PUT(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            first = self.rfile.read(HALF)
            first_bytes.set()
            rest = self.rfile.read(length - len(first))
            seen.append({"method": "PUT", "path": self.path, "body": first + rest})
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            seen.append({"method": "GET", "path": self.path})
            body = b"backup-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''backup.zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Upstream


@contextmanager
def _proxy(upstream_port: int, directory: Path, *, remote: bool = False) -> Iterator[int]:
    class _Proxy(_SpaStaticHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

        def setup(self) -> None:
            super().setup()
            if remote:
                self.client_address = ("203.0.113.9", 40000)

    _Proxy.api_target = f"http://127.0.0.1:{upstream_port}"
    with _serve(partial(_Proxy, directory=str(directory))) as port:
        yield port


@contextmanager
def _recovery_sidecar(recovery: SystemPluginRecovery, token: str) -> Iterator[int]:
    server = _RecoveryServer(("127.0.0.1", 0), recovery, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_import_bodies_are_streamed_to_the_gateway(tmp_path):
    seen: list[dict] = []
    first_bytes = threading.Event()
    body = bytes(range(256)) * (2 * HALF // 256)
    with _serve(_upstream(seen, first_bytes)) as upstream, _proxy(upstream, tmp_path) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.putrequest("PUT", UPLOAD_PATH)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        connection.send(body[:HALF])
        # A buffering proxy would hold the first half until the whole body arrived.
        assert first_bytes.wait(5)
        connection.send(body[HALF:])
        response = connection.getresponse()
        response.read()
        connection.close()
    assert response.status == 204
    assert seen == [{"method": "PUT", "path": UPLOAD_PATH, "body": body}]


def test_downloads_are_forwarded_with_their_headers(tmp_path):
    seen: list[dict] = []
    with _serve(_upstream(seen, threading.Event())) as upstream, _proxy(upstream, tmp_path) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", DOWNLOAD_PATH)
        response = connection.getresponse()
        content = response.read()
        connection.close()
    assert response.status == 200 and content == b"backup-bytes"
    assert response.getheader("Content-Disposition") == "attachment; filename*=UTF-8''backup.zip"
    assert seen == [{"method": "GET", "path": DOWNLOAD_PATH}]


def test_remote_clients_can_import_and_export(tmp_path):
    seen: list[dict] = []
    body = bytes(range(256)) * (2 * HALF // 256)
    with _serve(_upstream(seen, threading.Event())) as upstream, _proxy(upstream, tmp_path, remote=True) as port:
        statuses = []
        for method, path, payload in (("PUT", UPLOAD_PATH, body), ("GET", DOWNLOAD_PATH, None)):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request(method, path, body=payload)
            response = connection.getresponse()
            response.read()
            connection.close()
            statuses.append(response.status)
    assert statuses == [204, 200]
    assert seen == [{"method": "PUT", "path": UPLOAD_PATH, "body": body}, {"method": "GET", "path": DOWNLOAD_PATH}]


@pytest.mark.parametrize("public_host", ["203.0.113.10:5173", "app.example:5173"])
def test_remote_http_can_restore_without_configuring_credentials(tmp_path, monkeypatch, public_host):
    monkeypatch.delenv("JIUWENSWARM_REMOTE_RECOVERY_PASSWORD", raising=False)
    monkeypatch.delenv(system_plugin_recovery._RESTART_SIGNAL_PATH_ENV, raising=False)
    recovery = SystemPluginRecovery(tmp_path / "plugins", tmp_path / "recovery")
    monkeypatch.setattr(system_plugin_recovery, "SystemPluginRecovery", lambda: recovery)
    record_id = recovery.begin_session("test-session")["id"]
    token = "test-private-sidecar-token"
    mount = "/system-plugin-recovery"

    with _recovery_sidecar(recovery, token) as sidecar_port:
        recovery.publish_endpoint(f"http://127.0.0.1:{sidecar_port}{mount}?token={token}", "test-session")
        with _proxy(1, tmp_path, remote=True) as port, closing(
            http.client.HTTPConnection("127.0.0.1", port, timeout=10),
        ) as connection:
            for path, expected in (
                ("/endpoint", {"url": "./proxy"}),
                ("/api/history", {"records": recovery.records()}),
                (f"/api/preview?record_id={record_id}", {"packages": []}),
            ):
                connection.request("GET", mount + path, headers={"Host": public_host})
                response = connection.getresponse()
                payload = response.read()
                assert response.status == 200
                assert json.loads(payload) == expected
                assert token.encode() not in payload

            for path in ("/", "/proxy"):
                connection.request("GET", mount + path, headers={"Host": public_host})
                response = connection.getresponse()
                assert response.status == 200
                assert response.getheader("Content-Type", "").startswith("text/html")
                page = response.read()
                assert b'id="confirm-restore"' in page
                assert token.encode() not in page

            connection.request("GET", f"{mount}/api/restore", headers={"Host": public_host})
            response = connection.getresponse()
            assert response.status == 405
            response.read()

            connection.request(
                "POST", f"{mount}/api/restore",
                body=json.dumps({"record_id": record_id}),
                headers={
                    "Host": public_host, "Origin": f"http://{public_host}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            payload = response.read()
            assert response.status == 200
            assert json.loads(payload)["restart_requested"] is True
            assert token.encode() not in payload

    records = recovery.records()
    assert [record["action"] for record in records] == ["restore", "startup"]
    assert recovery.restart_path.exists()


def test_standalone_recovery_restores_and_requests_a_restart_without_spa_or_gateway(tmp_path, monkeypatch):
    # An empty frontend directory and a deliberately unavailable Gateway.
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    installed = tmp_path / "plugins"
    recovery_data = tmp_path / "recovery"
    recovery = SystemPluginRecovery(installed, recovery_data)
    now = 100.0
    monkeypatch.setattr(system_plugin_recovery.time, "time", lambda: now)
    monkeypatch.delenv(system_plugin_recovery._RESTART_SIGNAL_PATH_ENV, raising=False)
    monkeypatch.setattr(system_plugin_recovery, "SystemPluginRecovery", lambda: recovery)
    monkeypatch.setenv("JIUWENSWARM_REMOTE_RECOVERY_PASSWORD", "test-password")
    record_id = recovery.begin_session("test-session")["id"]
    package = installed / "demo.late" / "1.0.0"
    package.mkdir(parents=True)
    manifest = package / "manifest.json"
    manifest.write_text(json.dumps({
        "package_type": "extension",
        "schemaVersion": 1,
        "package": {"id": "demo.late", "version": "1.0.0", "domain": "agentserver"},
        "enable": True,
    }), encoding="utf-8")

    token = "private-token"
    credentials = base64.b64encode(b"recovery:test-password").decode("ascii")
    headers = {"Host": "app.example", "X-Forwarded-Proto": "https", "Authorization": f"Basic {credentials}"}
    mount = "/portal/system-plugin-recovery"

    with _recovery_sidecar(recovery, token) as sidecar_port:
        local_url = f"http://127.0.0.1:{sidecar_port}/system-plugin-recovery?token={token}"
        recovery.publish_endpoint(local_url, "test-session")
        with _proxy(1, frontend) as port, closing(http.client.HTTPConnection("127.0.0.1", port, timeout=10)) as connection:
            connection.request("GET", f"{mount}/endpoint")
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {"url": local_url}

            connection.request("GET", f"{mount}/endpoint", headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {"url": "./proxy"}

            connection.request(
                "GET", f"{mount}/?locale=en&parent_origin=https%3A%2F%2Fignored.example&proxy_base=%2Fignored",
                headers=headers,
            )
            response = connection.getresponse()
            page = response.read()
            assert response.status == 200
            assert response.getheader("Content-Type", "").startswith("text/html")
            assert response.getheader("Cache-Control") == "no-store"
            policy = response.getheader("Content-Security-Policy", "")
            assert "allow-same-origin" in policy
            assert "frame-ancestors 'self'" in policy
            assert b'<button id="restore" type="button">' in page
            assert b'id="confirm-restore"' in page
            assert b'<html lang="en">' in page
            assert b"const parentOrigin = false" in page
            assert b'const token = "";' in page
            assert b"window.location.pathname" in page
            assert token.encode() not in page
            assert b"ignored.example" not in page
            assert b"/ignored" not in page

            connection.request("GET", f"{mount}/proxy", headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            embedded = response.read()
            assert b"const parentOrigin = true" in embedded
            assert token.encode() not in embedded

            connection.request("GET", f"{mount}/api/history", headers=headers)
            response = connection.getresponse()
            history = json.loads(response.read())
            assert response.status == 200
            assert [record["id"] for record in history["records"]] == [record_id]

            connection.request("GET", f"{mount}/api/preview?record_id={record_id}", headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {
                "packages": [{"package_id": "demo.late", "version": "1.0.0", "enabled": False}],
            }
            assert json.loads(manifest.read_text(encoding="utf-8"))["enable"] is True

            body = json.dumps({"record_id": record_id}).encode("utf-8")
            restore_headers = {**headers, "Content-Type": "application/json"}
            connection.request(
                "POST", f"{mount}/api/restore",
                headers={**restore_headers, "Origin": "https://foreign.example"},
            )
            response = connection.getresponse()
            assert response.status == 403
            response.read()
            assert json.loads(manifest.read_text(encoding="utf-8"))["enable"] is True
            assert not recovery.restart_path.exists()

            connection.request(
                "POST", f"{mount}/api/restore", body=body,
                headers={**restore_headers, "Origin": "https://app.example"},
            )
            response = connection.getresponse()
            restored = json.loads(response.read())
            assert response.status == 200
            assert restored["record"]["action"] == "restore"
            assert restored["restart_requested"] is True

    assert json.loads(manifest.read_text(encoding="utf-8"))["enable"] is False
    assert recovery.consume_backend_restart("test-session") is False
    now += system_plugin_recovery._RESTART_DELAY_SECONDS
    assert recovery.consume_backend_restart("test-session") is True
    assert recovery.consume_backend_restart("test-session") is False
