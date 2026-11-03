import hashlib
import io
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from minio.error import S3Error

from jiuwenswarm.agents.harness.common.tools.obs_file_delivery import (
    build_object_file_download_info,
)
from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
    VerifiedDownloadAssetOwner,
)
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)
from jiuwenswarm.common.obs_files import AttachmentStore, DOWNLOAD_TTL_SECONDS
from jiuwenswarm.common.utils import build_tenant_key
from gateway.channel_manager.web.enterprise_files import (
    register_enterprise_file_routes,
    serve_presigned_download,
)
from starlette.requests import Request
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext


@pytest.mark.parametrize(
    "scenario", ["success", "full_access", "missing_grant", "obs_failure"]
)
@pytest.mark.asyncio
async def test_enterprise_tool_keeps_execution_authorization(
    delivery, tmp_path, monkeypatch, scenario
):
    from jiuwenswarm.agents.harness.common.tools import send_file_to_user as tool
    from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
        clear_send_file_execution_grant,
        create_send_file_execution_grant,
        publish_send_file_execution_grant,
    )

    clear_send_file_execution_grant()
    tool._SENT_FILE_PATHS_BY_SESSION.clear()
    source = tmp_path / "tool-output.txt"
    source.write_bytes(b"authorized tool delivery")
    push, history = AsyncMock(return_value=True), Mock()
    monkeypatch.setattr(tool, "send_runtime_push", push)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        history,
    )
    if scenario in {"success", "obs_failure"}:
        publish_send_file_execution_grant(
            create_send_file_execution_grant((source,), target_channels=("web",))
        )
    if scenario == "obs_failure":
        monkeypatch.setattr(
            delivery.client,
            "put_object",
            Mock(side_effect=RuntimeError("OBS unavailable")),
        )
    toolkit = tool.SendFileToolkit(
        request_id="r",
        session_id="tool-session",
        channel_id="web",
        user_id="u",
        metadata={"user_id": "u", "routing": {"group_id": "g", "bot_id": "b"}},
        project_dir=str(tmp_path),
        require_execution_authorization=scenario != "full_access",
        asset_owner=delivery.owner,
    )
    monkeypatch.setattr(
        delivery.owner,
        "stage",
        Mock(side_effect=AssertionError("enterprise must not create snapshots")),
    )
    try:
        result = await toolkit.send_file(str(source), target_channels=("web",))
        if scenario in {"success", "full_access"}:
            assert "成功发送 1 个文件" in result
            file = push.await_args.args[0]["payload"]["files"][0]
            assert file["download_token"] == file["url"]
            assert "/file-api/download?url=" in file["download_url"]
            assert history.call_args.kwargs["extra"]["files"][0] == file
        else:
            assert "失败" in result
            push.assert_not_awaited()
            history.assert_not_called()
        delivery.owner.stage.assert_not_called()
        assert not delivery.owner.root.exists()
    finally:
        clear_send_file_execution_grant()
        tool._SENT_FILE_PATHS_BY_SESSION.clear()


