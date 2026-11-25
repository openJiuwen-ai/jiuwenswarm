# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillHub-backed MCP package catalog and install lifecycle.

Enterprise catalogue / show / install require ``source_id`` and route through
Connector Source SPI (remote-only for catalogue). Enterprise mine
(filter=local) stays local-only. Personal edition keeps filter=builtin|local
on the legacy ``HubAssetPort`` path (``prefer_cache`` unchanged) and does not
need ``source_id``. Explicit ``hub_port`` / ``downloader`` also force the
legacy path (tests / compat).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlparse

from jiuwenswarm.common.utils import get_workspace_dir
from jiuwenswarm.extensions.sdk.connector_source import (
    ConnectorCandidate,
    ConnectorRef,
    ConnectorSearchRequest,
    ConnectorSourceProvider,
    DownloadPolicy,
    ProviderInvocationContext,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_installer import (
    _assert_no_symlinks,
    _find_package_root,
    install_hub_asset_package,
)
from jiuwenswarm.server.runtime.marketplace.hub_client import HubNotFoundError
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import (
    HubPackageDownloader,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetPort,
    HubAssetQuery,
    HubSearchRequest,
    create_default_hub_asset_port,
    hub_asset_matches_visible_query,
)
from jiuwenswarm.server.runtime.marketplace.hub_install_state import (
    HubInstallRecord,  # noqa: F401 - compatibility re-export
    HubInstallStateStore,
    normalize_install_source_id,
)
from jiuwenswarm.server.runtime.mcp import registry
from jiuwenswarm.server.runtime.mcp.package_manifest import load_mcp_package

logger = logging.getLogger(__name__)


def _mcp_root() -> Path:
    return get_workspace_dir() / "mcp"


def _builtin_root() -> Path:
    return _mcp_root() / "mcp_builtins"


def _hub_root() -> Path:
    return _mcp_root() / "mcp_hub"


def _state() -> HubInstallStateStore:
    return HubInstallStateStore(_mcp_root())


def _remote_icon(icon_uri: Any) -> str | None:
    """Cache a Hub thumbnail data URL; public http(s) URLs pass through."""
    value = str(icon_uri or "").strip()
    if not value:
        return None
    if value.lower().startswith(("http://", "https://")):
        return value
    from jiuwenswarm.server.runtime.package_avatars import cached_package_avatar_url

    return cached_package_avatar_url(
        value,
        cache_key="hub:" + hashlib.sha256(value.encode("utf-8")).hexdigest(),
    ) or None


def _is_enterprise_edition() -> bool:
    try:
        from jiuwenswarm.enterprise.edition import is_enterprise

        return bool(is_enterprise())
    except Exception:  # noqa: BLE001
        return False


def _provider_context(
    label: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    user_id: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> ProviderInvocationContext:
    identity: dict[str, Any] = {}
    meta = metadata if isinstance(metadata, Mapping) else {}
    if meta:
        try:
            from jiuwenswarm.common.request_identity import web_routing_identity

            identity = dict(web_routing_identity(meta))
        except Exception:  # noqa: BLE001
            identity = {}

    def _pick(key: str, explicit: str | None) -> str | None:
        if explicit is not None and str(explicit).strip():
            return str(explicit).strip()
        value = str(identity.get(key) or "").strip()
        return value or None

    trace_id = str(meta.get("trace_id") or "").strip() or None

    return ProviderInvocationContext(
        request_id=label,
        trace_id=trace_id,
        user_id=_pick("user_id", user_id),
        group_id=_pick("group_id", group_id),
        bot_id=_pick("bot_id", bot_id),
    )


def _connector_candidate_matches_query(item: ConnectorCandidate, query: str) -> bool:
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


def _ensure_process_connector_defaults() -> None:
    """Ensure the process ConnectorSourceManager has extension default sources.

    ``custom_config.connector_sources`` from effective policy is not applied
    yet; only extension ``default_source_config()`` entries are registered.
    """
    from jiuwenswarm.server.runtime.connector import get_connector_source_manager

    manager = get_connector_source_manager()
    if not manager.list_providers():
        manager.register_default_sources()


async def _get_connector_provider(
    source_id: str,
    capability: str,
) -> ConnectorSourceProvider | None:
    """Return a started Provider when ConnectorSourceManager has the source bound."""
    from jiuwenswarm.server.runtime.connector import get_connector_source_manager
    from jiuwenswarm.server.runtime.connector.registry import (
        ConnectorSourceRegistryError,
    )

    _ensure_process_connector_defaults()
    try:
        return await get_connector_source_manager().registry.get(source_id, capability)
    except ConnectorSourceRegistryError:
        return None


@asynccontextmanager
async def enterprise_connector_source_scope(
    *,
    request: Any | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AsyncIterator[None]:
    """Marketplace SPI scope: process defaults only (no policy connector_sources).

    ``request`` / ``metadata`` are accepted for call-site compatibility; they are
    not used to resolve or apply effective-policy ``connector_sources``.
    """
    del request, metadata
    _ensure_process_connector_defaults()
    yield


def _catalogue_version_id(item: Any) -> str:
    """Resolve installable version from SPI candidate or Hub summary."""
    for attr in ("version_id", "public_latest_version"):
        value = str(getattr(item, attr, None) or "").strip()
        if value:
            return value
    return ""


def _remote_card(item: Any, *, source_id: str | None = None) -> dict[str, Any]:
    """Build a marketplace remote card.

    ``source_id`` is only set for SPI / multi-source callers; the personal Hub
    path omits it to match pre-SPI card shape.
    """
    version_id = _catalogue_version_id(item)
    card = {
        "id": item.asset_id,
        "name": item.package_name,
        "package_name": item.package_name,
        "display_name": item.display_name or item.package_name,
        "description": item.short_description,
        "category": item.category_name,
        "integration_type": "",
        "connection_state": "disconnected",
        "has_bundled_skills": False,
        "icon": _remote_icon(item.icon_uri),
        "source": "hub",
        "installed": False,
        "version": version_id,
        "version_id": version_id,
        "tags": list(item.tags),
    }
    versions = getattr(item, "versions", ()) or ()
    if versions:
        card["versions"] = [
            dict(entry) for entry in versions if isinstance(entry, Mapping)
        ]
    all_versions = getattr(item, "all_versions", ()) or ()
    if all_versions and "versions" not in card:
        card["versions"] = [{"version_id": str(v)} for v in all_versions if str(v).strip()]
    if source_id:
        card["source_id"] = source_id
    return card


def _host_matches_allow_rule(host: str, rule: str) -> bool:
    """Match a download hostname against one allow-list rule."""
    host_value = str(host or "").strip().lower()
    rule_value = str(rule or "").strip().lower()
    if not host_value or not rule_value:
        return False
    if rule_value.startswith("."):
        suffix = rule_value[1:]
        return bool(suffix) and (
            host_value == suffix or host_value.endswith(rule_value)
        )
    host_parts = host_value.split(".")
    rule_parts = rule_value.split(".")
    if len(host_parts) != len(rule_parts):
        return False
    for host_part, rule_part in zip(host_parts, rule_parts):
        if rule_part == "*":
            continue
        if host_part != rule_part:
            return False
    return True


def _assert_connector_download_url_allowed(
    download_url: str,
    *,
    source_id: str,
    download_policy: DownloadPolicy | None,
) -> None:
    """Platform install-path host allow-list (SPI default download no longer checks)."""
    allowed_hosts = tuple(download_policy.allowed_hosts) if download_policy else ()
    if not allowed_hosts:
        raise ValueError(
            f"connector source {source_id} 未配置 download_policy.allowed_hosts，拒绝下载"
        )
    parsed = urlparse(str(download_url or "").strip())
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("connector download URL must be an absolute HTTP(S) URL")
    if any(_host_matches_allow_rule(host, rule) for rule in allowed_hosts):
        return
    raise ValueError(
        f"connector download host {host!r} is not allowed for source {source_id}"
    )


def _build_remote_cards_with_install_state(
    remote_items: list[Any],
    *,
    source_id: str,
) -> list[dict[str, Any]]:
    """Enterprise source-tab cards: remote catalogue only, install badge from ledger.

    Aligns with ``skills.source.search``: no built_in / other-source local merge.
    ``source_id`` must already be resolved by the caller (no default backfill here).
    """
    ledger = {}
    for record in _state().all().values():
        if record.kind != "mcp":
            continue
        if normalize_install_source_id(record.source_id) != source_id:
            continue
        ledger[record.asset_id] = record
    by_package = {record.package_id: record for record in ledger.values()}
    cards: list[dict[str, Any]] = []
    for item in remote_items:
        if not item.package_name:
            continue
        card = _remote_card(item, source_id=source_id)
        # Keep catalogue ``version`` (public_latest); local ledger goes to installed_version.
        remote_version = str(card.get("version") or "").strip()
        record = ledger.get(item.asset_id) or by_package.get(item.package_name)
        if record is not None:
            installed_version = str(record.version or "").strip()
            card["installed"] = True
            card["id"] = record.asset_id
            card["package_name"] = record.package_id
            card["name"] = record.package_id
            if installed_version:
                card["installed_version"] = installed_version
            card["has_update"] = bool(
                installed_version
                and remote_version
                and installed_version != remote_version
            )
            if (_hub_root() / record.package_id).is_dir():
                detail = registry.get_mcp(record.package_id)
                if detail is not None:
                    for key in (
                        "connection_state",
                        "integration_type",
                        "has_bundled_skills",
                        "category",
                        "display_name",
                        "description",
                        "icon",
                    ):
                        if key in detail and detail[key] not in (None, ""):
                            card[key] = detail[key]
        cards.append(card)
    return cards


def _build_enterprise_local_mcp_marketplace_cards() -> list[dict[str, Any]]:
    """Enterprise mine list: connected presets + all custom MCPs."""
    cards: list[dict[str, Any]] = []
    for summary in registry.list_marketplace_mcps("local"):
        name = str(summary.get("name") or "")
        record = _state().get_by_package_id(name)
        if record is not None and record.kind == "mcp" and summary.get("source") == "hub":
            cards.append(
                {
                    **summary,
                    "id": record.asset_id,
                    "package_name": name,
                    "source": "hub",
                    "installed": True,
                    "version": record.version,
                }
            )
        else:
            cards.append({**summary, "id": name, "package_name": name, "installed": True})
    return cards


async def _all_remote(port: HubAssetPort, query: str = "") -> list[Any]:
    items: list[Any] = []
    seen_asset_ids: set[str] = set()
    for page in range(1, 101):
        result = await port.search_assets(
            HubSearchRequest(kind="mcp", query=query, page=page, page_size=100)
        )
        new_items: list[Any] = []
        for item in result.items:
            if item.kind != "mcp" or item.asset_id in seen_asset_ids:
                continue
            seen_asset_ids.add(item.asset_id)
            new_items.append(item)
        if result.items and not new_items:
            raise ValueError(f"Hub MCP pagination page {page} made no progress")
        items.extend(new_items)
        if len(items) >= result.total or not result.items:
            return items
    raise ValueError("Hub MCP pagination exceeded 100 pages")


async def _fetch_all_candidates_from_provider(
    provider: ConnectorSourceProvider,
    *,
    source_id: str,
    query: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> list[ConnectorCandidate]:
    items: list[ConnectorCandidate] = []
    seen_asset_ids: set[str] = set()
    context = _provider_context(f"mcp-list:{source_id}", metadata=metadata)
    for page in range(1, 101):
        result = await provider.search(
            ConnectorSearchRequest(q=query, page=page, page_size=100),
            context,
        )
        new_items: list[ConnectorCandidate] = []
        for item in result.items:
            if item.asset_id in seen_asset_ids:
                continue
            if item.source_id and item.source_id != source_id:
                continue
            seen_asset_ids.add(item.asset_id)
            new_items.append(item)
        if result.items and not new_items:
            raise ValueError(
                f"Connector source {source_id} pagination page {page} made no progress"
            )
        items.extend(new_items)
        total = result.total
        if total is not None and len(items) >= total:
            return items
        if not result.items or result.next_page is None:
            return items
    raise ValueError(f"Connector source {source_id} pagination exceeded 100 pages")


def installed_mcp_descriptions() -> dict[str, dict[str, str]]:
    """Localized display metadata of installed MCP packages, by server name.

    Reads the installed package manifests (builtins + hub) — the same
    ``基本信息`` the web catalogue shows, available locally without a hub
    round-trip. Keyed by both the manifest's MCP server name (the
    ``mcp_{server}_`` tool-prefix segment) and the package id; values carry
    ``zh``/``en`` display descriptions (empty when absent).
    """

    out: dict[str, dict[str, str]] = {}
    for root in (_builtin_root(), _hub_root()):
        if not root.is_dir():
            continue
        for package_dir in sorted(root.iterdir()):
            if not package_dir.is_dir() or not (package_dir / "manifest.json").is_file():
                continue
            try:
                manifest = load_mcp_package(package_dir)
            except Exception:
                logger.debug(
                    "mcp manifest load failed: %s", package_dir, exc_info=True
                )
                continue
            raw = manifest.raw
            display_description = raw.get("display_description")
            if not isinstance(display_description, dict):
                display_description = {}
            payload = {
                "zh": str(display_description.get("zh") or "").strip(),
                "en": str(display_description.get("en") or "").strip(),
            }
            for key in (manifest.server_name, manifest.package_id):
                if key:
                    out.setdefault(str(key), payload)
    return out


async def list_connector_source_providers(
    *,
    request: Any | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return registered connector catalogue sources (D-08 enterprise UI)."""
    from jiuwenswarm.server.runtime.connector import get_connector_source_manager

    async with enterprise_connector_source_scope(
        request=request,
        metadata=metadata,
    ):
        return get_connector_source_manager().list_providers()


async def fetch_connector_source_candidates(
    source_id: str,
    *,
    query: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> list[ConnectorCandidate] | None:
    """Fetch one Connector Source SPI catalogue (no local merge, no prefer_cache).

    ``source_id`` must already be resolved by the caller (no default backfill here).
    Returns ``None`` when the source is not bound.
    """
    provider = await _get_connector_provider(source_id, "search")
    if provider is None:
        return None
    items = await _fetch_all_candidates_from_provider(
        provider,
        source_id=source_id,
        query=str(query or "").strip(),
        metadata=metadata,
    )
    normalized_query = str(query or "").strip()
    if normalized_query:
        items = [
            item
            for item in items
            if _connector_candidate_matches_query(item, normalized_query)
        ]
    return list(items)


async def list_mcps_from_enterprise(
    mcp_filter: str = "hub",
    *,
    source_id: str | None = None,
    query: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Enterprise marketplace list (edition-specific entry used by dispatcher).

    - ``local``: mine list; ``source_id`` not required.
    - ``hub``: remote-only SPI catalogue for one source; ``source_id`` required.
    - other / empty: ``filter must be hub or local``.
    """
    normalized_filter = str(mcp_filter or "").strip().lower()
    if normalized_filter == "local":
        return _build_enterprise_local_mcp_marketplace_cards()
    if normalized_filter != "hub":
        raise ValueError("filter must be hub or local")

    resolved_source_id = str(source_id or "").strip()
    if not resolved_source_id:
        raise ValueError("source_id is required")

    normalized_query = str(query or "").strip()
    try:
        remote_items = await fetch_connector_source_candidates(
            resolved_source_id,
            query=normalized_query,
            metadata=metadata,
        )
    except Exception:
        # Do not mask catalogue failures as an empty success — mcp.list handlers
        # surface raised errors as ok=False for the client.
        logger.exception(
            "connector catalogue fetch failed: source_id=%s",
            resolved_source_id,
        )
        raise
    if remote_items is None:
        raise ValueError(
            f"connector source is not registered: {resolved_source_id}"
        )
    return _build_remote_cards_with_install_state(
        remote_items,
        source_id=resolved_source_id,
    )


async def list_mcps_with_hub(
    mcp_filter: str | None = None,
    *,
    hub_port: HubAssetPort | None = None,
    cache_mode: str | None = None,
    refresh: bool = False,
    query: str = "",
    source_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Dispatch marketplace list: enterprise vs personal HubAssetPort.

    - Enterprise: :func:`list_mcps_from_enterprise` (``hub``|``local``;
      catalogue requires ``source_id``; omitted filter is rejected).
    - Personal / injected ``hub_port``: legacy HubAssetPort path inline
      (``builtin`` | ``local``; omitted filter defaults to ``builtin`` here only).
    """
    if _is_enterprise_edition():
        async with enterprise_connector_source_scope(metadata=metadata):
            return await list_mcps_from_enterprise(
                str(mcp_filter or "").strip().lower(),
                source_id=source_id,
                query=query,
                metadata=metadata,
            )

    personal_filter = str(mcp_filter or "builtin").strip().lower() or "builtin"
    local = await asyncio.to_thread(
        registry.list_marketplace_mcps,
        "local" if personal_filter == "local" else "builtin",
    )
    cards: dict[str, dict[str, Any]] = {}
    installed_asset_ids: set[str] = set()
    for card in local:
        name = str(card.get("name") or "")
        record = _state().get_by_package_id(name)
        if record is not None and record.kind == "mcp":
            installed_asset_ids.add(record.asset_id)
            if card.get("source") == "hub":
                card = {
                    **card,
                    "id": record.asset_id,
                    "package_name": name,
                    "source": "hub",
                    "installed": True,
                    "version": record.version,
                }
            else:
                card = {**card, "id": name, "package_name": name, "installed": True}
        else:
            card = {**card, "id": name, "package_name": name, "installed": True}
        cards[name] = card
    if personal_filter == "local":
        return list(cards.values())
    normalized_query = str(query or "").strip()
    if normalized_query:
        query_terms = normalized_query.casefold().split()
        filtered_cards: dict[str, dict[str, Any]] = {}
        for name, card in cards.items():
            searchable_text = " ".join(
                (
                    str(card.get("name", "")),
                    str(card.get("display_name", "")),
                    str(card.get("description", "")),
                    str(card.get("category", "")),
                    " ".join(card.get("tags") or []),
                )
            ).casefold()
            matches_query = True
            for term in query_terms:
                if term not in searchable_text:
                    matches_query = False
                    break
            if matches_query:
                filtered_cards[name] = card
        cards = filtered_cards
    port = hub_port or create_default_hub_asset_port()
    cache_state = None
    if cache_mode == "prefer_cache" and not normalized_query:
        from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import cached_asset_catalog
        remote_items, cache_state = await cached_asset_catalog(
            port, "mcp", refresh=refresh
        )
    else:
        try:
            remote_items = await _all_remote(port, normalized_query)
        except Exception:
            return list(cards.values())
    if normalized_query:
        remote_items = [
            item
            for item in remote_items
            if hub_asset_matches_visible_query(item, normalized_query)
        ]
    pending: list[Any] = []
    for item in remote_items:
        if not item.package_name:
            continue
        if item.asset_id in installed_asset_ids or item.package_name in cards:
            continue
        pending.append(item)
    if pending:
        # Thumbnail caching writes files; keep it off the event loop.
        remote_cards = await asyncio.to_thread(
            lambda: [_remote_card(item) for item in pending]
        )
        for card in remote_cards:
            cards.setdefault(card["package_name"], card)
    # Use the same marketplace sort as registry.list_marketplace_mcps.
    priority = {"huaweiyun-mcp": 0, "harmonyos-mcp": 0}
    sorted_cards = sorted(
        cards.values(),
        key=lambda card: (
            priority.get(str(card.get("name") or ""), 1),
            str(card.get("name") or ""),
        ),
    )
    if cache_state is not None:
        from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import CatalogCards
        return CatalogCards(sorted_cards, cache_state)
    return sorted_cards


async def _resolve_hub_asset_id_by_package_name(
    port: HubAssetPort, package_name: str
) -> str:
    """Resolve a Hub asset id from an exact package name ("" when absent).

    ``mcp.show``/``mcp.connect`` use the runtime package name while Hub
    addresses assets by opaque id; only an exact name match resolves.
    """
    result = await port.search_assets(
        HubSearchRequest(kind="mcp", query=package_name, page=1, page_size=100)
    )
    for item in result.items:
        if item.kind == "mcp" and item.package_name == package_name:
            return item.asset_id
    return ""


async def _resolve_provider_asset_id_by_package_name(
    provider: ConnectorSourceProvider,
    *,
    source_id: str,
    package_name: str,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    result = await provider.search(
        ConnectorSearchRequest(q=package_name, page=1, page_size=100),
        _provider_context(f"mcp-resolve:{source_id}", metadata=metadata),
    )
    for item in result.items:
        if item.package_name == package_name and (
            not item.source_id or item.source_id == source_id
        ):
            return item.asset_id
    return ""


async def show_mcp_from_enterprise(
    identifier: str,
    *,
    source_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Enterprise SPI remote detail for one MCP.

    Returns ``None`` when the connector source is not bound for ``get_detail``.
    ``source_id`` must already be normalized by the caller.
    """
    provider = await _get_connector_provider(source_id, "get_detail")
    if provider is None:
        return None

    target = str(identifier or "").strip()
    context = _provider_context(f"mcp-show:{source_id}", metadata=metadata)
    asset_id = target
    resolved_by_name = False
    # Empty version_id: Provider resolves the Hub public/latest version.
    version_id = ""
    try:
        detail = await provider.get_detail(
            ConnectorRef(source_id=source_id, asset_id=asset_id),
            version_id,
            context,
        )
    except Exception:
        asset_id = await _resolve_provider_asset_id_by_package_name(
            provider,
            source_id=source_id,
            package_name=target,
            metadata=metadata,
        )
        if not asset_id:
            raise
        resolved_by_name = True
        detail = await provider.get_detail(
            ConnectorRef(source_id=source_id, asset_id=asset_id),
            version_id,
            context,
        )
    if detail.asset_id != target and not (
        resolved_by_name and detail.package_name == target
    ):
        raise ValueError("Hub MCP asset identity mismatch")
    card: dict[str, Any] = {
        "id": detail.asset_id,
        "name": detail.package_name,
        "package_name": detail.package_name,
        "display_name": detail.display_name or detail.package_name,
        "description": detail.detail_description or detail.short_description,
        "category": detail.category_name,
        "integration_type": "",
        "connection_state": "disconnected",
        "has_bundled_skills": False,
        "icon": detail.icon_uri or None,
        "source": "hub",
        "source_id": source_id,
        "installed": False,
        "version": str(detail.version_id or "").strip(),
        "version_id": str(detail.version_id or "").strip(),
        "tags": list(detail.tags),
        "mcp_spec": None,
        "cli_spec_present": False,
        "bundled_skills": [],
        "skills": [],
        "tools": [],
    }
    versions = detail.metadata.get("versions") if isinstance(detail.metadata, Mapping) else None
    if isinstance(versions, (list, tuple)):
        card["versions"] = [
            dict(entry) for entry in versions if isinstance(entry, Mapping)
        ]
    return card


async def show_mcp_with_hub(
    identifier: str,
    *,
    hub_port: HubAssetPort | None = None,
    source_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    target = str(identifier or "").strip()
    local = registry.get_mcp(target)
    if local is not None:
        record = _state().get(target, source_id=source_id) or _state().get_by_package_id(
            target
        )
        if record is not None and local.get("source") == "hub":
            return {
                **local,
                "id": record.asset_id,
                "package_name": record.package_id,
                "source": "hub",
                "source_id": normalize_install_source_id(record.source_id),
                "installed": True,
                "version": record.version,
            }
        return {**local, "id": target, "package_name": target, "installed": True}

    record = _state().get(target, source_id=source_id) or _state().get_by_package_id(
        target
    )
    if record is not None and (_hub_root() / record.package_id).is_dir():
        detail = registry.get_mcp(record.package_id)
        if detail is not None:
            return {
                **detail,
                "id": record.asset_id,
                "package_name": record.package_id,
                "source": "hub",
                "source_id": normalize_install_source_id(record.source_id),
                "installed": True,
                "version": record.version,
            }

    if _is_enterprise_edition():
        resolved_source_id = str(source_id or "").strip()
        if not resolved_source_id:
            raise ValueError("source_id is required")
        async with enterprise_connector_source_scope(metadata=metadata):
            enterprise_detail = await show_mcp_from_enterprise(
                target,
                source_id=resolved_source_id,
                metadata=metadata,
            )
        if enterprise_detail is None:
            raise ValueError(
                f"connector source is not registered: {resolved_source_id}"
            )
        return enterprise_detail

    port = hub_port or create_default_hub_asset_port()
    resolved_by_name = False
    try:
        detail = await port.query_asset(HubAssetQuery(kind="mcp", asset_id=target))
    except HubNotFoundError:
        # ``target`` may be a runtime package name (amap), not the asset id.
        asset_id = await _resolve_hub_asset_id_by_package_name(port, target)
        if not asset_id:
            raise
        resolved_by_name = True
        detail = await port.query_asset(HubAssetQuery(kind="mcp", asset_id=asset_id))
    if detail.kind != "mcp" or (
        detail.asset_id != target
        and not (resolved_by_name and detail.package_name == target)
    ):
        raise ValueError("Hub MCP asset identity mismatch")
    return {
        "id": detail.asset_id,
        "name": detail.package_name,
        "package_name": detail.package_name,
        "display_name": detail.display_name or detail.package_name,
        "description": detail.detail_description or detail.short_description,
        "category": "",
        "integration_type": "",
        "connection_state": "disconnected",
        "has_bundled_skills": False,
        "icon": await asyncio.to_thread(_remote_icon, detail.icon_uri),
        "source": "hub",
        "installed": False,
        "version": detail.version,
        "tags": list(detail.tags),
        "mcp_spec": None,
        "cli_spec_present": False,
        "bundled_skills": [],
        "skills": [],
        "tools": [],
    }


def _validate_hub_package_name(value: str) -> str:
    package_id = str(value or "").strip()
    if not package_id or package_id in {".", ".."}:
        raise ValueError("Hub MCP package name is invalid")
    if package_id.startswith(".") or "/" in package_id or "\\" in package_id:
        raise ValueError("Hub MCP package name is invalid")
    if Path(package_id).is_absolute() or PureWindowsPath(package_id).is_absolute():
        raise ValueError("Hub MCP package name is invalid")
    return package_id


def _precheck_mcp_package_conflicts(
    package_id: str,
    *,
    asset_id: str,
    source_id: str,
) -> None:
    if (_builtin_root() / package_id).exists():
        raise ValueError(f"Hub MCP package conflicts with built-in: {package_id}")
    _state().assert_package_id_available(
        package_id,
        asset_id=asset_id,
        source_id=source_id,
        kind="mcp",
    )
    if (_hub_root() / package_id).exists():
        raise ValueError(f"Hub mcp destination already exists: {package_id}")


async def _install_via_provider(
    provider: ConnectorSourceProvider,
    *,
    asset_id: str,
    source_id: str,
    version_id: str,
    package_name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Install one MCP via Connector Source SPI without calling ``get_detail``.

    Caller supplies ``version_id`` (required) and optional ``package_name`` for
    early conflict checks—aligned with Skill install (caller params +
    ``get_artifact``). Final package id is confirmed from the ZIP manifest.
    """
    from jiuwenswarm.server.runtime.connector import get_connector_source_manager

    version = str(version_id or "").strip()
    if not version:
        raise ValueError("version_id is required")

    hinted_package = str(package_name or "").strip()
    package_id_hint = (
        _validate_hub_package_name(hinted_package) if hinted_package else ""
    )
    if package_id_hint:
        _precheck_mcp_package_conflicts(
            package_id_hint, asset_id=asset_id, source_id=source_id
        )

    context = _provider_context(
        f"mcp-install:{source_id}:{asset_id}", metadata=metadata
    )
    ref = ConnectorRef(source_id=source_id, asset_id=asset_id)
    descriptor = await provider.get_artifact(ref, version, context)
    desc_package = str(descriptor.package_name or "").strip()
    if package_id_hint and desc_package and desc_package != package_id_hint:
        raise ValueError(
            f"package_name mismatch: expected {package_id_hint!r}, "
            f"got {desc_package!r}"
        )
    if not package_id_hint and desc_package:
        package_id_hint = _validate_hub_package_name(desc_package)
        _precheck_mcp_package_conflicts(
            package_id_hint, asset_id=asset_id, source_id=source_id
        )

    download_policy = None
    try:
        download_policy = get_connector_source_manager().registry.get_config(source_id).download_policy
    except Exception:  # noqa: BLE001
        download_policy = None
    _assert_connector_download_url_allowed(
        descriptor.download_url,
        source_id=source_id,
        download_policy=download_policy,
    )
    body = await provider.download_artifact(descriptor, download_policy)
    await provider.verify_artifact(descriptor, body)

    destination_root = _hub_root()
    destination_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm_connector_mcp_") as temp:
        extracted = Path(temp)
        HubPackageDownloader.extract_zip(body, extracted)
        package_root = _find_package_root(extracted, "mcp")
        _assert_no_symlinks(package_root)
        # Hub ZIPs are often flat (manifest.json at archive root). Extract lands in a
        # random temp dir, so dirname≠manifest.id until we stage under package_id—
        # same order as install_hub_asset_package (personal path).
        if package_id_hint:
            package_id = package_id_hint
        else:
            raw_manifest = json.loads(
                (package_root / "manifest.json").read_text(encoding="utf-8")
            )
            package_id = _validate_hub_package_name(
                str(raw_manifest.get("id") or "").strip()
            )
            _precheck_mcp_package_conflicts(
                package_id, asset_id=asset_id, source_id=source_id
            )
        destination = destination_root / package_id
        staging_parent = Path(
            tempfile.mkdtemp(prefix=f".{package_id}.staging-", dir=destination_root)
        )
        staged = staging_parent / package_id
        try:
            shutil.copytree(package_root, staged)
            load_mcp_package(staged, expected_id=package_id)
            staged.replace(destination)
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)

    checksum = str(descriptor.content_sha256 or "").strip()
    record = HubInstallRecord(
        asset_id=asset_id,
        package_id=package_id,
        kind="mcp",
        version=str(descriptor.version_id or version),
        checksum_sha256=checksum,
        installed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source_id=normalize_install_source_id(source_id),
    )
    try:
        _state().upsert(record)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        try:
            _state().remove(asset_id, source_id=source_id)
        except Exception:
            logger.exception("Failed to remove Hub install state during rollback")
        raise
    return {
        "id": asset_id,
        "name": package_id,
        "installed": True,
        "source": "hub",
        "source_id": source_id,
    }


async def install_hub_mcp(
    asset_id: str,
    *,
    source_id: str | None = None,
    version_id: str | None = None,
    package_name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    hub_port: HubAssetPort | None = None,
    downloader: HubPackageDownloader | None = None,
) -> dict[str, Any]:
    asset = str(asset_id or "").strip()
    if not asset:
        raise ValueError("Hub MCP asset id is required")
    existing = _state().get(asset, source_id=source_id)
    if existing is not None and (_hub_root() / existing.package_id).is_dir():
        return {
            "id": asset,
            "name": existing.package_id,
            "installed": True,
            "source": "hub",
            "source_id": normalize_install_source_id(existing.source_id),
        }

    if downloader is None and _is_enterprise_edition():
        resolved_source_id = str(source_id or "").strip()
        if not resolved_source_id:
            raise ValueError("source_id is required")
        resolved_version = str(version_id or "").strip()
        if not resolved_version:
            raise ValueError("version_id is required")
        async with enterprise_connector_source_scope(metadata=metadata):
            provider = await _get_connector_provider(
                resolved_source_id, "get_artifact"
            )
            if provider is None:
                raise ValueError(
                    f"connector source is not registered: {resolved_source_id}"
                )
            return await _install_via_provider(
                provider,
                asset_id=asset,
                source_id=resolved_source_id,
                version_id=resolved_version,
                package_name=package_name,
                metadata=metadata,
            )

    def reject_builtin_conflict(package_id: str) -> None:
        if (_builtin_root() / package_id).exists():
            raise ValueError(f"Hub MCP package conflicts with built-in: {package_id}")

    def validate_package(package_root: Path, expected_package_id: str) -> None:
        load_mcp_package(package_root, expected_id=expected_package_id)

    result = await install_hub_asset_package(
        kind="mcp",
        asset_id=asset,
        destination_root=_hub_root(),
        state_store=_state(),
        package_name_validator=_validate_hub_package_name,
        package_validator=validate_package,
        conflict_validator=reject_builtin_conflict,
        hub_port=hub_port,
        downloader=downloader,
        source_id=source_id,
    )
    return {
        "id": asset,
        "name": result.package_id,
        "installed": True,
        "source": "hub",
        "source_id": normalize_install_source_id(result.record.source_id),
    }


# Design alias (I-04).
install_connector_from_source = install_hub_mcp


def _hub_mcp_record(identifier: str) -> HubInstallRecord | None:
    state = _state()
    record = state.get(identifier)
    if record is not None and record.kind == "mcp":
        return record
    record = state.get_by_package_id(identifier)
    if record is not None and record.kind == "mcp":
        return record
    return None


def _remove_hub_mcp_package(package_id: str) -> None:
    try:
        registry.disconnect_mcp(package_id)
    except (KeyError, ValueError):
        pass
    shutil.rmtree(_mcp_root() / "skills" / package_id, ignore_errors=True)
    (_mcp_root() / "credentials" / f"{package_id}.json").unlink(missing_ok=True)
    shutil.rmtree(_hub_root() / package_id, ignore_errors=True)


def uninstall_hub_mcp(identifier: str) -> dict[str, Any]:
    target = str(identifier or "").strip()
    if not target:
        raise ValueError("Hub MCP asset id is required")
    record = _hub_mcp_record(target)
    if record is not None:
        _remove_hub_mcp_package(record.package_id)
        _state().remove(record.asset_id, source_id=record.source_id)
        return {"id": record.asset_id, "name": record.package_id, "removed": True}
    try:
        package_id = _validate_hub_package_name(target)
    except ValueError:
        package_id = ""
    if package_id and (_hub_root() / package_id).is_dir():
        _remove_hub_mcp_package(package_id)
        return {"id": package_id, "name": package_id, "removed": True}
    return {"id": target, "name": target, "removed": False, "already_removed": True}
