# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm Connector Hub adapter maps HubAssetPort MCP APIs to SPI."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest

from jiuwenswarm.extensions.swarm_connector_hub.extension import (
    SwarmConnectorHubSourceExtension,
    trust_policy_from_config,
)
from jiuwenswarm.extensions.swarm_connector_hub.hub_verify import (
    HubTrustPolicy,
    resolve_env_secret,
    verify_hub_connector_artifact,
)
from jiuwenswarm.extensions.swarm_connector_hub.provider import (
    SwarmConnectorHubProvider,
)
from jiuwenswarm.extensions.sdk.connector_source import (
    ConnectorArtifactDescriptor,
    ConnectorArtifactVerificationError,
    ConnectorRef,
    ConnectorSearchRequest,
    ConnectorSourceConfig,
    InstalledConnectorArtifact,
    ProviderInvocationContext,
)
from jiuwenswarm.server.runtime.marketplace.hub_client import (
    HubClient,
    HttpHubTransport,
)
from jiuwenswarm.extensions.swarm_connector_hub.constants import (
    OPENJIUWEN_HUB_PROVIDER_TYPE,
    OPENJIUWEN_HUB_SOURCE_ID,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetQuery,
    HubDownloadRequest,
    HubResolvedDownload,
    HubSearchPage,
    HubSearchRequest,
    HubAssetSummary,
)


class _FakeHubPort:
    async def search_assets(self, request: HubSearchRequest) -> HubSearchPage:
        assert request.kind == "mcp"
        item = HubAssetSummary(
            kind="mcp",
            asset_id="asset-1",
            display_name="Demo MCP",
            short_description="desc",
            public_latest_version="1.2.0",
            icon_uri="https://example.invalid/icon.png",
            tags=("demo",),
            package_name="demo-mcp",
            category_name="tools",
        )
        return HubSearchPage(
            items=(item,), total=1, page=request.page, page_size=request.page_size
        )

    async def query_asset(self, request: HubAssetQuery) -> HubAssetDetail:
        assert request.kind == "mcp"
        return HubAssetDetail(
            kind="mcp",
            asset_id=request.asset_id,
            version="1.2.0",
            display_name="Demo MCP",
            short_description="desc",
            detail_description="long",
            icon_uri="",
            tags=("demo",),
            package_name="demo-mcp",
            category_name="tools",
        )

    async def resolve_download(self, request: HubDownloadRequest) -> HubResolvedDownload:
        assert request.kind == "mcp"
        return HubResolvedDownload(
            kind="mcp",
            asset_id=request.asset_id,
            version=request.version,
            download_url="https://example.invalid/demo-mcp.zip",
            checksum_sha256="abc",
            package_name="demo-mcp",
        )


def test_extension_default_source_config() -> None:
    extension = SwarmConnectorHubSourceExtension()
    config = extension.default_source_config()
    assert config is not None
    assert config.source_id == OPENJIUWEN_HUB_SOURCE_ID
    assert config.provider_type == OPENJIUWEN_HUB_PROVIDER_TYPE
    assert "search" in config.capabilities


def test_provider_search_detail_artifact_and_updates() -> None:
    provider = SwarmConnectorHubProvider(hub_port=_FakeHubPort())
    ctx = ProviderInvocationContext(request_id="t1")

    search = asyncio.run(provider.search(ConnectorSearchRequest(q="demo"), ctx))
    assert len(search.items) == 1
    assert search.items[0].source_id == OPENJIUWEN_HUB_SOURCE_ID
    assert search.items[0].package_name == "demo-mcp"
    assert search.items[0].to_dict()["source_id"] == OPENJIUWEN_HUB_SOURCE_ID

    ref = ConnectorRef(source_id=OPENJIUWEN_HUB_SOURCE_ID, asset_id="asset-1")
    detail = asyncio.run(provider.get_detail(ref, "1.2.0", ctx))
    assert search.items[0].version_id == "1.2.0"
    assert detail.version_id == "1.2.0"

    artifact = asyncio.run(provider.get_artifact(ref, "1.2.0", ctx))
    assert artifact.download_url.endswith("demo-mcp.zip")
    assert artifact.content_sha256 == "abc"

    updates = asyncio.run(
        provider.check_updates(
            [
                InstalledConnectorArtifact(
                    source_id=OPENJIUWEN_HUB_SOURCE_ID,
                    asset_id="asset-1",
                    version_id="1.0.0",
                    package_name="demo-mcp",
                )
            ],
            ctx,
        )
    )
    assert len(updates) == 1
    assert updates[0].has_update is True
    assert updates[0].latest_version_id == "1.2.0"


def test_resolve_env_secret_and_trust_policy_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECTOR_HUB_TEST_SECRET", "secret-value")
    assert resolve_env_secret("env://CONNECTOR_HUB_TEST_SECRET") == "secret-value"
    assert resolve_env_secret("env://MISSING_SECRET") is None
    assert resolve_env_secret("plain-secret") is None
    assert resolve_env_secret("env://bad-name") is None

    policy = trust_policy_from_config()
    assert policy.verification == "off"
    assert policy.hmac_key_id
    assert policy.hmac_secret_ref.startswith("env://")


