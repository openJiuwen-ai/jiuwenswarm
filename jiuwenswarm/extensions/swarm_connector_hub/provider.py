# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OpenJiuwen Hub MCP catalogue adapter for the Connector Source SPI."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from jiuwenswarm.extensions.sdk.connector_source import (
    ConnectorArtifactDescriptor,
    ConnectorCandidate,
    ConnectorDetail,
    ConnectorRef,
    ConnectorSearchRequest,
    ConnectorSearchResult,
    ConnectorSourceProvider,
    ConnectorUpdateStatus,
    ConnectorVerificationResult,
    InstalledConnectorArtifact,
    ProviderInvocationContext,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetPort,
    HubAssetQuery,
    HubDownloadRequest,
    HubSearchRequest,
    create_default_hub_asset_port,
)

from .constants import OPENJIUWEN_HUB_PROVIDER_TYPE, OPENJIUWEN_HUB_SOURCE_ID
from .hub_verify import HubTrustPolicy, verify_hub_connector_artifact

_DEFAULT_DISPLAY_NAME = "OpenJiuwen Hub"


class SwarmConnectorHubProvider(ConnectorSourceProvider):
    """Maps HubAssetPort MCP APIs onto ConnectorSourceProvider."""

    provider_type = OPENJIUWEN_HUB_PROVIDER_TYPE
    capabilities = frozenset({"search", "get_artifact", "get_detail", "check_updates"})

    def __init__(
        self,
        *,
        source_id: str | None = None,
        display_name: str | None = None,
        hub_port: HubAssetPort | None = None,
        verify_ssl: bool = True,
        download_allowed_hosts: tuple[str, ...] = (),
        trust_policy: HubTrustPolicy | None = None,
    ) -> None:
        # None / blank → product defaults (single fallback site).
        self.source_id = str(source_id or "").strip() or OPENJIUWEN_HUB_SOURCE_ID
        self.display_name = str(display_name or "").strip() or _DEFAULT_DISPLAY_NAME
        self.verify_ssl = verify_ssl
        self.download_allowed_hosts = tuple(download_allowed_hosts)
        self._hub = hub_port or create_default_hub_asset_port()
        self._hub_trust_policy = trust_policy or HubTrustPolicy()

    async def search(
        self,
        request: ConnectorSearchRequest,
        context: ProviderInvocationContext,
    ) -> ConnectorSearchResult:
        del context
        page = await self._hub.search_assets(
            HubSearchRequest(
                kind="mcp",
                query=str(request.q or "").strip(),
                page=max(1, int(request.page or 1)),
                page_size=max(1, min(int(request.page_size or 20), 100)),
            )
        )
        items = tuple(
            ConnectorCandidate(
                source_id=self.source_id,
                asset_id=item.asset_id,
                package_name=item.package_name or item.asset_id,
                display_name=item.display_name or item.package_name or item.asset_id,
                version_id=str(item.public_latest_version or "").strip(),
                short_description=item.short_description,
                icon_uri=item.icon_uri,
                tags=tuple(item.tags),
                category_name=item.category_name,
            )
            for item in page.items
            if item.kind == "mcp"
        )
        next_page = (
            page.page + 1
            if len(items) > 0 and page.page * page.page_size < page.total
            else None
        )
        return ConnectorSearchResult(
            items=items,
            total=page.total,
            next_page=next_page,
        )

    async def get_detail(
        self,
        connector_ref: ConnectorRef,
        version_id: str,
        context: ProviderInvocationContext,
    ) -> ConnectorDetail:
        del context
        if connector_ref.source_id != self.source_id:
            raise ValueError(
                f"source_id mismatch: expected {self.source_id}, got {connector_ref.source_id}"
            )
        detail = await self._hub.query_asset(
            HubAssetQuery(kind="mcp", asset_id=connector_ref.asset_id)
        )
        metadata: dict[str, Any] = {}
        package_profile = getattr(detail, "package_profile", None)
        if isinstance(package_profile, Mapping):
            metadata["package_profile"] = dict(package_profile)
        requested = str(version_id or "").strip()
        version = requested or str(detail.version or "").strip()
        return ConnectorDetail(
            source_id=self.source_id,
            asset_id=detail.asset_id,
            package_name=detail.package_name or detail.asset_id,
            display_name=detail.display_name or detail.package_name or detail.asset_id,
            version_id=version,
            short_description=detail.short_description,
            detail_description=detail.detail_description,
            icon_uri=detail.icon_uri,
            tags=tuple(detail.tags),
            category_name=detail.category_name,
            metadata=metadata,
        )

    async def get_artifact(
        self,
        connector_ref: ConnectorRef,
        version_id: str,
        context: ProviderInvocationContext,
    ) -> ConnectorArtifactDescriptor:
        del context
        if connector_ref.source_id != self.source_id:
            raise ValueError(
                f"source_id mismatch: expected {self.source_id}, got {connector_ref.source_id}"
            )
        version = str(version_id or "").strip()
        if not version:
            detail = await self._hub.query_asset(
                HubAssetQuery(kind="mcp", asset_id=connector_ref.asset_id)
            )
            version = str(detail.version or "").strip()
        if not version:
            raise RuntimeError(f"Hub MCP asset has no version: {connector_ref.asset_id}")
        resolved = await self._hub.resolve_download(
            HubDownloadRequest(
                kind="mcp",
                asset_id=connector_ref.asset_id,
                version=version,
            )
        )
        return ConnectorArtifactDescriptor(
            source_id=self.source_id,
            asset_id=resolved.asset_id,
            version_id=resolved.version,
            download_url=resolved.download_url,
            content_sha256=resolved.checksum_sha256 or None,
            package_name=resolved.package_name,
        )

    async def verify_artifact(
        self,
        descriptor: ConnectorArtifactDescriptor,
        content: bytes,
    ) -> ConnectorVerificationResult | None:
        """Hub-compatible HMAC verify; overrides the generic SPI default."""
        return verify_hub_connector_artifact(
            descriptor,
            content,
            trust_policy=self._hub_trust_policy,
        )

    async def check_updates(
        self,
        installed: Sequence[InstalledConnectorArtifact],
        context: ProviderInvocationContext,
    ) -> Sequence[ConnectorUpdateStatus]:
        del context
        results: list[ConnectorUpdateStatus] = []
        for item in installed:
            if item.source_id and item.source_id != self.source_id:
                continue
            ref = ConnectorRef(source_id=self.source_id, asset_id=item.asset_id)
            try:
                detail = await self._hub.query_asset(
                    HubAssetQuery(kind="mcp", asset_id=item.asset_id)
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    ConnectorUpdateStatus(
                        connector_ref=ref,
                        current_version_id=item.version_id,
                        latest_version_id=None,
                        has_update=False,
                        remote_status="unavailable",
                        error_code=type(exc).__name__,
                    )
                )
                continue
            latest = str(detail.version or "").strip() or None
            current = str(item.version_id or "").strip()
            results.append(
                ConnectorUpdateStatus(
                    connector_ref=ref,
                    current_version_id=current,
                    latest_version_id=latest,
                    has_update=bool(latest and current and latest != current),
                    latest_version=latest,
                    remote_status="ok",
                )
            )
        return results
