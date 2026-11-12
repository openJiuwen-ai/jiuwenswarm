"""The desktop's office add-in routes and the HTTPS listener, through the real web handler."""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from jiuwenswarm.channels.desktop.office_addins import OfficeAddins
from jiuwenswarm.channels.desktop.office_addins.certificate import MacTrustStore, TlsFiles
from jiuwenswarm.channels.desktop.office_addins.listener import TlsListener
from jiuwenswarm.channels.desktop.office_addins.powerpoint import MacRegistration
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

ROOT = Path(__file__).resolve().parents[4]
TOKEN = "desktop-token"
COOKIE = f"__wsdt={TOKEN}"
# What the desktop window's own page sends with a POST.
PAGE = {"Cookie": COOKIE, "Sec-Fetch-Site": "same-origin"}
_UPGRADE_RESPONSE = (
    b"HTTP/1.1 101 Switching Protocols\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Accept: test\r\n\r\n"
)


def _trusting_keychain(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), 0, "", "")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Desktop:
    http_port: int
    listener: TlsListener
    home: Path
    tls: TlsFiles


@contextmanager
def _upstream_gateway() -> Iterator[int]:
    """Answers one websocket upgrade with 101."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            conn.sendall(_UPGRADE_RESPONSE)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield int(server.getsockname()[1])
    finally:
        server.close()
        thread.join(timeout=5)


@contextmanager
def _desktop(tmp_path: Path, *, desktop_mode: bool = True) -> Iterator[Desktop]:
    dist = tmp_path / "dist"
    (dist / "office").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (dist / "office" / "taskpane.powerpoint.html").write_text("<!doctype html><title>pane</title>", encoding="utf-8")
    shutil.copy(
        ROOT / "packages" / "jiuwenswarm-office-addin" / "manifest.powerpoint.xml",
        dist / "office" / "manifest.powerpoint.xml",
    )
    home = tmp_path / "home"
    (home / "Library" / "Containers" / "com.microsoft.Powerpoint").mkdir(parents=True)

    class Handler(_SpaStaticHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

    with _upstream_gateway() as gateway_port:
        Handler.ws_target = f"ws://127.0.0.1:{gateway_port}"
        handler = partial(Handler, directory=str(dist))
        listener = TlsListener(_free_port(), handler)
        tls = TlsFiles(tmp_path / "data" / "office-addin" / "tls")
        if desktop_mode:
            Handler.desktop_token = TOKEN
            Handler.desktop_cookie_name = "__wsdt"
            Handler.office_addins = OfficeAddins(
                tls=tls,
                trust=MacTrustStore(home, runner=_trusting_keychain),
                registrations={"powerpoint": MacRegistration(home)},
                listener=listener,
                office_dist=dist / "office",
            )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield Desktop(int(server.server_port), listener, home, tls)
        finally:
            listener.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _request(port: int, method: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict | None]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        is_json = response.getheader("Content-Type", "").startswith("application/json")
        return response.status, json.loads(body) if is_json else None
    finally:
        connection.close()


def _tls_context(tls: TlsFiles) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(tls.ca))


def test_routes_need_the_desktop_cookie(tmp_path: Path) -> None:
    with _desktop(tmp_path) as desktop:
        assert _request(desktop.http_port, "GET", "/desktop-api/office-addins")[0] == 404
        assert _request(desktop.http_port, "POST", "/desktop-api/office-addins/office/install")[0] == 404
        assert not MacRegistration(desktop.home).path.exists()

        status, body = _request(desktop.http_port, "GET", "/desktop-api/office-addins", {"Cookie": COOKIE})
        assert status == 200
        assert body == {"suites": {"office": {"installed": False, "problems": [], "apps": {"powerpoint": False}}}}


@pytest.mark.parametrize("site", [{"Sec-Fetch-Site": "same-site"}, {}], ids=["another page", "no fetch metadata"])
def test_only_the_desktop_page_can_install(tmp_path: Path, site: dict[str, str]) -> None:
    with _desktop(tmp_path) as desktop:
        status, _ = _request(
            desktop.http_port,
            "POST",
            "/desktop-api/office-addins/office/install",
            {"Cookie": COOKIE, **site},
        )
        assert status == 404
        assert not MacRegistration(desktop.home).path.exists()


@pytest.mark.parametrize(
    "path",
    [
        "/desktop-api/office-addins/wps/install",
        "/desktop-api/office-addins/powerpoint/install",
        "/desktop-api/office-addins/office/x",
        "/desktop-api/office-addins/office/powerpoint/open",
        "/desktop-api/office-addins/office/powerpoint/install",
    ],
)
def test_unknown_suite_app_or_action_is_not_found(tmp_path: Path, path: str) -> None:
    with _desktop(tmp_path) as desktop:
        assert _request(desktop.http_port, "POST", path, PAGE)[0] == 404


def test_install_serves_the_pane_over_https_and_tunnels_ws(tmp_path: Path) -> None:
    with _desktop(tmp_path) as desktop:
        status, body = _request(
            desktop.http_port, "POST", "/desktop-api/office-addins/office/install", PAGE,
        )
        assert (status, body) == (200, {"installed": True, "problems": [], "apps": {"powerpoint": True}})
        manifest = MacRegistration(desktop.home).path.read_text(encoding="utf-8")
        assert f"{desktop.listener.origin}/office/taskpane.powerpoint.html" in manifest

        pane = http.client.HTTPSConnection("localhost", desktop.listener.port, context=_tls_context(desktop.tls))
        pane.request("GET", "/office/taskpane.powerpoint.html")
        response = pane.getresponse()
        assert (response.status, response.read()) == (200, b"<!doctype html><title>pane</title>")
        pane.close()

        raw = socket.create_connection(("127.0.0.1", desktop.listener.port), timeout=10)
        with _tls_context(desktop.tls).wrap_socket(raw, server_hostname="localhost") as client:
            client.sendall(
                (
                    "GET /ws HTTP/1.1\r\n"
                    f"Host: localhost:{desktop.listener.port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
            )
            assert client.recv(4096).split(b"\r\n", 1)[0] == b"HTTP/1.1 101 Switching Protocols"


def test_a_silent_preconnect_does_not_block_the_pane(tmp_path: Path) -> None:
    with _desktop(tmp_path) as desktop:
        _request(desktop.http_port, "POST", "/desktop-api/office-addins/office/install", PAGE)
        with socket.create_connection(("127.0.0.1", desktop.listener.port), timeout=10):
            pane = http.client.HTTPSConnection(
                "localhost", desktop.listener.port, context=_tls_context(desktop.tls), timeout=5,
            )
            pane.request("GET", "/office/taskpane.powerpoint.html")
            assert pane.getresponse().status == 200
            pane.close()


def test_remove_stops_the_https_listener(tmp_path: Path) -> None:
    with _desktop(tmp_path) as desktop:
        _request(desktop.http_port, "POST", "/desktop-api/office-addins/office/install", PAGE)
        status, body = _request(
            desktop.http_port, "POST", "/desktop-api/office-addins/office/remove", PAGE,
        )
        assert (status, body) == (200, {"installed": False, "problems": [], "apps": {"powerpoint": False}})
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", desktop.listener.port), timeout=2).close()


def test_routes_do_not_exist_outside_desktop_mode(tmp_path: Path) -> None:
    with _desktop(tmp_path, desktop_mode=False) as desktop:
        assert _request(desktop.http_port, "GET", "/desktop-api/office-addins", {"Cookie": COOKIE})[0] == 404
        assert _request(
            desktop.http_port, "POST", "/desktop-api/office-addins/office/install", PAGE,
        )[0] == 404
