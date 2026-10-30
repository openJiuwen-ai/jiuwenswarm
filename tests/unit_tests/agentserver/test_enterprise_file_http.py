import base64
from pathlib import Path

import httpx
import pytest

from jiuwenswarm.common.utils import build_tenant_key
from jiuwenswarm.server.agent_http_routes import build_http_app
from jiuwenswarm.server.app_services import AppServices
from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry
from jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter import (
    WorkspaceFileAdapter,
)
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext


@pytest.fixture
def app(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    services = AppServices()
    services.adapter_registry = AdapterRegistry()
    services.adapter_registry.register(WorkspaceFileAdapter())
    yield build_http_app(services)
    from jiuwenswarm.server.runtime.session import session_history, session_metadata

    assert session_history.flush_pending_writes()
    assert session_metadata.flush_pending_writes()
    TenantContext.clear()


def headers(user="u"):
    return {
        "X-User-Id": user,
        "X-Group-Id": "g",
        "X-Bot-Id": "b",
        "X-Session-Id": "session-one",
    }


@pytest.mark.asyncio
async def test_http_persist_is_scoped_and_reuses_document_adapter(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://agent"
    ) as client:
        body = {
            "documents": [
                {
                    "filename": "中文.txt",
                    "base64_data": base64.b64encode(b"hello").decode(),
                }
            ]
        }
        first = await client.post(
            "/api/v1/sessions/session-one/documents", headers=headers(), json=body
        )
        assert first.status_code == 200, first.text
        assert first.json()["ok"], first.text
        payload = first.json().get("data", first.json().get("payload"))
        path = Path(payload["media_items"][0]["path"])
        assert path.read_bytes() == b"hello"
        second = await client.post(
            "/api/v1/sessions/session-one/documents",
            headers=headers("v"),
            json={"documents": [{"filename": "中文.txt", "path": str(path)}]},
        )
        assert not second.json()["ok"]
        assert TenantContext.current_tenant_key() is None


@pytest.mark.asyncio
async def test_http_session_list_uses_the_same_tenant_scope(app):
    from jiuwenswarm.server.runtime.session.session_metadata import (
        update_session_metadata,
    )

    with TenantContext.scoped(build_tenant_key("g", "b", "u")):
        update_session_metadata(
            session_id="u-session",
            channel_id="web",
            user_id="u",
            title="U only",
            sync_write=True,
        )
    with TenantContext.scoped(build_tenant_key("g", "b", "v")):
        update_session_metadata(
            session_id="v-session",
            channel_id="web",
            user_id="v",
            title="V only",
            sync_write=True,
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    ) as client:
        own = (await client.get("/api/v1/sessions", headers=headers())).json()["data"]
        other = (await client.get("/api/v1/sessions", headers=headers("v"))).json()[
            "data"
        ]
        assert {item["session_id"] for item in own["sessions"]} == {"u-session"}
        assert {item["session_id"] for item in other["sessions"]} == {"v-session"}
    assert TenantContext.current_tenant_key() is None


@pytest.mark.asyncio
async def test_obs_import_strips_url_and_rejects_other_scope(app, monkeypatch):
    from jiuwenswarm.common.obs_files import AttachmentStore

    prefixes = []

    class Store:
        def read(self, url, prefix):
            prefixes.append(prefix)
            return b"from obs"

    monkeypatch.setattr(AttachmentStore, "from_env", lambda: Store())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://agent"
    ) as client:
        response = await client.post(
            "/api/v1/sessions/session-one/files/import",
            headers=headers(),
            json={"files": [{"name": "data.txt", "url": "http://obs/signed"}]},
        )
        assert response.json()["ok"], response.text
        data = response.json().get("data", response.json().get("payload"))
        assert "url" not in data["files"][0]
        assert Path(data["files"][0]["path"]).read_bytes() == b"from obs"
        relative = (
            "agent/sessions/session-one/uploads/" + Path(data["files"][0]["path"]).name
        )
        preview = await client.get(
            "/api/v1/workspace/preview",
            headers=headers(),
            params={"relative_path": relative},
        )
        assert preview.json()["data"]["content"] == "from obs", preview.text
        other_preview = await client.get(
            "/api/v1/workspace/preview",
            headers=headers("v"),
            params={"relative_path": relative},
        )
        assert not other_preview.json()["ok"]
        await client.post(
            "/api/v1/sessions/session-one/files/import",
            headers=headers("v"),
            json={"files": [{"name": "data.txt", "url": "http://obs/signed"}]},
        )
        assert prefixes[0] != prefixes[1]


@pytest.mark.asyncio
async def test_file_routes_registered(app):
    from gateway.channel_manager.web.http_routes.mapped import (
        WORKSPACE_FILE_ROUTES,
    )

    for spec in WORKSPACE_FILE_ROUTES:
        if "{session_id}" in spec.path:
            assert spec.path_to_param.get("session_id") == "session_id"
    paths = {route.path for route in app.routes}
    assert "/api/v1/sessions/{session_id}/files/download" in paths
    assert "/api/v1/sessions/{session_id}/media" in paths
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://agent"
    ) as client:
        result = await client.get("/api/v1/documents/formats", headers=headers())
        assert result.json()["ok"], result.text