def test_unknown_verification_rejected_at_policy_construction() -> None:
    with pytest.raises(ValueError, match="invalid HubTrustPolicy.verification"):
        HubTrustPolicy(verification="require")
    with pytest.raises(ValueError, match="invalid HubTrustPolicy.verification"):
        HubTrustPolicy(verification="")
    normalized = HubTrustPolicy(verification="  Required ")
    assert normalized.verification == "required"


def test_verify_hub_connector_artifact_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "unit-test-hmac-secret"
    monkeypatch.setenv("CONNECTOR_HUB_HMAC_TEST", secret)
    content = b"pk-fake-zip-body"
    content_sha256 = hashlib.sha256(content).hexdigest()
    asset_id = "asset-1"
    version_id = "1.2.0"
    signing_input = f"skillhub-artifact-v1\n{asset_id}\n{version_id}\n{content_sha256}"
    signature = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    descriptor = ConnectorArtifactDescriptor(
        source_id=OPENJIUWEN_HUB_SOURCE_ID,
        asset_id=asset_id,
        version_id=version_id,
        download_url="https://example.invalid/demo-mcp.zip",
        content_sha256=content_sha256,
        signature=signature,
        signature_algorithm="HmacSHA256",
        signature_encoding="hex-lower",
        signature_scope="skillhub-artifact-v1",
        key_id="default",
    )
    policy = HubTrustPolicy(
        verification="required",
        hmac_key_id="default",
        hmac_secret_ref="env://CONNECTOR_HUB_HMAC_TEST",
    )
    result = verify_hub_connector_artifact(descriptor, content, trust_policy=policy)
    assert result is not None
    assert result.verified is True
    assert result.key_id == "default"

    with pytest.raises(ConnectorArtifactVerificationError):
        verify_hub_connector_artifact(
            descriptor,
            content,
            trust_policy=HubTrustPolicy(
                verification="required",
                hmac_key_id="other",
                hmac_secret_ref="env://CONNECTOR_HUB_HMAC_TEST",
            ),
        )

    monkeypatch.delenv("CONNECTOR_HUB_HMAC_TEST", raising=False)
    with pytest.raises(ConnectorArtifactVerificationError):
        verify_hub_connector_artifact(descriptor, content, trust_policy=policy)


def test_verify_if_present_allows_unsigned() -> None:
    content = b"unsigned"
    content_sha256 = hashlib.sha256(content).hexdigest()
    descriptor = ConnectorArtifactDescriptor(
        source_id=OPENJIUWEN_HUB_SOURCE_ID,
        asset_id="asset-1",
        version_id="1.0.0",
        download_url="https://example.invalid/demo-mcp.zip",
        content_sha256=content_sha256,
    )
    assert (
        verify_hub_connector_artifact(
            descriptor,
            content,
            trust_policy=HubTrustPolicy(verification="if-present"),
        )
        is None
    )


def test_verify_off_skips_hmac_even_when_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "unit-test-hmac-secret"
    monkeypatch.setenv("CONNECTOR_HUB_HMAC_TEST", secret)
    content = b"signed-but-off"
    content_sha256 = hashlib.sha256(content).hexdigest()
    asset_id = "asset-1"
    version_id = "1.0.0"
    signature = hmac.new(
        secret.encode("utf-8"),
        f"skillhub-artifact-v1\n{asset_id}\n{version_id}\n{content_sha256}".encode(),
        hashlib.sha256,
    ).hexdigest()
    descriptor = ConnectorArtifactDescriptor(
        source_id=OPENJIUWEN_HUB_SOURCE_ID,
        asset_id=asset_id,
        version_id=version_id,
        download_url="https://example.invalid/demo-mcp.zip",
        content_sha256=content_sha256,
        signature=signature,
        signature_algorithm="HmacSHA256",
        signature_encoding="hex-lower",
        signature_scope="skillhub-artifact-v1",
        key_id="default",
    )
    assert (
        verify_hub_connector_artifact(
            descriptor,
            content,
            trust_policy=HubTrustPolicy(
                verification="off",
                hmac_secret_ref="env://CONNECTOR_HUB_HMAC_TEST",
            ),
        )
        is None
    )


def test_extension_forwards_verify_ssl_into_hub_client() -> None:
    extension = SwarmConnectorHubSourceExtension()
    provider = extension.create_provider(
        ConnectorSourceConfig(
            source_id=OPENJIUWEN_HUB_SOURCE_ID,
            provider_type=OPENJIUWEN_HUB_PROVIDER_TYPE,
            endpoint_ref="https://hub.example.test",
            verify_ssl=False,
        )
    )
    assert isinstance(provider, SwarmConnectorHubProvider)
    assert provider.verify_ssl is False
    hub = provider._hub
    assert isinstance(hub, HubClient)
    assert isinstance(hub.transport, HttpHubTransport)
    assert hub.transport.verify is False
