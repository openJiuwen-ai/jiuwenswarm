# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版才挂 CORE/mapped；personal 不挂；mapped 企业剔 A2A ingress。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
MAPPED_PATH = ROOT / "gateway" / "channel_manager" / "web" / "http_routes" / "mapped.py"
OUTBOUND_PATH = ROOT / "gateway" / "channel_manager" / "web" / "outbound.py"
TIMEOUTS_PATH = ROOT / "gateway" / "channel_manager" / "web" / "web_http_timeouts.py"
HTTP_PATH = ROOT / "gateway" / "channel_manager" / "web" / "web_http_routes.py"
CHANNEL_APP_PATH = ROOT / "gateway" / "channel_manager" / "web" / "web_channel_app.py"


def _load_module(mod_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mapped_mod() -> ModuleType:
    return _load_module("jw_mapped_edition_ut", MAPPED_PATH)


def test_mapped_routes_for_edition_strips_a2a_ingress(mapped_mod: ModuleType) -> None:
    personal = mapped_mod.mapped_routes_for_edition(enterprise=False)
    enterprise = mapped_mod.mapped_routes_for_edition(enterprise=True)
    assert len(personal) > len(enterprise)
    assert any("/a2a/ingress" in r.path for r in personal)
    assert not any("/a2a/ingress" in r.path for r in enterprise)
    assert not any("/a2a/outbound" in r.path for r in personal)
    assert not any("/a2a/outbound" in r.path for r in enterprise)


def test_register_http_routes_personal_skips_core(monkeypatch: pytest.MonkeyPatch) -> None:
    ge = ModuleType("gateway.enterprise")
    ge.is_enterprise = lambda: False  # type: ignore[attr-defined]
    ge.try_build_enterprise_plane = lambda: None  # type: ignore[attr-defined]
    ge.EnterprisePlaneError = type(  # type: ignore[attr-defined]
        "EnterprisePlaneError", (RuntimeError,), {}
    )
    monkeypatch.setitem(sys.modules, "gateway.enterprise", ge)

    for pkg in (
        "jiuwenswarm",
        "gateway",
        "gateway.channel_manager",
        "gateway.channel_manager.web",
    ):
        if pkg not in sys.modules:
            m = ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, pkg, m)

    # Stub baseline mounts used by web_channel_app.register_http_routes.
    cfile = ModuleType("gateway.channel_manager.web.container_file_http")
    cfile.attach_container_file_routes = lambda app, channel: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, cfile.__name__, cfile)

    traj = ModuleType("gateway.channel_manager.web.trajectory_http")
    traj.attach_trajectory_routes = lambda app, channel: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, traj.__name__, traj)

    diagnosis = ModuleType("gateway.channel_manager.web.diagnosis_http")
    diagnosis.attach_diagnosis_routes = lambda app, channel: None
    monkeypatch.setitem(sys.modules, diagnosis.__name__, diagnosis)

    files = ModuleType("gateway.channel_manager.web.enterprise_files")
    files.register_enterprise_file_routes = lambda app, channel: None
    monkeypatch.setitem(sys.modules, files.__name__, files)

    auth = ModuleType("gateway.channel_manager.web.web_http_auth")
    auth.register_auth_routes = lambda app: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, auth.__name__, auth)

    # security.ws_origin used at import of web_channel_app
    sec = ModuleType("gateway.common.security.ws_origin")
    sec.get_header_value = lambda *a, **k: None  # type: ignore[attr-defined]
    sec.is_allowed_browser_origin = lambda *a, **k: True  # type: ignore[attr-defined]
    sec.is_origin_check_enabled = lambda: False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jiuwenswarm.common.security", ModuleType("jiuwenswarm.common.security"))
    monkeypatch.setitem(sys.modules, sec.__name__, sec)

    common = ModuleType("jiuwenswarm.common")
    common.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jiuwenswarm.common", common)

    adapter = ModuleType("gateway.channel_manager.web.ws_connection_adapter")
    adapter.StarletteWsAdapter = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)

    ext_host = ModuleType("gateway.extensions.application_host")
    ext_host.iter_websocket_routes = lambda *_a, **_k: []  # type: ignore[attr-defined]
    ext_host.mount_application_plugin_http_routes = lambda *_a, **_k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jiuwenswarm.extensions", ModuleType("jiuwenswarm.extensions"))
    monkeypatch.setitem(sys.modules, ext_host.__name__, ext_host)

    channel_app = _load_module("jw_web_channel_app_edition_ut", CHANNEL_APP_PATH)
    app = FastAPI()
    channel_app.register_http_routes(app, MagicMock())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/v1/chat/completions" not in paths
    assert "/api/v1/health" not in paths
    assert "/api/v1/catalog" not in paths


