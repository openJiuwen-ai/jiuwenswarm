# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Connector Source SPI：真源在 gateway；母仓路径为同一类身份的转发别名。"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

import agent_gateway.extensions.sdk.connector_source as gw_connector_source
from agent_gateway.extensions.sdk.base import BaseExtension
import jiuwenswarm.extensions.sdk.connector_source as connector_source
from jiuwenswarm.extensions.sdk.connector_source import (
    DownloadPolicy,
    ProviderInvocationContext,
)


def _zip_bytes(payload: bytes = b"connector-package") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", '{"name":"demo"}')
        archive.writestr("payload.bin", payload)
    return buffer.getvalue()


class DummyConnectorProvider(connector_source.ConnectorSourceProvider):
    source_id = "dummy"
    provider_type = "dummy"
    display_name = "Dummy"
    capabilities = frozenset({"search", "get_artifact", "get_detail"})

    def __init__(self, *, body: bytes | None = None) -> None:
        self._body = body if body is not None else _zip_bytes()
        self._sha256 = hashlib.sha256(self._body).hexdigest()

    async def search(self, request, context):
        del request, context
        item = connector_source.ConnectorCandidate(
            source_id=self.source_id,
            asset_id="asset-1",
            package_name="demo-connector",
            display_name="Demo Connector",
            version_id="1.0.0",
            short_description="dummy",
        )
        return connector_source.ConnectorSearchResult(
            items=(item,), total=1, next_page=None
        )

    async def get_detail(self, connector_ref, version_id, context):
        del context
        return connector_source.ConnectorDetail(
            source_id=connector_ref.source_id,
            asset_id=connector_ref.asset_id,
            package_name="demo-connector",
            display_name="Demo Connector",
            version_id=str(version_id or "1.0.0"),
            detail_description="detail",
        )

    async def get_artifact(self, connector_ref, version_id, context):
        del context
        return connector_source.ConnectorArtifactDescriptor(
            source_id=connector_ref.source_id,
            asset_id=connector_ref.asset_id,
            version_id=version_id,
            download_url="https://example.invalid/demo.zip",
            content_sha256=self._sha256,
            package_name="demo-connector",
        )

    async def download_artifact(self, descriptor, download_policy=None):
        del descriptor, download_policy
        return self._body


class UnsupportedDetailProvider(DummyConnectorProvider):
    capabilities = frozenset({"search", "get_artifact"})

    async def get_detail(self, connector_ref, version_id, context):
        return await connector_source.ConnectorSourceProvider.get_detail(
            self, connector_ref, version_id, context
        )


def test_connector_source_shim_shares_class_identity() -> None:
    assert connector_source.ConnectorRef is gw_connector_source.ConnectorRef
    assert (
        connector_source.ConnectorSourceProvider
        is gw_connector_source.ConnectorSourceProvider
    )
    assert (
        connector_source.ConnectorSourceExtension
        is gw_connector_source.ConnectorSourceExtension
    )
    assert issubclass(connector_source.ConnectorSourceExtension, BaseExtension)
    assert connector_source.ConnectorSourceExtension.__bases__[0] is BaseExtension
    assert not hasattr(connector_source, "ConnectorArtifactRef")


@pytest.mark.asyncio
async def test_connector_source_exports_core_types() -> None:
    assert connector_source.ConnectorRef is not None
    assert connector_source.ConnectorSourceProvider is not None
    assert connector_source.ConnectorSourceExtension is not None
    assert not hasattr(connector_source, "OPENJIUWEN_HUB_SOURCE_ID")


@pytest.mark.asyncio
async def test_dummy_provider_search_get_artifact_download() -> None:
    provider = DummyConnectorProvider()
    context = ProviderInvocationContext(request_id="req-1")
    search = await provider.search(
        connector_source.ConnectorSearchRequest(q="demo"), context
    )
    assert len(search.items) == 1
    assert search.items[0].source_id == "dummy"
    assert search.items[0].to_dict()["source_id"] == "dummy"
    assert "source" not in search.items[0].to_dict()

    ref = connector_source.ConnectorRef(source_id="dummy", asset_id="asset-1")
    detail = await provider.get_detail(ref, "1.0.0", context)
    assert detail.package_name == "demo-connector"
    assert detail.version_id == "1.0.0"

    descriptor = await provider.get_artifact(ref, "1.0.0", context)
    body = await provider.download_artifact(descriptor, DownloadPolicy())
    assert body.startswith(b"PK")
    assert hashlib.sha256(body).hexdigest() == descriptor.content_sha256


@pytest.mark.asyncio
async def test_default_download_rejects_non_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = DummyConnectorProvider()

    class _Resp:
        is_redirect = False
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            yield b"not-a-zip"

    class _StreamContext:
        def __init__(self, response: _Resp) -> None:
            self._response = response

        async def __aenter__(self):
            return self._response

        async def __aexit__(self, *args):
            del args
            return None

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args
            return None

        def stream(self, method: str, url: str):
            del method, url
            return _StreamContext(_Resp())

    # download_artifact 定义在 gateway 真源模块，需 patch 该模块绑定的 httpx。
    monkeypatch.setattr(gw_connector_source.httpx, "AsyncClient", _Client)
    descriptor = connector_source.ConnectorArtifactDescriptor(
        source_id="dummy",
        asset_id="a",
        version_id="1",
        download_url="https://example.invalid/demo.bin",
    )
    with pytest.raises(connector_source.ConnectorDownloadError, match="ZIP") as download_exc:
        await connector_source.ConnectorSourceProvider.download_artifact(
            provider, descriptor
        )
    assert download_exc.value.code == "not_zip"


@pytest.mark.asyncio
async def test_get_detail_default_raises_capability_error() -> None:
    provider = UnsupportedDetailProvider()
    with pytest.raises(connector_source.ConnectorCapabilityError) as exc:
        await provider.get_detail(
            connector_source.ConnectorRef(source_id="dummy", asset_id="a"),
            "1.0.0",
            ProviderInvocationContext(request_id="req-2"),
        )
    assert exc.value.code == "capability_unsupported"
