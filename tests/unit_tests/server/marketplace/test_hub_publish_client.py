# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from dataclasses import asdict
from email.parser import BytesParser
from email.policy import default
import hashlib
import io
import zipfile

import httpx
import pytest

from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity
from jiuwenswarm.server.runtime.marketplace.hub_publish_client import (
    HubPublishClient,
    PublishAuth,
    PublishRequest,
    PublishUploadError,
)


@pytest.fixture
def draft(tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "manifest.json",
            '{"id":"sales-assistant","version":"1.0.0","package_type":"plugin"}',
        )
    path = tmp_path / "asset.zip"
    path.write_bytes(data.getvalue())
    return PublishRequest(
        identity=PublishIdentity("plugin", "sales-assistant", "1.0.0"),
        artifact_path=path,
        artifact_sha256=hashlib.sha256(data.getvalue()).hexdigest(),
        display_name="销售助手",
        description="整理资料",
        tags=("sales", "crm"),
        version_desc="首次发布",
        visibility="private",
    )


def response_data(draft):
    return dict(
        asset_id="asset-1",
        plugin_id="asset-1",
        asset_type="agent-plugin",
        plugin_type="agent-plugin",
        name="sales-assistant",
        version="1.0.0",
        publish_result="pending_moderation",
        status="ACTIVE",
        visibility=draft.visibility,
        deduplicated=False,
    )


@pytest.mark.asyncio
async def test_uploads_zip_with_checksum_and_user_identity(draft):
    received = []

    async def handler(request):
        body = await request.aread()
        parsed = BytesParser(policy=default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + body
        )
        fields = {
            part.get_param("name", header="content-disposition"): part.get_payload(
                decode=True
            )
            for part in parsed.iter_parts()
        }
        received.append((request, fields))
        return httpx.Response(200, json={"code": 200, "data": response_data(draft)})

    client = HubPublishClient(
        base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
    )
    result = await client.publish(
        draft, auth=PublishAuth("secret-user-token", "gitcode")
    )
    request, fields = received[0]
    assert str(request.url) == "https://hub.example.test/api/v1/plugins"
    assert request.headers["authorization"] == "Bearer secret-user-token"
    assert request.headers["x-oauth-provider"] == "gitcode"
    assert "x-system-token" not in request.headers
    assert fields["file"] == draft.artifact_path.read_bytes()
    assert (
        request.headers["x-checksum-sha256"]
        == hashlib.sha256(fields["file"]).hexdigest()
    )
    assert fields == dict(
        file=fields["file"],
        asset_name=b"sales-assistant",
        plugin_version=b"1.0.0",
        display_name="销售助手".encode(),
        description="整理资料".encode(),
        tags=b"sales,crm",
        version_desc="首次发布".encode(),
        visibility=b"private",
        force=b"false",
    )
    assert result.publish_result == "pending_moderation"
    assert "secret-user-token" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_version"),
        (401, "unauthorized"),
        (403, "permission_denied"),
        (404, "plugin_not_found"),
        (409, "version_exists"),
        (413, "file_too_large"),
        (429, "rate_limited"),
    ],
)
async def test_known_rejections_are_not_retried_or_leaked(draft, status, code):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Retry-After": "30"},
            json={
                "detail": {
                    "code": status,
                    "error": code,
                    "message": "secret-user-token",
                }
            },
        )

    client = HubPublishClient(
        base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PublishUploadError) as exc:
        await client.publish(draft, auth=PublishAuth("secret-user-token"))
    assert len(calls) == 1
    assert exc.value.http_status == status
    assert not exc.value.outcome_unknown
    assert exc.value.code == code
    assert "secret-user-token" not in str(exc.value)
    if status == 429:
        assert exc.value.retry_after == 30


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["timeout", "500", "redirect", "malformed", "wrong_identity"]
)
async def test_uncertain_results_do_not_retry(draft, failure):
    calls = []

    async def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-user-token", request=request)
        if failure == "500":
            return httpx.Response(500)
        if failure == "redirect":
            return httpx.Response(
                307, headers={"location": "https://other.example.test"}
            )
        if failure == "malformed":
            return httpx.Response(200, text="secret-user-token")
        data = response_data(draft)
        data["version"] = "2.0.0"
        return httpx.Response(200, json={"code": 200, "data": data})

    client = HubPublishClient(
        base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PublishUploadError) as exc:
        await client.publish(draft, auth=PublishAuth("secret-user-token"))
    assert len(calls) == 1
    assert exc.value.outcome_unknown
    assert "secret-user-token" not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.asyncio