@pytest.mark.asyncio
async def test_import_reuses_quota_gate(app, monkeypatch):
    from jiuwenswarm.common import obs_files
    from jiuwenswarm.enterprise.capabilities.workspace import quota
    from types import SimpleNamespace

    monkeypatch.setenv("WORKSPACE_QUOTA_ENABLED", "true")
    monkeypatch.setenv("AGENT_WORKSPACE_QUOTA_DEFAULT_LIMIT_BYTES", "1")
    monkeypatch.setattr(quota, "_DB_POLICIES", [])
    monkeypatch.setattr(
        obs_files.AttachmentStore,
        "from_env",
        lambda: SimpleNamespace(read=lambda *args: b"too large"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://agent"
    ) as client:
        result = await client.post(
            "/api/v1/sessions/session-one/files/import",
            headers=headers(),
            json={"files": [{"name": "data.txt", "url": "http://obs/file"}]},
        )
        assert not result.json()["ok"]
        assert result.json()["error"]["code"] == "WORKSPACE_QUOTA_EXCEEDED"


@pytest.mark.asyncio
async def test_signed_download_is_bound_to_tenant(app, monkeypatch, tmp_path):
    import time
    from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
        VerifiedDownloadAssetOwner,
    )
    from jiuwenswarm.agents.harness.common.tools.web_file_download import (
        WebFileDownloadManager,
    )

    source = tmp_path / "source.txt"
    source.write_bytes(b"verified content")
    owner = VerifiedDownloadAssetOwner(root=tmp_path / "assets")
    asset = owner.stage(source, file_name="source.txt", expires_at=time.time() + 600)
    manager = WebFileDownloadManager("local-test-secret" * 3, asset_owner=owner)
    monkeypatch.setattr(WebFileDownloadManager, "_instance", manager)
    with TenantContext.scoped(build_tenant_key("g", "b", "u")):
        token = manager.generate_verified_asset_token(
            asset, file_name="source.txt", session_id="session-one"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://agent"
    ) as client:
        route = "/api/v1/sessions/session-one/files/download"
        response = await client.post(
            route, headers=headers(), json={"token": token, "offset": 0, "limit": 512}
        )
        assert response.json()["ok"], response.text
        assert (
            base64.b64decode(response.json()["data"]["data_base64"])
            == b"verified content"
        )
        denied = await client.post(
            route,
            headers=headers("v"),
            json={"token": token, "offset": 0, "limit": 512},
        )
        assert not denied.json()["ok"]


def test_verified_download_rejects_missing_enterprise_tenant(monkeypatch, tmp_path):
    import time
    from jiuwenswarm.common.file_download_token import WebFileDownloadManager
    from jiuwenswarm.common.verified_download_assets import VerifiedDownloadAssetOwner

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    TenantContext.clear()
    source = tmp_path / "source.txt"
    source.write_bytes(b"verified content")
    owner = VerifiedDownloadAssetOwner(root=tmp_path / "assets")
    asset = owner.stage(source, file_name="source.txt", expires_at=time.time() + 600)
    manager = WebFileDownloadManager("local-test-secret" * 3, asset_owner=owner)
    with pytest.raises(ValueError, match="tenant context"):
        manager.generate_verified_asset_token(asset, file_name="source.txt", session_id="s")


@pytest.mark.parametrize(
    "edition,tenant,token_tenant,allowed",
    [
        ("enterprise", None, None, False),
        ("enterprise", "", "", False),
        ("enterprise", "g::b::u", None, False),
        ("enterprise", "g::b::u", "g::b::v", False),
        ("enterprise", "g::b::u", "g::b::u", True),
        ("personal", None, None, True),
    ],
)
@pytest.mark.asyncio
async def test_verified_download_requires_nonempty_enterprise_tenant(
    monkeypatch, tmp_path, edition, tenant, token_tenant, allowed
):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.agents.harness.common.tools import web_file_download

    path = tmp_path / "asset"
    path.write_bytes(b"data")
    payload = {
        "kind": "verified_asset_v1",
        "path": str(path),
        "name": "asset",
        "size": 4,
    }
    if token_tenant is not None:
        payload["tenant_key"] = token_tenant
    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    monkeypatch.setattr(TenantContext, "current_tenant_key", lambda: tenant)
    monkeypatch.setattr(
        web_file_download, "validate_file_download_token", lambda *args, **kw: payload
    )
    response = await WorkspaceFileAdapter()._handle_verified_download_chunk(
        AgentRequest(
            request_id="r", channel_id="web", session_id="s", params={"token": "signed"}
        )
    )
    assert response.ok is allowed
    if not allowed:
        assert response.payload["code"] == "FORBIDDEN"