class ObjectClient:
    def __init__(self):
        self.objects = {}
        self.signed = {}
        self.reads = []
        self.closed = False
        self.released = False

    def bucket_exists(self, bucket):
        return True

    def presigned_get_object(self, bucket, key, *, expires):
        assert expires == timedelta(days=7)
        url = f"http://obs/{bucket}/{quote(key)}?signature=issued"
        self.signed[url] = (bucket, key, time.time() + expires.total_seconds())
        return url

    def signed_request(self, method, url, *, headers=None, redirect, preload_content):
        headers = headers or {}
        from gateway.channel_manager.web.container_file_http import _parse_single_byte_range

        assert method == "GET" and not redirect and not preload_content
        status, response_headers = 200, {}
        signed = self.signed.get(url)
        location = signed[:2] if signed else None
        if not signed or time.time() > signed[2]:
            status = 403
        elif location not in self.objects:
            status = 404
        if status != 200:
            response = io.BytesIO()
            response.status, response.headers = status, response_headers
            response.release_conn = Mock()
            return response
        bucket, key = location
        data, metadata, etag = self.objects[location]
        byte_range = _parse_single_byte_range(headers["Range"], len(data)) if headers.get("Range") else None
        if headers.get("Range") and byte_range is None:
            response = io.BytesIO()
            response.status, response.headers = 416, {}
            response.release_conn = Mock()
            return response
        offset, end = byte_range or (0, max(len(data) - 1, 0))
        length = end - offset + 1 if data else 0
        response = self.get_object(bucket, key, offset=offset, length=length,
            request_headers={"If-Match": f'"{etag}"'})
        response.status = 206 if byte_range else 200
        response.headers = {"Content-Length": str(length),
            "x-amz-meta-session-id": metadata.get("session-id", "")}
        if byte_range:
            response.headers["Content-Range"] = f"bytes {offset}-{end}/{len(data)}"
        return response

    def put_object(self, bucket, key, reader, length, metadata):
        data = b""
        while len(data) < length:
            chunk = reader.read(min(13, length - len(data)))
            assert chunk
            data += chunk
        etag = hashlib.md5(data).hexdigest()
        self.objects[(bucket, key)] = (data, metadata, etag)
        return SimpleNamespace(etag=etag)

    def remove_object(self, bucket, key):
        self.objects.pop((bucket, key))

    def stat_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise S3Error(
                response=None,
                code="NoSuchKey",
                message="missing",
                resource="",
                request_id="",
                host_id="",
            )
        data, metadata, etag = self.objects[(bucket, key)]
        return SimpleNamespace(
            size=len(data),
            etag=etag,
            metadata={"x-amz-meta-sha256": metadata["sha256"]},
        )

    def get_object(self, bucket, key, *, offset=0, length=None, request_headers):
        data, _, etag = self.objects[(bucket, key)]
        assert request_headers["If-Match"] == f'"{etag}"'
        self.reads.append((offset, length))
        response = io.BytesIO(
            data[offset:] if length is None else data[offset : offset + length]
        )
        close = response.close

        def closed():
            self.closed = True
            close()

        response.close = closed
        response.release_conn = lambda: setattr(self, "released", True)
        return response


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    identity = {"group_id": "g", "bot_id": "b", "user_id": "u"}
    tenant = build_tenant_key("g", "b", "u")
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(TenantContext, "current_tenant_key", lambda: tenant)
    client = ObjectClient()
    store = AttachmentStore(client, "bucket", "http://obs")
    monkeypatch.setattr(AttachmentStore, "from_env", lambda: store)
    monkeypatch.setattr(
        "gateway.channel_manager.web.enterprise_files._obs_http_client",
        lambda: SimpleNamespace(request=client.signed_request, clear=Mock()),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.obs_files._obs_http_client",
        lambda: SimpleNamespace(request=client.signed_request, clear=Mock()),
    )
    owner = VerifiedDownloadAssetOwner(root=tmp_path / "sealed", start_sweeper=False)
    source = tmp_path / "中文.txt"
    source.write_bytes(b"workspace file\n")
    metadata = {"user_id": "u", "routing": {"group_id": "g", "bot_id": "b"}}
    info = build_object_file_download_info(source, "session", "u", metadata=metadata)
    return SimpleNamespace(
        client=client,
        store=store,
        owner=owner,
        source=source,
        info=info,
        identity=identity,
        tenant=tenant,
        metadata=metadata,
    )

def test_workspace_change_during_upload_is_not_delivered(delivery, monkeypatch):
    put = delivery.client.put_object

    def changed(*args, **kwargs):
        delivery.source.write_bytes(b"X" * delivery.source.stat().st_size)
        return put(*args, **kwargs)

    monkeypatch.setattr(delivery.client, "put_object", changed)
    with pytest.raises(RuntimeError, match="object-store delivery failed") as error:
        build_object_file_download_info(delivery.source, "session", "u", metadata=delivery.metadata)
    assert isinstance(error.value.__cause__, ValueError)
    assert "source changed" in str(error.value.__cause__)
    assert len(delivery.client.objects) == 1

@pytest.mark.asyncio
async def test_gateway_reads_obs_without_agent_or_snapshot(delivery):
    delivery.source.unlink()
    app = FastAPI()

    # No AgentServer client: the test must fail if the download falls back to Runtime.
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        url = delivery.info["download_url"]
        full = await client.get(url)
        assert full.status_code == 200 and full.content == b"workspace file\n"
        assert delivery.client.closed and delivery.client.released
        count = len(delivery.client.reads)
        head = await client.head(url)
        assert head.status_code == 206 and not head.content
        assert len(delivery.client.reads) == count + 1
        part = await client.get(url, headers={"Range": "bytes=2-8"})
        assert part.status_code == 206 and part.content == full.content[2:9]
        suffix = await client.get(url, headers={"Range": "bytes=-4"})
        assert suffix.status_code == 206 and suffix.content == full.content[-4:]
        invalid = await client.get(url, headers={"Range": "bytes=999-"})
        assert invalid.status_code == 416
        assert (await client.get(url + "&session_id=another")).status_code == 403
        assert (await client.get(url, headers={"X-User-Id": "other"})).status_code == 403
        assert (await client.get(url, headers={"X-Group-Id": "other"})).status_code == 403
        assert (await client.get(url, headers={"X-Bot-Id": "other"})).status_code == 403


@pytest.mark.asyncio
async def test_presigned_delivery_does_not_read_gateway_signing_key(delivery, monkeypatch):
    monkeypatch.setattr(WebFileDownloadManager, "get_instance", Mock(side_effect=AssertionError("Gateway read a local signing key")))
    file = build_object_file_download_info(delivery.source, "session", "u", metadata=delivery.metadata)
    app = FastAPI()
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        assert (await client.head(file["download_url"])).status_code == 206
        response = await client.get(file["download_url"])
        assert response.status_code == 200 and response.content == b"workspace file\n"


