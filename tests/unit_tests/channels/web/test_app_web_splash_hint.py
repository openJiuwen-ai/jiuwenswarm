"""The Web UI server answers the startup splash hint locally (Gateway F_12, S_09 section 4)."""

from __future__ import annotations

import http.client
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer

from jiuwenswarm.channels.web import app_web
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig, load_gateway_config
from jiuwenswarm.gateway.extensions_v2.splash_hint import installed_splash_providers


def _install(root, package_id, items, *, versioned=True, domain="gateway"):
    directory = root / package_id / "1.0.0" if versioned else root / package_id
    directory.mkdir(parents=True)
    manifest = {
        "schemaVersion": 1, "package_type": "extension",
        "package": {"id": package_id, "version": "1.0.0", "domain": domain},
        "extensions": [{"id": "main", "runtime": "backend", "module": "extension.py"}],
        "ui": {"apiVersion": 1, "items": items},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "extension.py").write_text("def create_extension():\n    raise AssertionError('not run')\n",
                                            encoding="utf-8")


def _slot(slot, **extra):
    return {"type": "web.slot.contribution", "id": f"item.{slot}", "slot": slot, **extra}


def test_candidates_follow_the_gateway_rules(tmp_path):
    config = GatewayExtensionConfig(install_dir=tmp_path / "pkgs", data_dir=tmp_path / "data")
    assert installed_splash_providers(config) == []
    _install(config.install_dir, "test.footer", [_slot("application.sidebar.footer")])
    # Text that only mentions the slot is not a declaration.
    _install(config.install_dir, "test.mention", [_slot("application.background", title="application.splash")])
    _install(config.install_dir, "test.agent", [_slot("application.splash")], domain="agentserver")
    _install(config.install_dir, "test.invalid", [_slot("application.splash", splash={"maxDurationMs": 10})])
    assert installed_splash_providers(config) == []
    _install(config.install_dir, "test.splash", [_slot("application.splash")], versioned=False)
    _install(config.install_dir, "test.off", [_slot("application.splash")])
    (tmp_path / "data" / "host").mkdir(parents=True)
    (tmp_path / "data" / "host" / "splash.json").write_text(json.dumps(
        {"version": 1, "installed": ["test.splash", "test.off"], "order": None, "disabled": ["test.off"]}))
    assert installed_splash_providers(config) == ["test.splash"]
    assert installed_splash_providers(GatewayExtensionConfig(enabled=False, install_dir=config.install_dir)) == []


def test_the_hint_uses_the_gateway_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    install_dir = load_gateway_config({}).install_dir
    seen = []
    monkeypatch.setattr("jiuwenswarm.gateway.extensions_v2.splash_hint.load_gateway_config",
                        lambda: seen.append(True) or GatewayExtensionConfig(install_dir=tmp_path / "custom"))
    _install(tmp_path / "custom", "test.splash", [_slot("application.splash")])
    assert app_web._splash_candidates() is True and seen == [True]
    assert install_dir != tmp_path / "custom"


def test_hint_is_served_by_the_web_server(tmp_path, monkeypatch):
    monkeypatch.setattr(app_web, "_splash_candidates", lambda: True)

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