def test_register_http_routes_enterprise_mounts_core(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakePlane:
        def install_web_channel(self, app: FastAPI, channel: object) -> None:
            from gateway.channel_manager.web.web_http_routes import (
                register_web_http_api_routes,
            )
            from gateway.channel_manager.web.enterprise_files import (
                register_enterprise_file_routes,
            )

            register_web_http_api_routes(app, channel, enterprise=True)
            register_enterprise_file_routes(app, channel)

    ge = ModuleType("gateway.enterprise")
    ge.is_enterprise = lambda: True  # type: ignore[attr-defined]
    ge.try_build_enterprise_plane = lambda: _FakePlane()  # type: ignore[attr-defined]
    ge.EnterprisePlaneError = type(  # type: ignore[attr-defined]
        "EnterprisePlaneError", (RuntimeError,), {}
    )
    monkeypatch.setitem(sys.modules, "gateway.enterprise", ge)

    for pkg in (
        "jiuwenswarm",
        "gateway",
        "gateway.channel_manager",
        "gateway.channel_manager.web",
        "jiuwenswarm.common",
        "jiuwenswarm.common.security",
        "jiuwenswarm.extensions",
    ):
        if pkg not in sys.modules:
            m = ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, pkg, m)

    cfile = ModuleType("gateway.channel_manager.web.container_file_http")
    cfile.attach_container_file_routes = lambda app, channel: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, cfile.__name__, cfile)

    traj = ModuleType("gateway.channel_manager.web.trajectory_http")
    traj.attach_trajectory_routes = lambda app, channel: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, traj.__name__, traj)

    diagnosis = ModuleType("gateway.channel_manager.web.diagnosis_http")
    diagnosis.attach_diagnosis_routes = lambda app, channel: None
    monkeypatch.setitem(sys.modules, diagnosis.__name__, diagnosis)

    files = ModuleType("gateway.channel_manager.web.enterprise_files")
    files.register_enterprise_file_routes = lambda app, channel: None
    monkeypatch.setitem(sys.modules, files.__name__, files)

    auth = ModuleType("gateway.channel_manager.web.web_http_auth")
    auth.register_auth_routes = lambda app: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, auth.__name__, auth)

    sec = ModuleType("gateway.common.security.ws_origin")
    sec.get_header_value = lambda *a, **k: None  # type: ignore[attr-defined]
    sec.is_allowed_browser_origin = lambda *a, **k: True  # type: ignore[attr-defined]
    sec.is_origin_check_enabled = lambda: False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, sec.__name__, sec)

    adapter = ModuleType("gateway.channel_manager.web.ws_connection_adapter")
    adapter.StarletteWsAdapter = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)

    ext_host = ModuleType("gateway.extensions.application_host")
    ext_host.iter_websocket_routes = lambda *_a, **_k: []  # type: ignore[attr-defined]
    ext_host.mount_application_plugin_http_routes = lambda *_a, **_k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, ext_host.__name__, ext_host)

    # Stub dispatch before loading web_http_routes.
    dispatch_mod = ModuleType("gateway.channel_manager.web.web_http_dispatch")
    dispatch_mod.dispatch_http_request = MagicMock()  # type: ignore[attr-defined]
    dispatch_mod._trust_client_tenant_headers = lambda client_host=None: True  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, dispatch_mod.__name__, dispatch_mod)

    routes_pkg = ModuleType("gateway.channel_manager.web.http_routes")
    routes_pkg.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, routes_pkg.__name__, routes_pkg)
    share_jobs = ModuleType(routes_pkg.__name__ + ".share_jobs")
    share_jobs.register_share_job_routes = lambda app: None
    monkeypatch.setitem(sys.modules, share_jobs.__name__, share_jobs)

    # Manager routes have independent mTLS tests; isolate this mount contract.
    manager = ModuleType("gateway.manager_config")
    manager.register_manager_config_routes = lambda app: None
    monkeypatch.setitem(sys.modules, manager.__name__, manager)

    # File behavior has separate tests against the AgentServer adapter.
    files = ModuleType("gateway.channel_manager.web.enterprise_files")
    files.register_enterprise_file_routes = lambda app, channel: None
    monkeypatch.setitem(sys.modules, files.__name__, files)
    monkeypatch.delenv("GATEWAY_IDENTITY_HTTP_URL", raising=False)
    monkeypatch.delenv("GATEWAY_MANAGER_HTTP_URL", raising=False)

    mapped = _load_module(
        "gateway.channel_manager.web.http_routes.mapped",
        MAPPED_PATH,
    )
    routes_pkg.mapped_routes_for_edition = mapped.mapped_routes_for_edition  # type: ignore[attr-defined]
    routes_pkg.catalog_entries = mapped.catalog_entries  # type: ignore[attr-defined]
    routes_pkg.WebHttpMappedRoute = mapped.WebHttpMappedRoute  # type: ignore[attr-defined]

    _load_module("gateway.channel_manager.web.web_http_timeouts", TIMEOUTS_PATH)
    _load_module("gateway.channel_manager.web.outbound", OUTBOUND_PATH)
    web_http = _load_module(
        "gateway.channel_manager.web.web_http_routes",
        HTTP_PATH,
    )

    channel_app = _load_module("jw_web_channel_app_ent_ut", CHANNEL_APP_PATH)
    app = FastAPI()
    from types import SimpleNamespace
    channel_app.register_http_routes(app, SimpleNamespace(config=SimpleNamespace(path="/ws")))
    assert not app.user_middleware
    client = TestClient(app)
    assert client.get("/api/v1/health").status_code == 200
    catalog = client.get("/api/v1/catalog").json()
    paths = [r["path"] for r in catalog["data"]["routes"]]
    assert "/api/v1/chat/completions" in paths
    assert not any(p.startswith("/api/v1/a2a/ingress") for p in paths)
    assert not any(p.startswith("/api/v1/a2a/outbound") for p in paths)
    assert "/api/v1/chat/{session_id}/actions/steer" not in paths
    assert "/api/v1/workspace/download" not in paths
    assert "/api/sessions" not in paths

    # These were blocked by the removed middleware before dispatch, even when
    # the deployment deliberately omitted built-in Identity.
    outbound = SimpleNamespace(wait_response=AsyncMock(return_value={"ok": True, "payload": {}}))
    dispatch = AsyncMock(return_value=(outbound, "request", "session"))
    monkeypatch.setattr(web_http, "dispatch_http_request", dispatch)
    assert client.get("/api/v1/connection/status").status_code == 200
    assert client.post("/api/v1/sessions", json={}).status_code == 201
    assert [call.kwargs["method"] for call in dispatch.await_args_list] == ["connection.status", "session.create"]
