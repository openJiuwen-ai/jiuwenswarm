"""Loopback HTTP integration of the new uploader; no live Hub or OAuth involved."""

from email.parser import BytesParser
from email.policy import default
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import zipfile

import pytest

from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity
from jiuwenswarm.server.runtime.marketplace.hub_client import HubClient
from jiuwenswarm.server.runtime.marketplace.hub_publish_client import (
    PublishAuth,
    PublishRequest,
    PublishUploadError,
)


@pytest.fixture
def hub(monkeypatch):
    # Loopback fixtures must not traverse the developer machine's proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    captured = []
    scenario = {"status": 200, "publish_result": "pending_moderation"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            parsed = BytesParser(policy=default).parsebytes(
                f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
            )
            parts = {
                p.get_param("name", header="Content-Disposition"): p.get_payload(
                    decode=True
                )
                for p in parsed.iter_parts()
            }
            captured.append(
                {
                    "parts": parts,
                    "checksum": self.headers["X-Checksum-SHA256"],
                    "authorization": self.headers.get("Authorization"),
                    "system": self.headers.get("X-System-Token"),
                    "provider": self.headers.get("X-OAuth-Provider"),
                    "path": self.path,
                }
            )
            status = scenario["status"]
            if status == 200:
                kind = {
                    "skill": "skill",
                    "plugin": "agent-plugin",
                    "agent_template": "agent-template",
                    "mcp": "agent-mcp",
                }[scenario["kind"]]
                payload = {
                    "code": 200,
                    "data": {
                        "asset_id": "local-test-asset",
                        "plugin_id": "local-test-asset",
                        "asset_type": kind,
                        "plugin_type": kind,
                        "name": "e2e-asset",
                        "version": "1.0.0",
                        "visibility": "private",
                        "publish_result": scenario["publish_result"],
                        "status": "ACTIVE",
                        "deduplicated": False,
                    },
                }
            else:
                payload = {
                    "detail": {
                        "code": status,
                        "error": "version_exists" if status == 409 else "rate_limited",
                        "message": "test rejection",
                    }
                }
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", scenario, captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["skill", "plugin", "agent_template", "mcp"])
@pytest.mark.parametrize(
    "publish_result", ["pending_moderation", "publish_failed", "publish_success"]
)
async def test_real_http_preserves_result_and_exact_artifact(
    hub, tmp_path, kind, publish_result
):
    url, scenario, captured = hub
    scenario.update(kind=kind, publish_result=publish_result)
    path = tmp_path / "asset.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "SKILL.md" if kind == "skill" else "manifest.json",
            "synthetic fixture; structural validation is outside transport scope",
        )
    expected = path.read_bytes()
    request = PublishRequest(
        PublishIdentity(kind, "e2e-asset", "1.0.0"),
        path,
        hashlib.sha256(expected).hexdigest(),
        visibility="private",
    )
    result = await HubClient(base_url=url, system_token="must-not-be-used").publish(
        request, auth=PublishAuth("synthetic-user-token", "gitcode")
    )
    assert result.publish_result == publish_result
    assert len(captured) == 1
    received = captured[0]
    assert received["parts"]["file"] == expected
    assert received["checksum"] == hashlib.sha256(expected).hexdigest()
    assert (
        received["authorization"] == "Bearer synthetic-user-token"
        and received["system"] is None
    )
    assert received["provider"] == "gitcode"
    assert received["path"] == "/api/v1/plugins"
    assert "plugin_type" not in received["parts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [409, 429, 500])
async def test_real_http_rejection_not_retried(hub, tmp_path, status):
    url, scenario, captured = hub
    scenario.update(status=status, kind="plugin")
    path = tmp_path / "asset.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", "{}")
    request = PublishRequest(
        PublishIdentity("plugin", "e2e-asset", "1.0.0"),
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    with pytest.raises(PublishUploadError) as exc:
        await HubClient(base_url=url).publish(
            request, auth=PublishAuth("synthetic-user-token")
        )
    assert exc.value.outcome_unknown == (status == 500)
    assert len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["skill", "plugin", "agent_template", "mcp"])
async def test_package_task_upload_and_persistent_result_over_http(hub, tmp_path, kind):
    from jiuwenswarm.server.runtime.marketplace.asset_publish_service import (
        AssetPublishService,
    )
    from jiuwenswarm.server.runtime.marketplace.asset_publish_store import PublishStore
    import io

    url, scenario, captured = hub
    scenario.update(kind=kind)
    source = tmp_path / "local"
    source.mkdir()
    if kind == "skill":
        (source / "SKILL.md").write_text(
            "---\nname: e2e-asset\ndescription: example\n---\nbody\n"
        )
    else:
        manifest = dict(
            package_type=kind,
            id="e2e-asset",
            name="e2e-asset",
            version="0.0.1",
            description="example",
        )
        if kind == "mcp":
            manifest.update(
                display_name={"zh": "示例", "en": "Example"},
                display_description={"zh": "示例", "en": "Example"},
                integration={"type": "remote-mcp", "file": "mcp.json"},
            )
            (source / "mcp.json").write_text(
                json.dumps(
                    {"mcpServers": {"e2e-asset": {"url": "https://example.com/mcp"}}}
                )
            )
        (source / "manifest.json").write_text(json.dumps(manifest))
    (source / ".env").write_text("TOKEN=synthetic-excluded-secret")
    store = PublishStore(tmp_path / "records.db")
    service = AssetPublishService(
        store,
        HubClient(base_url=url),
        lambda scope, kind, local_id: source,
        tmp_path / "packages",
    )
    try:
        draft = await service.prepare(
            "trusted-scope",
            "local-id",
            PublishIdentity(kind, "e2e-asset", "1.0.0"),
            {"visibility": "private"},
        )
        operation = await service.commit(
            "trusted-scope",
            draft["draft_id"],
            "once",
            auth=PublishAuth("synthetic-user-token"),
        )
        await service.wait_idle()
        record = service.status("trusted-scope", operation["operation_id"])
        assert record["execution_status"] == "completed"
        assert record["result"]["publish_result"] == "pending_moderation"
        assert len(captured) == 1
        with zipfile.ZipFile(io.BytesIO(captured[0]["parts"]["file"])) as archive:
            assert not any(
                name.endswith("/.env") or name == ".env" for name in archive.namelist()
            )
            assert (
                "e2e-asset/plugin.yaml" if kind == "skill" else "manifest.json"
            ) in archive.namelist()
        assert captured[0]["checksum"] == draft["checksum_sha256"]
        assert not list((tmp_path / "packages").rglob("artifact.zip"))
        await service.close()
    finally:
        store.close()
    reopened = PublishStore(tmp_path / "records.db")
    try:
        assert (
            reopened.status("trusted-scope", operation["operation_id"])["result"]
            == record["result"]
        )
    finally:
        reopened.close()
