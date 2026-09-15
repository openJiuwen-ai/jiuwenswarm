# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic asynchronous client for Team Skills Hub compatible APIs."""

from __future__ import annotations

import os
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlparse

import httpx

from jiuwenswarm.server.runtime.marketplace.hub_models import (
    HubArtifact,
    HubCatalogItem,
    HubPage,
    HubPayloadError,
    HubVersionDetail,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetPort,
    HubAssetQuery,
    HubAssetSummary,
    HubDownloadRequest,
    HubResolvedDownload,
    HubSearchRequest,
    HubSearchPage,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_type_adapter import (
    HubAssetTypeConflictError,
    get_hub_asset_type_contract,
    resolve_hub_asset_kind,
)

DEFAULT_HUB_BASE_URL = "https://swarmskills.openjiuwen.com"
_DEFAULT_HUB_TIMEOUT = 60.0
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def _allow_insecure_http() -> bool:
    return (
        os.getenv("TEAM_SKILLS_HUB_ALLOW_INSECURE_HTTP", "").strip().lower()
        in _TRUE_VALUES
    )


class HubProtocolError(RuntimeError):
    """Raised for malformed or inconsistent Hub responses."""


class HubNotFoundError(HubProtocolError):
    """Raised when an exact Hub asset cannot be found."""


class HubTransport(Protocol):
    async def get_data(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        ...


class HttpHubTransport:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        token: str | None = None,
        system_token: str | None = None,
    ) -> None:
        configured = (
            base_url or os.getenv("TEAM_SKILLS_HUB_BASE_URL") or DEFAULT_HUB_BASE_URL
        )
        self.base_url = configured.strip().rstrip("/")
        parsed = urlparse(self.base_url)
        host = (parsed.hostname or "").lower()
        if not host or parsed.scheme not in {"http", "https"}:
            raise ValueError("Hub base_url must be an absolute HTTP(S) URL")
        if (
            parsed.scheme == "http"
            and host not in {"localhost", "127.0.0.1", "::1"}
            and not _allow_insecure_http()
        ):
            raise ValueError("Hub base_url must use HTTPS outside loopback development")
        self.timeout = timeout or float(
            os.getenv("TEAM_SKILLS_HUB_TIMEOUT", _DEFAULT_HUB_TIMEOUT)
        )
        self.token = (token or "").strip()
        self.system_token = (system_token or "").strip()

    def _headers(self) -> dict[str, str] | None:
        if self.system_token:
            return {"X-System-Token": self.system_token}
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return None

    async def get_data(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        relative = path if path.startswith("/") else f"/{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            ) as client:
                response = await client.get(
                    f"{self.base_url}{relative}",
                    params=params,
                    headers=self._headers(),
                )
        except Exception as exc:
            raise HubProtocolError(f"无法连接 Team Skills Hub: {exc}") from exc
        if response.status_code == 404:
            raise HubNotFoundError("SkillHub 资源不存在")
        if not response.is_success:
            raise HubProtocolError(
                f"Team Skills Hub API 错误 HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise HubProtocolError("Team Skills Hub API 响应不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise HubProtocolError("Team Skills Hub API 响应格式错误")
        try:
            code = int(payload.get("code", 200))
        except (TypeError, ValueError) as exc:
            raise HubProtocolError("Team Skills Hub API 响应 code 格式错误") from exc
        if code != 200:
            raise HubProtocolError("Team Skills Hub API 返回失败")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise HubProtocolError("Team Skills Hub API 响应 data 格式错误")
        return data


class HubClient(HubAssetPort):
    def __init__(
        self,
        *,
        transport: HubTransport | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        token: str | None = None,
        system_token: str | None = None,
    ) -> None:
        self.transport = transport or HttpHubTransport(
            base_url=base_url,
            timeout=timeout,
            token=token,
            system_token=system_token,
        )
        configured_base_url = (
            base_url
            or getattr(self.transport, "base_url", None)
            or os.getenv("TEAM_SKILLS_HUB_BASE_URL")
            or DEFAULT_HUB_BASE_URL
        )
        self.base_url = str(configured_base_url).strip().rstrip("/")

    def _normalize_icon_uri(self, icon_uri: str) -> str:
        icon = str(icon_uri or "").strip()
        if not icon:
            return ""
        parsed = urlparse(icon)
        if parsed.scheme or parsed.netloc:
            return icon
        return urljoin(f"{self.base_url}/", icon)

    async def list_plugins(
        self,
        plugin_type: str,
        *,
        page: int = 1,
        page_size: int = 100,
        search_keyword: str = "",
    ) -> HubPage:
        normalized_type = str(plugin_type or "").strip()
        if not normalized_type:
            raise ValueError("plugin_type is required")
        params: dict[str, Any] = {
            "plugin_type": normalized_type,
            "page": max(1, int(page)),
            "page_size": max(1, min(int(page_size), 100)),
        }
        normalized_query = str(search_keyword or "").strip()
        if normalized_query:
            params["search_keyword"] = normalized_query
        payload = await self.transport.get_data("/api/v1/plugins", params=params)
        try:
            return HubPage.from_payload(payload)
        except (HubPayloadError, TypeError, ValueError) as exc:
            raise HubProtocolError(str(exc)) from exc

    async def get_plugin(
        self, asset_id: str, *, plugin_type: str = ""
    ) -> HubCatalogItem:
        wanted = str(asset_id or "").strip()
        if not wanted:
            raise ValueError("asset_id is required")
        params: dict[str, Any] = {
            "asset_id": wanted,
            "page": 1,
            "page_size": 100,
        }
        normalized_type = str(plugin_type or "").strip()
        if normalized_type:
            params["plugin_type"] = normalized_type
        payload = await self.transport.get_data(
            "/api/v1/plugins",
            params=params,
        )
        try:
            page = HubPage.from_payload(payload)
        except (HubPayloadError, TypeError, ValueError) as exc:
            raise HubProtocolError(str(exc)) from exc
        for item in page.items:
            if item.asset_id == wanted:
                return item
        raise HubNotFoundError(f"SkillHub asset not found: {wanted}")

    async def get_version(self, asset_id: str, version: str) -> HubVersionDetail:
        wanted = str(asset_id or "").strip()
        wanted_version = str(version or "").strip()
        if not wanted or not wanted_version:
            raise ValueError("asset_id and version are required")
        payload = await self.transport.get_data(
            f"/api/v1/plugins/{quote(wanted, safe='')}/versions/"
            f"{quote(wanted_version, safe='')}"
        )
        try:
            detail = HubVersionDetail.from_payload(payload)
        except (HubPayloadError, TypeError, ValueError) as exc:
            raise HubProtocolError(str(exc)) from exc
        if detail.asset_id != wanted or detail.version != wanted_version:
            raise HubProtocolError("Hub version identity mismatch")
        return detail

    async def get_artifact(
        self, asset_id: str, *, version: str | None = None
    ) -> HubArtifact:
        wanted = str(asset_id or "").strip()
        if not wanted:
            raise ValueError("asset_id is required")
        wanted_version = str(version or "").strip()
        params: dict[str, Any] = {"is_cli_download": False}
        if wanted_version:
            params["version"] = wanted_version
        payload = await self.transport.get_data(
            f"/api/v1/artifacts/{quote(wanted, safe='')}", params=params
        )
        try:
            artifact = HubArtifact.from_payload(payload)
        except (HubPayloadError, TypeError, ValueError) as exc:
            raise HubProtocolError(str(exc)) from exc
        if artifact.asset_id != wanted:
            raise HubProtocolError("Hub artifact asset_id mismatch")
        if wanted_version and artifact.version != wanted_version:
            raise HubProtocolError("Hub artifact version mismatch")
        return artifact

    @staticmethod
    def _require_request_kind(
        requested_kind: str, plugin_type: str, asset_type: str
    ) -> None:
        try:
            actual_kind = resolve_hub_asset_kind(plugin_type, asset_type)
        except HubAssetTypeConflictError as exc:
            raise HubProtocolError(str(exc)) from exc
        if actual_kind != requested_kind:
            raise HubProtocolError("Hub asset type mismatch")

    async def search_assets(self, request: HubSearchRequest) -> HubSearchPage:
        contract = get_hub_asset_type_contract(request.kind)
        page = await self.list_plugins(
            contract.hub_plugin_type,
            page=request.page,
            page_size=request.page_size,
            search_keyword=request.query,
        )
        items: list[HubAssetSummary] = []
        for item in page.items:
            self._require_request_kind(
                request.kind, item.plugin_type, item.asset_type
            )
            items.append(
                HubAssetSummary(
                    kind=request.kind,
                    asset_id=item.asset_id,
                    display_name=item.display_name,
                    short_description=item.short_description,
                    public_latest_version=item.public_latest_version,
                    icon_uri=self._normalize_icon_uri(item.icon_uri),
                    tags=item.tags,
                    package_name=item.name,
                )
            )
        return HubSearchPage(
            items=tuple(items),
            total=page.total,
            page=page.page,
            page_size=page.page_size,
        )

    async def query_asset(self, request: HubAssetQuery) -> HubAssetDetail:
        contract = get_hub_asset_type_contract(request.kind)
        item = await self.get_plugin(
            request.asset_id, plugin_type=contract.hub_plugin_type
        )
        self._require_request_kind(request.kind, item.plugin_type, item.asset_type)
        version = item.public_latest_version
        if not version:
            raise HubProtocolError(f"Hub asset has no public version: {request.asset_id}")
        detail = await self.get_version(item.asset_id, version)
        self._require_request_kind(
            request.kind, detail.plugin_type, detail.asset_type
        )
        return HubAssetDetail(
            kind=request.kind,
            asset_id=item.asset_id,
            version=detail.version,
            display_name=detail.display_name,
            short_description=detail.short_description,
            detail_description=detail.detail_description,
            icon_uri=self._normalize_icon_uri(detail.icon_uri or item.icon_uri),
            tags=detail.tags,
            package_name=detail.name or item.name,
        )

    async def resolve_download(
        self, request: HubDownloadRequest
    ) -> HubResolvedDownload:
        artifact = await self.get_artifact(request.asset_id, version=request.version)
        self._require_request_kind(
            request.kind, artifact.plugin_type, artifact.asset_type
        )
        return HubResolvedDownload(
            kind=request.kind,
            asset_id=artifact.asset_id,
            version=artifact.version,
            download_url=artifact.download_url,
            checksum_sha256=artifact.checksum_sha256,
            package_name=artifact.name,
        )