@pytest.mark.parametrize("change", ["signature", "origin", "bucket", "tenant", "path", "session"])
@pytest.mark.asyncio
async def test_presigned_delivery_rejects_tampering(delivery, change):
    from urllib.parse import parse_qs, urlencode, urlsplit

    query = {key: value[0] for key, value in parse_qs(urlsplit(delivery.info["download_url"]).query).items()}
    if change == "signature":
        query["url"] = query["url"].replace("signature=issued", "signature=other")
    elif change == "origin":
        query["url"] = query["url"].replace("http://obs/", "http://another/")
    elif change == "bucket":
        query["url"] = query["url"].replace("/bucket/", "/another/")
    elif change == "tenant":
        query["user_id"] = "another"
    elif change == "path":
        query["url"] = query["url"].replace("/downloads/", "/uploads/")
    else:
        query["session_id"] = "another"
    app = FastAPI()
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        response = await client.get("/file-api/download?" + urlencode(query))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_presigned_empty_file_supports_head(delivery):
    source = delivery.source
    source.write_bytes(b"")
    file = build_object_file_download_info(source, "session", "u", metadata=delivery.metadata)
    app = FastAPI()
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        head = await client.head(file["download_url"])
        get = await client.get(file["download_url"])
    assert head.status_code == get.status_code == 200
    assert head.headers["content-length"] == "0" and not get.content

@pytest.mark.asyncio
async def test_missing_object_is_404(delivery):
    delivery.client.objects.clear()
    app = FastAPI()

    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        assert (await client.get(delivery.info["download_url"])).status_code == 404

@pytest.mark.parametrize("identity", [{}, {"user_id": "other", "routing": {"group_id": "g", "bot_id": "b"}}])
def test_delivery_rejects_missing_or_mismatched_identity_before_upload(delivery, identity):
    count = len(delivery.client.objects)
    with pytest.raises(ValueError, match="identity must match"):
        build_object_file_download_info(delivery.source, "session", identity.get("user_id", "u"), metadata=identity)
    assert len(delivery.client.objects) == count


def test_publishes_seven_day_obs_url_without_snapshot_or_app_token(delivery):
    assert abs(delivery.info["expires_at"] - time.time() - DOWNLOAD_TTL_SECONDS) < 3
    assert delivery.info["download_token"] == delivery.info["url"]
    key = delivery.store.download_key(delivery.info["url"], delivery.tenant)
    data, metadata, _ = delivery.client.objects[(delivery.store.bucket, key)]
    assert data == b"workspace file\n"
    assert metadata["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert metadata["session-id"] == "session"
    delivery.source.write_bytes(b"changed source")
    assert delivery.client.objects[(delivery.store.bucket, key)][0] == data
    assert not delivery.owner.root.exists()


@pytest.mark.parametrize("elapsed,valid", [(601, True), (604799, True), (604802, False)])
@pytest.mark.asyncio
async def test_obs_enforces_seven_day_expiry(delivery, monkeypatch, elapsed, valid):
    created_at = delivery.info["expires_at"] - DOWNLOAD_TTL_SECONDS
    monkeypatch.setattr(time, "time", lambda: created_at + elapsed)
    app = FastAPI()
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        response = await client.get(delivery.info["download_url"])
    assert response.status_code == (200 if valid else 403)


def test_skill_delivery_read_checks_session_and_tenant(delivery):
    assert delivery.store.read_download(delivery.info["url"], delivery.tenant, "session") == b"workspace file\n"
    assert delivery.client.closed and delivery.client.released
    with pytest.raises(ValueError, match="session mismatch"):
        delivery.store.read_download(delivery.info["url"], delivery.tenant, "another")
    with pytest.raises(ValueError, match="current tenant"):
        delivery.store.read_download(delivery.info["url"], "another", "session")


@pytest.mark.asyncio
async def test_proxy_releases_connection_when_close_raises(delivery, monkeypatch):
    get_object = delivery.client.get_object
    def failed_close(*args, **kwargs):
        response = get_object(*args, **kwargs)
        response.close = Mock(side_effect=OSError("close failed"))
        return response
    monkeypatch.setattr(delivery.client, "get_object", failed_close)
    response = await serve_presigned_download(
        Request({"type": "http", "method": "GET", "headers": [], "query_string": b""}),
        delivery.info["url"], delivery.identity,
    )
    with pytest.raises(OSError, match="close failed"):
        async for _ in response.body_iterator:
            pass
    assert delivery.client.released


@pytest.mark.parametrize("status", [301, 302, 401, 500, 503])
@pytest.mark.asyncio
async def test_proxy_does_not_follow_redirects_or_expose_upstream_errors(delivery, monkeypatch, status):
    response = Mock(status=status, headers={"Location": "http://untrusted/"})
    pool = Mock(request=Mock(return_value=response))
    monkeypatch.setattr("gateway.channel_manager.web.enterprise_files._obs_http_client", lambda: pool)
    result = await serve_presigned_download(
        Request({"type": "http", "method": "HEAD", "headers": [], "query_string": b""}),
        delivery.info["url"], delivery.identity,
    )
    assert result.status_code == 502
    assert pool.request.call_args.kwargs["redirect"] is False
    response.release_conn.assert_called_once()
    pool.clear.assert_called_once()
