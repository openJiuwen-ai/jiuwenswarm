# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Normalized Team Skills Hub response models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class HubPayloadError(ValueError):
    """Raised when Hub data cannot satisfy the public identity contract."""


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HubPayloadError(f"Hub payload missing {key}")
    return value.strip()


def _optional_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _string_tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )


@dataclass(frozen=True, slots=True)
class HubCatalogItem:
    asset_id: str
    asset_type: str
    plugin_type: str
    name: str
    display_name: str
    short_description: str
    latest_version: str
    public_latest_version: str
    icon_uri: str
    tags: tuple[str, ...]
    publisher_id: str
    publisher_name: str
    install_count: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HubCatalogItem":
        asset_id = _required_string(payload, "asset_id")
        name = _optional_string(payload, "name") or asset_id
        latest = _optional_string(payload, "latest_version")
        public_latest = _optional_string(payload, "public_latest_version")
        raw_count = payload.get("install_count", 0)
        try:
            install_count = max(0, int(raw_count or 0))
        except (TypeError, ValueError):
            install_count = 0
        return cls(
            asset_id=asset_id,
            asset_type=_optional_string(payload, "asset_type"),
            plugin_type=_optional_string(payload, "plugin_type"),
            name=name,
            display_name=_optional_string(payload, "display_name") or name,
            short_description=_optional_string(payload, "short_desc"),
            latest_version=latest,
            public_latest_version=public_latest,
            icon_uri=_optional_string(payload, "icon_uri"),
            tags=_string_tuple(payload, "tags"),
            publisher_id=_optional_string(payload, "publisher_id"),
            publisher_name=_optional_string(payload, "publisher_name"),
            install_count=install_count,
        )


@dataclass(frozen=True, slots=True)
class HubVersionDetail:
    asset_id: str
    asset_type: str
    plugin_type: str
    version: str
    name: str
    display_name: str
    short_description: str
    detail_description: str
    icon_uri: str
    tags: tuple[str, ...]
    publisher_id: str
    publisher_name: str
    changelog: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HubVersionDetail":
        asset_id = _required_string(payload, "asset_id")
        name = _optional_string(payload, "name") or asset_id
        return cls(
            asset_id=asset_id,
            asset_type=_optional_string(payload, "asset_type"),
            plugin_type=_optional_string(payload, "plugin_type"),
            version=_required_string(payload, "version"),
            name=name,
            display_name=_optional_string(payload, "display_name") or name,
            short_description=_optional_string(payload, "short_desc"),
            detail_description=_optional_string(payload, "detail_desc"),
            icon_uri=_optional_string(payload, "icon_uri"),
            tags=_string_tuple(payload, "tags"),
            publisher_id=_optional_string(payload, "publisher_id"),
            publisher_name=_optional_string(payload, "publisher_name"),
            changelog=_optional_string(payload, "changelog"),
        )


@dataclass(frozen=True, slots=True)
class HubArtifact:
    asset_id: str
    plugin_type: str
    version: str
    download_url: str
    checksum_sha256: str
    asset_type: str = ""
    name: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HubArtifact":
        return cls(
            asset_id=_required_string(payload, "asset_id"),
            plugin_type=_optional_string(payload, "plugin_type"),
            version=_required_string(payload, "version"),
            download_url=_required_string(payload, "download_url"),
            checksum_sha256=_optional_string(payload, "checksum_sha256").lower(),
            asset_type=_optional_string(payload, "asset_type"),
            name=_optional_string(payload, "name"),
        )


@dataclass(frozen=True, slots=True)
class HubPage:
    items: tuple[HubCatalogItem, ...]
    total: int
    page: int
    page_size: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HubPage":
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise HubPayloadError("Hub payload missing items")
        items = tuple(
            HubCatalogItem.from_payload(item)
            for item in raw_items
            if isinstance(item, dict)
        )
        return cls(
            items=items,
            total=int(payload.get("total") or len(items)),
            page=int(payload.get("page") or 1),
            page_size=int(payload.get("page_size") or len(items)),
        )
