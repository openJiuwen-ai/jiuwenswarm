# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SwarmSkillHub protocol adapter for the common Skill Source SPI."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from jiuwenswarm.extensions.sdk.skill_source import (
    ArtifactDescriptor,
    ArtifactRef,
    InstalledArtifact,
    ProviderInvocationContext,
    SkillCandidate,
    SkillRef,
    SkillSearchRequest,
    SkillSearchResult,
    SkillSourceProvider,
    SkillUpdateStatus,
)

SWARM_SKILL_HUB_SOURCE_ID = "swarmskillhub"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on"}


def _timestamp(value: Any) -> datetime | str | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return int(value)
    return _text(value)


class SwarmSkillHubProvider(SkillSourceProvider):
    """Maps `/api/v1/plugins` and `/api/v1/artifacts` to the v2 SPI."""

    source_id = SWARM_SKILL_HUB_SOURCE_ID
    provider_type = "swarmskillhub"
    display_name = "SwarmSkillHub"
    capabilities = frozenset({"search", "check_updates", "get_artifact"})

    def __init__(
        self,
        base_url: str,
        *,
        source_id: str = SWARM_SKILL_HUB_SOURCE_ID,
        display_name: str = "SwarmSkillHub",
        timeout: float = 60.0,
    ) -> None:
        self.source_id = _text(source_id)
        self.display_name = _text(display_name) or self.source_id
        self._base_url = str(base_url or "").strip().rstrip("/")
        if not self._base_url:
            raise ValueError("SwarmSkillHub base_url is required")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _get_data(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self._client is None:
            await self.start()
        client = self._client
        if client is None:
            raise RuntimeError("SwarmSkillHub 客户端未初始化")
        try:
            response = await client.get(f"{self._base_url}{path}", params=params)
        except Exception as exc:
            raise RuntimeError(f"无法连接 SwarmSkillHub: {exc}") from exc
        if not response.is_success:
            detail = (response.text or "").strip()[:300]
            raise RuntimeError(f"SwarmSkillHub API 错误 HTTP {response.status_code}: {detail}")
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"SwarmSkillHub API 响应不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("SwarmSkillHub API 响应格式错误")
        try:
            code = int(payload.get("code", 200))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("SwarmSkillHub API code 格式错误") from exc
        if code != 200:
            raise RuntimeError(_text(payload.get("message")) or "SwarmSkillHub API 返回失败")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("SwarmSkillHub API data 格式错误")
        return data

    async def search(
        self,
        request: SkillSearchRequest,
        context: ProviderInvocationContext,
    ) -> SkillSearchResult:
        del context
        filters = dict(request.filters)
        query: dict[str, Any] = {
            "page": request.page,
            "page_size": request.page_size,
            "order_by": _text(filters.get("order_by")) or "install_count",
            "desc": str(_bool_value(filters.get("desc"), True)).lower(),
        }
        if request.q:
            query["search_keyword"] = request.q
        for public_name, remote_name in (
            ("skill_type", "plugin_type"),
            ("author", "publisher_name"),
            ("asset_id", "asset_id"),
            ("asset_type", "asset_type"),
            ("publisher_id", "publisher_id"),
        ):
            value = _text(filters.get(public_name))
            if value:
                query[remote_name] = value

        data = await self._get_data("/api/v1/plugins", query)
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise RuntimeError("SwarmSkillHub items 格式错误")
        items: list[SkillCandidate] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise RuntimeError("SwarmSkillHub item 格式错误")
            skill_id = _text(raw.get("asset_id") or raw.get("skill_id") or raw.get("id"))
            name = _text(raw.get("name")) or skill_id
            version_id = _text(raw.get("latest_version_id") or raw.get("version_id") or raw.get("latest_version"))
            if not skill_id or not version_id or not name:
                raise RuntimeError("SwarmSkillHub item 缺少 asset_id/latest_version/name")
            tags = raw.get("labels", raw.get("tags", []))
            labels: tuple[Mapping[str, Any], ...] = tuple(
                item if isinstance(item, dict) else {"name": _text(item)}
                for item in (tags if isinstance(tags, list) else [])
                if isinstance(item, dict) or _text(item)
            )
            versions = raw.get("versions", [])
            items.append(
                SkillCandidate(
                    source_id=self.source_id,
                    skill_id=skill_id,
                    version_id=version_id,
                    name=name,
                    display_name=_text(raw.get("display_name")) or name,
                    summary=_text(raw.get("short_desc") or raw.get("summary") or raw.get("description")) or None,
                    version=_text(raw.get("latest_version") or raw.get("version")) or None,
                    fingerprint=_text(raw.get("fingerprint")) or None,
                    namespace=_text(raw.get("namespace") or raw.get("space_name")) or None,
                    slug=_text(raw.get("slug")) or None,
                    canonical_slug=_text(raw.get("canonical_slug")) or None,
                    owner_display_name=_text(raw.get("publisher_name") or raw.get("author")) or None,
                    labels=labels,
                    homepage=_text(raw.get("homepage") or raw.get("detail_url")) or None,
                    rating_avg=_float_or_none(raw.get("rating_avg", raw.get("rating"))),
                    rating_count=_int_or_none(raw.get("rating_count")),
                    download_count=_int_or_none(raw.get("download_count", raw.get("install_count"))),
                    updated_at=_timestamp(raw.get("updated_at", raw.get("update_time"))),
                    versions=tuple(v for v in versions if isinstance(v, dict)) if isinstance(versions, list) else (),
                    previous_version=_text(raw.get("previous_version")) or None,
                    accessible=_bool_value(raw["accessible"]) if "accessible" in raw else None,
                    downloadable=_bool_value(raw.get("downloadable"), True),
                    metadata={"asset_type": raw.get("asset_type")} if raw.get("asset_type") is not None else {},
                )
            )
        total = _int_or_none(data.get("total", data.get("total_count")))
        next_page = request.page + 1 if total is not None and request.page * request.page_size < total else None
        return SkillSearchResult(items=tuple(items), total=total, next_page=next_page)

    async def check_updates(
        self,
        installed: list[InstalledArtifact] | tuple[InstalledArtifact, ...],
        context: ProviderInvocationContext,
    ) -> tuple[SkillUpdateStatus, ...]:
        statuses: list[SkillUpdateStatus] = []
        for current in installed:
            if current.source_id != self.source_id:
                raise RuntimeError("InstalledArtifact source_id 与 Provider 不一致")
            result = await self.search(
                SkillSearchRequest(
                    page=1,
                    page_size=2,
                    filters={"asset_id": current.skill_id},
                ),
                context,
            )
            candidate = next(
                (item for item in result.items if item.skill_id == current.skill_id),
                None,
            )
            if candidate is None:
                statuses.append(
                    SkillUpdateStatus(
                        skill_ref=SkillRef(self.source_id, current.skill_id),
                        current_version_id=current.version_id,
                        latest_version_id=None,
                        has_update=False,
                        accessible=False,
                        downloadable=False,
                        remote_status="removed",
                    )
                )
                continue
            accessible = candidate.accessible
            downloadable = candidate.downloadable
            remote_status = "available"
            if accessible is False:
                remote_status = "no_access"
            fingerprint_matched = None
            if current.fingerprint and candidate.fingerprint:
                fingerprint_matched = current.fingerprint == candidate.fingerprint
            statuses.append(
                SkillUpdateStatus(
                    skill_ref=SkillRef(self.source_id, current.skill_id),
                    current_version_id=current.version_id,
                    latest_version_id=candidate.version_id,
                    has_update=(
                        remote_status == "available"
                        and candidate.version_id != current.version_id
                        and fingerprint_matched is not True
                    ),
                    latest_version=candidate.version,
                    fingerprint_matched=fingerprint_matched,
                    accessible=accessible,
                    downloadable=downloadable,
                    remote_status=remote_status,
                )
            )
        return tuple(statuses)

    async def get_artifact(
        self,
        skill_ref: SkillRef,
        version_id: str,
        context: ProviderInvocationContext,
    ) -> ArtifactDescriptor:
        del context
        if skill_ref.source_id != self.source_id:
            raise RuntimeError("SkillRef source_id 与 Provider 不一致")
        data = await self._get_data(
            f"/api/v1/artifacts/{skill_ref.skill_id}",
            {"version": version_id},
        )
        download_url = _text(data.get("download_url"))
        if not download_url:
            raise RuntimeError("SwarmSkillHub 未返回 download_url")
        metadata: dict[str, Any] = {}
        for key in (
            "display_name",
            "short_desc",
            "version",
            "author",
            "publisher_name",
            "owner_display_name",
        ):
            if key in data and data[key] is not None:
                metadata[key] = data[key]
        return ArtifactDescriptor(
            artifact_ref=ArtifactRef(skill_ref=skill_ref, version_id=version_id),
            download_url=download_url,
            checksum_sha256=_text(data.get("checksum_sha256")) or None,
            artifact_sha256=_text(data.get("artifact_sha256")) or None,
            signature=_text(data.get("signature")) or None,
            signature_algorithm=_text(
                data.get("signature_algorithm") or data.get("signatureAlgorithm")
            ) or None,
            signature_encoding=_text(
                data.get("signature_encoding") or data.get("signatureEncoding")
            ) or None,
            signature_scope=_text(
                data.get("signature_scope") or data.get("signatureScope")
            ) or None,
            key_id=_text(data.get("key_id") or data.get("keyId")) or None,
            fingerprint=_text(data.get("fingerprint")) or None,
            content_length=_int_or_none(data.get("content_length")),
            metadata=metadata,
        )
