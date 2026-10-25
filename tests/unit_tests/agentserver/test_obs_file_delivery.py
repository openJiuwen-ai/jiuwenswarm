import hashlib
import io
import time
from types import SimpleNamespace
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
from jiuwenswarm.gateway.channel_manager.web.enterprise_files import (
    register_enterprise_file_routes,
    serve_object_download,
)
from starlette.requests import Request
from jiuwenswarm.server.runtime.tenant_context import TenantContext


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
            claims = delivery.manager.validate_token(file["download_token"])
            assert claims["kind"] == "obs_asset_v1" and claims["sid"] == "tool-session"
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
        self.reads = []
        self.closed = False
        self.released = False

    def bucket_exists(self, bucket):
        return True

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
    manager = WebFileDownloadManager("s" * 64)
    monkeypatch.setattr(WebFileDownloadManager, "get_instance", lambda: manager)
    owner = VerifiedDownloadAssetOwner(root=tmp_path / "sealed", start_sweeper=False)
    source = tmp_path / "中文.txt"
    source.write_bytes(b"workspace file\n")
    metadata = {"user_id": "u", "routing": {"group_id": "g", "bot_id": "b"}}
    info = build_object_file_download_info(source, "session", "u", metadata=metadata)
    return SimpleNamespace(
        client=client,
        store=store,
        manager=manager,
        owner=owner,
        source=source,
        info=info,
        identity=identity,
        tenant=tenant,
        metadata=metadata,
    )


def test_publishes_workspace_file_without_snapshot_and_seven_day_claims(delivery):
    payload = delivery.manager.validate_token(delivery.info["download_token"])
    assert payload["kind"] == "obs_asset_v1"
    assert abs(payload["exp"] - time.time() - 7 * 86400) < 3
    assert payload["tenant_key"] == delivery.tenant
    data = next(iter(delivery.client.objects.values()))[0]
    assert data == b"workspace file\n"
    assert payload["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert "path" not in payload
    assert not delivery.owner.root.exists()
    delivery.source.write_bytes(b"changed workspace source")
    assert next(iter(delivery.client.objects.values()))[0] == data
    assert delivery.manager.validate_token(delivery.info["download_token"])


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


@pytest.mark.parametrize(
    "elapsed, valid", [(601, True), (604799, True), (604802, False)]
)
def test_object_delivery_expiry_is_seven_days_not_ten_minutes(
    delivery, monkeypatch, elapsed, valid
):
    payload = delivery.manager.validate_token(delivery.info["download_token"])
    monkeypatch.setattr(
        time, "time", lambda: payload["exp"] - DOWNLOAD_TTL_SECONDS + elapsed
    )
    assert (
        bool(delivery.manager.validate_token(delivery.info["download_token"])) is valid
    )


def test_materialize_object_checks_digest_and_closes_response(delivery, tmp_path):
    payload = delivery.manager.validate_token(delivery.info["download_token"])
    target = tmp_path / "restored.txt"
    delivery.source.unlink()
    delivery.store.download_file(payload, target)
    assert target.read_bytes() == b"workspace file\n"
    assert delivery.client.closed and delivery.client.released
    key = (delivery.store.bucket, payload["object_key"])
    _, metadata, etag = delivery.client.objects[key]
    delivery.client.objects[key] = (b"X" * payload["size"], metadata, etag)
    with pytest.raises(ValueError, match="digest mismatch"):
        delivery.store.download_file(payload, tmp_path / "corrupt.txt")


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
        assert head.status_code == 200 and not head.content
        assert len(delivery.client.reads) == count
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


@pytest.mark.parametrize(
    "change",
    [
        {"exp": 1},
        {"sid": ""},
        {"object_key": "downloads/another/file"},
        {"digest": "bad"},
        {"size": -1},
        {"size": True},
        {"bucket": "other"},
        {"etag": "changed"},
        {"size": 999},
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_changed_objects_fail_closed(delivery, change):
    payload = delivery.manager.validate_token(delivery.info["download_token"])
    payload.update(change)
    token = delivery.manager._sign_payload(payload)
    app = FastAPI()

    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.get("/file-api/download", params={"token": token})
        assert response.status_code == 403
        assert not delivery.client.reads


@pytest.mark.asyncio
async def test_missing_object_is_404(delivery):
    delivery.client.objects.clear()
    app = FastAPI()

    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        assert (await client.get(delivery.info["download_url"])).status_code == 404


@pytest.mark.asyncio
async def test_history_token_with_delimited_tenant_still_downloads(delivery):
    payload = delivery.manager.validate_token(delivery.info["download_token"])
    old_key = (delivery.store.bucket, payload["object_key"])
    payload.pop("identity")
    payload["tenant_key"] = "g::b::u"
    from jiuwenswarm.common.obs_files import download_object_key

    payload["object_key"] = download_object_key(payload["tenant_key"], payload["asset_id"], payload["name"])
    delivery.client.objects[(delivery.store.bucket, payload["object_key"])] = delivery.client.objects[old_key]
    token = delivery.manager._sign_payload(payload)
    app = FastAPI()
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        url = delivery.manager.generate_download_url(token, "u")
        response = await client.get(url)
        assert response.status_code == 200 and response.content == b"workspace file\n"
        assert (await client.get(url, headers={"X-User-Id": "other"})).status_code == 403


@pytest.mark.parametrize("identity", [{}, {"user_id": "other", "routing": {"group_id": "g", "bot_id": "b"}}])
def test_delivery_rejects_missing_or_mismatched_identity_before_upload(delivery, identity):
    count = len(delivery.client.objects)
    with pytest.raises(ValueError, match="identity must match"):
        build_object_file_download_info(delivery.source, "session", identity.get("user_id", "u"), metadata=identity)
    assert len(delivery.client.objects) == count


@pytest.mark.parametrize(
    "missing", ["tenant_key", "bucket", "object_key", "size", "etag", "digest", "name"]
)
@pytest.mark.asyncio
async def test_malformed_signed_claims_are_forbidden_before_sdk_access(
    delivery, missing, monkeypatch
):
    payload = delivery.manager.validate_token(delivery.info["download_token"])
    del payload[missing]
    token = delivery.manager._sign_payload(payload)
    store_factory = Mock(side_effect=AssertionError("malformed token reached OBS"))
    monkeypatch.setattr(AttachmentStore, "from_env", store_factory)
    response = await serve_object_download(
        Request({"type": "http", "method": "GET", "headers": [], "query_string": b""}),
        token,
        delivery.identity,
    )
    assert response.status_code == 403
    store_factory.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_releases_connection_when_stream_close_raises(
    delivery, monkeypatch
):
    get_object = delivery.client.get_object

    def response_with_failed_close(*args, **kwargs):
        response = get_object(*args, **kwargs)
        response.close = Mock(side_effect=OSError("close failed"))
        return response

    monkeypatch.setattr(delivery.client, "get_object", response_with_failed_close)
    response = await serve_object_download(
        Request({"type": "http", "method": "GET", "headers": [], "query_string": b""}),
        delivery.info["download_token"],
        delivery.identity,
    )
    with pytest.raises(OSError, match="close failed"):
        async for _ in response.body_iterator:
            pass
    assert delivery.client.released