async def test_changed_package_never_reaches_network(draft):
    with zipfile.ZipFile(draft.artifact_path, "w") as archive:
        archive.writestr("manifest.json", "changed-content")
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200)

    client = HubPublishClient(
        base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PublishUploadError) as exc:
        await client.publish(draft, auth=PublishAuth("secret-user-token"))
    assert not calls
    assert exc.value.code == "artifact_changed"
    assert not exc.value.outcome_unknown


def test_credentials_are_not_part_of_publish_record(draft):
    auth = PublishAuth("secret-user-token", "gitcode")
    assert "secret-user-token" not in repr(auth)
    assert "access_token" not in asdict(draft)


@pytest.mark.parametrize("token", ["", " secret", "secret\r\nInjected: header", None])
def test_invalid_auth_is_rejected(token):
    with pytest.raises(ValueError):
        PublishAuth(token)


@pytest.mark.asyncio
async def test_update_sends_target_and_explicit_force(draft):
    from dataclasses import replace

    draft = replace(
        draft,
        identity=PublishIdentity("plugin", "sales-assistant", "1.0.0", "asset-1"),
        force=True,
    )
    bodies = []

    async def handler(request):
        bodies.append(await request.aread())
        data = response_data(draft)
        data["publish_result"] = "publish_failed"
        return httpx.Response(200, json={"code": 200, "data": data})

    result = await HubPublishClient(
        base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
    ).publish(draft, auth=PublishAuth("secret"))
    assert b'name="plugin_id"\r\n\r\nasset-1\r\n' in bodies[0]
    assert b'name="force"\r\n\r\ntrue\r\n' in bodies[0]
    assert result.publish_result == "publish_failed"


@pytest.mark.asyncio
async def test_large_artifact_is_streamed_in_bounded_chunks(draft):
    from dataclasses import replace

    with zipfile.ZipFile(draft.artifact_path, "w") as archive:
        archive.writestr("data.bin", b"x" * (2 * 1024 * 1024))
    draft = replace(
        draft,
        artifact_sha256=hashlib.sha256(draft.artifact_path.read_bytes()).hexdigest(),
    )
    lengths = []

    class StreamingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            async for chunk in request.stream:
                lengths.append(len(chunk))
            assert sum(lengths) == int(request.headers["content-length"])
            return httpx.Response(200, json={"code": 200, "data": response_data(draft)})

    await HubPublishClient(
        base_url="https://hub.example.test", transport=StreamingTransport()
    ).publish(draft, auth=PublishAuth("secret"))
    assert max(lengths) <= 65536
    assert len(lengths) > 32


@pytest.mark.asyncio
async def test_size_limit_rejects_before_upload(draft):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200)

    with pytest.raises(PublishUploadError) as exc:
        await HubPublishClient(
            base_url="https://hub.example.test",
            max_zip_bytes=8,
            transport=httpx.MockTransport(handler),
        ).publish(draft, auth=PublishAuth("secret"))
    assert exc.value.code == "file_too_large"
    assert calls == []


@pytest.mark.asyncio
async def test_symlink_is_not_followed(draft, tmp_path):
    from dataclasses import replace

    link = tmp_path / "link.zip"
    link.symlink_to(draft.artifact_path)
    with pytest.raises(PublishUploadError) as exc:
        await HubPublishClient(base_url="https://hub.example.test").publish(
            replace(draft, artifact_path=link), auth=PublishAuth("secret")
        )
    assert exc.value.code == "artifact_unavailable"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret@hub.example.test",
        "https://hub.example.test?token=secret",
        "https://hub.example.test#fragment",
    ],
)
def test_publish_rejects_credential_bearing_urls(base_url):
    with pytest.raises(ValueError):
        HubPublishClient(base_url=base_url)


@pytest.mark.asyncio
async def test_hub_client_exposes_publish_without_changing_read_transport(draft):
    from jiuwenswarm.server.runtime.marketplace.hub_client import HubClient

    async def handler(request):
        return httpx.Response(200, json={"code": 200, "data": response_data(draft)})

    publisher = HubPublishClient(
        base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
    )
    client = HubClient(
        base_url="https://hub.example.test",
        publisher=publisher,
        system_token="must-not-be-used",
    )
    result = await client.publish(draft, auth=PublishAuth("secret"))
    assert result.asset_id == "asset-1"


@pytest.mark.asyncio
async def test_regular_files_work_without_platform_specific_open_flags(
    draft, monkeypatch
):
    import os

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)

    async def handler(request):
        return httpx.Response(200, json={"code": 200, "data": response_data(draft)})

    result = await HubPublishClient(
        base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
    ).publish(draft, auth=PublishAuth("secret"))
    assert result.asset_id == "asset-1"


