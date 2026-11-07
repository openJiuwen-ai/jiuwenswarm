"""The Web UI server answers the startup splash hint locally (Gateway F_12)."""

from __future__ import annotations

import http.client
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer

from jiuwenswarm.channels.web import app_web
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler, _gateway_install_dir, _splash_candidates

def _install(root, package_id, slot, *, versioned=True):
    directory = root / package_id / "1.0.0" if versioned else root / package_id
    directory.mkdir(parents=True)
    manifest = {
        "schemaVersion": 1, "package_type": "extension",
        "package": {"id": package_id, "version": "1.0.0", "domain": "gateway"},
        "ui": {"apiVersion": 1, "items": [{"type": "web.slot.contribution", "id": f"{package_id}.view", "slot": slot}]},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_install_dir_follows_the_gateway_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    assert _gateway_install_dir() == tmp_path / "extension_pkgs"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("extensions:\n  gateway:\n    install_dir: custom_pkgs\n")
    assert _gateway_install_dir() == tmp_path / "custom_pkgs"
    (tmp_path / "config" / "config.yaml").write_text(f"extensions:\n  gateway:\n    install_dir: {tmp_path / 'abs'}\n")
    assert _gateway_install_dir() == tmp_path / "abs"
    (tmp_path / "config" / "config.yaml").write_text("extensions: [broken")
    assert _gateway_install_dir() == tmp_path / "extension_pkgs"


def test_candidates_follow_installed_manifests(tmp_path):
    root = tmp_path / "extension_pkgs"
    assert _splash_candidates(root) is False
    _install(root, "test.footer", "application.sidebar.footer")
    assert _splash_candidates(root) is False
    _install(root, "test.splash", "application.splash", versioned=False)
    assert _splash_candidates(root) is True


def test_hint_is_served_by_the_web_server(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    _install(tmp_path / "extension_pkgs", "test.splash", "application.splash")

    class _Handler(_SpaStaticHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(tmp_path)))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.request("GET", app_web.SPLASH_HINT_PATH)
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"candidates": True}
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
