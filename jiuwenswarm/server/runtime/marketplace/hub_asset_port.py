# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stable downstream-facing port for remote marketplace assets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

HubAssetKind = Literal["agent_template", "plugin", "mcp"]


@dataclass(frozen=True, slots=True)
class HubSearchRequest:
    kind: HubAssetKind
    query: str = ""
    page: int = 1
    page_size: int = 100


@dataclass(frozen=True, slots=True)
class HubAssetQuery:
    kind: HubAssetKind
    asset_id: str


@dataclass(frozen=True, slots=True)
class HubDownloadRequest:
    kind: HubAssetKind
    asset_id: str
    version: str


@dataclass(frozen=True, slots=True)
class HubAssetSummary:
    kind: HubAssetKind
    asset_id: str
    display_name: str
    short_description: str
    public_latest_version: str
    icon_uri: str
    tags: tuple[str, ...]
    package_name: str = ""


@dataclass(frozen=True, slots=True)
class HubSearchPage:
    items: tuple[HubAssetSummary, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class HubAssetDetail:
    kind: HubAssetKind
    asset_id: str
    version: str
    display_name: str
    short_description: str
    detail_description: str
    icon_uri: str
    tags: tuple[str, ...]
    package_name: str = ""


@dataclass(frozen=True, slots=True)
class HubResolvedDownload:
    kind: HubAssetKind
    asset_id: str
    version: str
    download_url: str
    checksum_sha256: str
    package_name: str = ""


class HubAssetPort(Protocol):
    """Port to be implemented by the SkillHub downstream integration."""

    async def search_assets(self, request: HubSearchRequest) -> HubSearchPage:
        ...

    async def query_asset(self, request: HubAssetQuery) -> HubAssetDetail:
        ...

    async def resolve_download(
        self, request: HubDownloadRequest
    ) -> HubResolvedDownload:
        ...


def create_default_hub_asset_port() -> HubAssetPort:
    """Compatibility factory until the downstream SkillHub provider is wired."""
    from jiuwenswarm.server.runtime.marketplace.hub_client import HubClient

    return HubClient()
