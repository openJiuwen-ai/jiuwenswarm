# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stable downstream-facing port for remote marketplace assets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

HubAssetKind = Literal["agent_template", "agent_group", "plugin", "mcp", "system_plugin", "app"]


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
    version: str = ""


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
    category_name: str = ""


def hub_asset_matches_visible_query(item: HubAssetSummary, query: str) -> bool:
    """Return whether every search term occurs in a marketplace-visible field."""
    terms = str(query or "").strip().casefold().split()
    if not terms:
        return True
    visible_text = " ".join(
        (
            item.package_name,
            item.display_name,
            item.short_description,
            item.category_name,
            *item.tags,
        )
    ).casefold()
    return all(term in visible_text for term in terms)


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
    category_name: str = ""
    preview_url: str = ""
    # Published package profile from the version endpoint; renders detail cards without
    # downloading the package archive.
    package_profile: dict[str, Any] | None = None
    versions: tuple[str, ...] = ()
    # App-only distribution metadata (absent for other kinds).
    file_size: int | None = None
    source_size: int | None = None
    requires_project: bool | None = None
    permissions: tuple[str, ...] = ()
    has_server_script: bool | None = None
    checksum_sha256: str = ""


@dataclass(frozen=True, slots=True)
class HubResolvedDownload:
    kind: HubAssetKind
    asset_id: str
    version: str
    download_url: str
    checksum_sha256: str
    package_name: str = ""
    file_size: int | None = None


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


def verify_package_hub_tls(kind: HubAssetKind) -> bool:
    """Apply the AgentServer Hub TLS setting to experts and expert teams."""
    if kind not in {"agent_template", "agent_group"}:
        return True
    return os.getenv("TEAM_SKILLS_HUB_VERIFY_SSL", "").strip().lower() not in {"false", "0", "no", "off"}


def create_package_hub_port(kind: HubAssetKind) -> HubAssetPort:
    """Build a Hub client with the TLS policy for the requested package kind."""
    if verify_package_hub_tls(kind):
        return create_default_hub_asset_port()

    from jiuwenswarm.server.runtime.marketplace.hub_client import HubClient

    return HubClient(verify=False)