@pytest.mark.asyncio
async def test_initial_read_failure_is_known_not_submitted(draft, monkeypatch):
    import asyncio

    async def fail_read(*args, **kwargs):
        raise OSError("private-path-secret")

    monkeypatch.setattr(asyncio, "to_thread", fail_read)
    with pytest.raises(PublishUploadError) as exc:
        await HubPublishClient(base_url="https://hub.example.test").publish(
            draft, auth=PublishAuth("secret")
        )
    assert not exc.value.outcome_unknown
    assert "private-path-secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_http_request_timeout_is_uncertain(draft):
    async def handler(request):
        return httpx.Response(408)

    with pytest.raises(PublishUploadError) as exc:
        await HubPublishClient(
            base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
        ).publish(draft, auth=PublishAuth("secret"))
    assert exc.value.outcome_unknown


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_compression", "nested_200", "nested_500"])
async def test_malformed_wire_response_is_safe_and_uncertain(draft, failure):
    calls = []

    async def handler(request):
        calls.append(request)
        if failure == "invalid_compression":
            return httpx.Response(
                200, content=b"private-secret", headers={"Content-Encoding": "gzip"}
            )
        return httpx.Response(
            200 if failure == "nested_200" else 500, content=b"[" * 1100 + b"]" * 1100
        )

    with pytest.raises(PublishUploadError) as exc:
        await HubPublishClient(
            base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
        ).publish(draft, auth=PublishAuth("secret"))
    assert exc.value.outcome_unknown
    assert len(calls) == 1
    assert "private-secret" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["overwrite", "truncate", "read_error"])
async def test_changes_after_preflight_are_uncertain_and_never_retried(
    draft, mutation, monkeypatch
):
    import asyncio

    calls = []

    class MutatingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request)
            if mutation == "truncate":
                draft.artifact_path.write_bytes(b"")
            elif mutation == "overwrite":
                data = bytearray(draft.artifact_path.read_bytes())
                data[-1] ^= 1
                draft.artifact_path.write_bytes(data)
            else:

                async def fail_read(*args, **kwargs):
                    raise OSError("private-path-secret")

                monkeypatch.setattr(asyncio, "to_thread", fail_read)
            async for chunk in request.stream:
                pass
            return httpx.Response(200, json={"code": 200, "data": response_data(draft)})

    with pytest.raises(PublishUploadError) as exc:
        await HubPublishClient(
            base_url="https://hub.example.test", transport=MutatingTransport()
        ).publish(draft, auth=PublishAuth("secret"))
    assert exc.value.outcome_unknown
    assert exc.value.code == (
        "artifact_read_failed" if mutation == "read_error" else "artifact_changed"
    )
    assert len(calls) == 1
    assert "private-path-secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_compressed_response_is_rejected_before_decoding(draft):
    import gzip

    read_chunks = []
    request_encodings = []
    compressed = gzip.compress(b"x" * (2 * 1024 * 1024))

    class CompressedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            read_chunks.append(1)
            yield compressed

    async def handler(request):
        request_encodings.append(request.headers.get("accept-encoding"))
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=CompressedBody()
        )

    with pytest.raises(PublishUploadError) as exc:
        await HubPublishClient(
            base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
        ).publish(draft, auth=PublishAuth("secret"))
    assert read_chunks == []
    assert request_encodings == ["identity"]
    assert exc.value.outcome_unknown


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["pending_moderation", "publish_success", "published", "publish_failed"]
)
async def test_null_visibility_preserves_confirmed_upload_without_claiming_visibility(
    draft, state
):
    calls = []

    async def handler(request):
        calls.append(request.method)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    **response_data(draft),
                    "visibility": None,
                    "publish_result": state,
                },
            },
        )

    result = await HubPublishClient(
        base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
    ).publish(draft, auth=PublishAuth("user-token"))
    assert result.asset_id == "asset-1"
    assert result.publish_result == state
    assert result.visibility is None
    assert calls == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"visibility": "public"},
        {"name": "wrong"},
        {"version": "wrong"},
        {"publish_result": None},
        {"publish_result": "unrecognized"},
    ],
)
async def test_missing_visibility_does_not_bypass_other_response_checks(draft, change):
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {**response_data(draft), "visibility": None, **change},
            },
        )

    with pytest.raises(PublishUploadError) as error:
        await HubPublishClient(
            base_url="https://hub.example.test", transport=httpx.MockTransport(handler)
        ).publish(draft, auth=PublishAuth("user-token"))
    assert error.value.outcome_unknown
