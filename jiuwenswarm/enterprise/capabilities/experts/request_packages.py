# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版按本次请求下发的专家包决定能不能看、能装、能用。

个人版先看产品形态并放行，不读专家包，不组装租户平面。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from jiuwenswarm.common.schema.expert_packages import (
    HIDDEN_HUB_ASSET_IDS,
    METADATA_KEY,
    canonical_kind,
    ensure_packages_fit,
    normalize_packages,
)

logger = logging.getLogger(__name__)

# 本次请求的 packages。None 表示还没有绑定。
_REQUEST_PACKAGES: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "expert_packages", default=None
)

_KIND_DIRS = {
    "agent_template": "agent_templates",
    "agent_group": "agent_groups",
}


def hidden_from_enterprise(package_id: str) -> bool:
    """企业版不展示这四个 Hub 包。个人版直接返回假。"""
    from jiuwenswarm.enterprise.edition import is_enterprise

    if not is_enterprise():
        return False
    return str(package_id or "").strip() in HIDDEN_HUB_ASSET_IDS


def _packages_from_metadata(metadata: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """结构非法或超体积时，整份按空名单。"""
    if not isinstance(metadata, Mapping) or METADATA_KEY not in metadata:
        return []
    try:
        packages = normalize_packages(metadata.get(METADATA_KEY))
        ensure_packages_fit(packages)
    except ValueError:
        logger.warning("[expert_packages] request packages rejected, treating as empty")
        return []
    return packages


@contextmanager
def bind_request_subject(metadata: Mapping[str, Any] | None) -> Iterator[None]:
    """把本次请求的专家包绑定到允许判断。不写入业务 params。"""
    from jiuwenswarm.enterprise.edition import is_enterprise

    if not is_enterprise():
        token = _REQUEST_PACKAGES.set([])
        try:
            yield
        finally:
            _REQUEST_PACKAGES.reset(token)
        return
    token = _REQUEST_PACKAGES.set(
        _packages_from_metadata(metadata if isinstance(metadata, Mapping) else None)
    )
    try:
        yield
    finally:
        _REQUEST_PACKAGES.reset(token)


def _request_packages() -> list[dict[str, Any]]:
    bound = _REQUEST_PACKAGES.get()
    if not bound:
        return []
    return bound


def allows(kind: str, package_id: str) -> bool:
    """个人版放行。企业版放行用户自己的包，以及本次请求下发的专家包。"""
    from jiuwenswarm.enterprise.edition import is_enterprise

    if not is_enterprise():
        return True
    canonical = canonical_kind(kind)
    wanted = str(package_id or "").strip()
    if canonical is None or not wanted:
        return False
    if _is_user_owned(canonical, wanted):
        return True
    candidates = _candidate_ids(canonical, wanted)
    for item in _request_packages():
        item_kind = canonical_kind(str(item.get("kind") or ""))
        package_id_value = str(item.get("id") or "").strip()
        if item_kind == canonical and package_id_value in candidates:
            return True
    return False


def require_allowed(kind: str, package_id: str) -> None:
    if allows(kind, package_id):
        return
    label = canonical_kind(kind) or str(kind or "").strip()
    raise ValueError(f"package not allowed: {label} {str(package_id or '').strip()}")


def filter_cards(kind: str, cards: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for card in cards:
        card_id = str(card.get("id") or "").strip()
        if card_id and allows(kind, card_id):
            kept.append(card)
    return kept


def _kind_dir(kind: str) -> str:
    return _KIND_DIRS[kind]


def _hub_record(kind: str, package_id: str):
    from jiuwenswarm.server.runtime.extension_package_manager import (
        AgentGroupPackageError,
        hub_install_state_store,
    )

    try:
        store = hub_install_state_store(_kind_dir(kind))
    except AgentGroupPackageError:
        return None
    return store.get(package_id) or store.get_by_package_id(package_id)


def _candidate_ids(kind: str, package_id: str) -> set[str]:
    ids = {package_id}
    record = _hub_record(kind, package_id)
    if record is not None:
        asset_id = str(getattr(record, "asset_id", "") or "").strip()
        runtime_id = str(getattr(record, "package_id", "") or "").strip()
        if asset_id:
            ids.add(asset_id)
        if runtime_id:
            ids.add(runtime_id)
    return ids


def _marketplace_source(kind: str, *package_ids: str) -> str | None:
    from jiuwenswarm.server.runtime.extension_package_manager import (
        read_agent_group_marketplace_entries,
        read_agent_template_marketplace_entries,
    )

    if kind == "agent_template":
        entries = read_agent_template_marketplace_entries()
    else:
        entries = read_agent_group_marketplace_entries()
    wanted = {item for item in package_ids if item}
    for entry in entries:
        if entry.get("id") not in wanted:
            continue
        source = entry.get("source")
        if isinstance(source, str) and source.strip():
            return source.strip()
    return None


def _is_user_owned(kind: str, package_id: str) -> bool:
    """用户 local 根下、且不是 Hub 安装的包。

    企业版专家团没有绑定租户时，安装目录不可读，当作不是用户自己的包。
    """
    from jiuwenswarm.server.runtime.extension_package_manager import (
        AgentGroupPackageError,
        get_local_root,
    )

    record = _hub_record(kind, package_id)
    if record is not None:
        return False
    if _marketplace_source(kind, package_id) == "hub":
        return False
    try:
        root = get_local_root(_kind_dir(kind)).resolve()
    except AgentGroupPackageError:
        return False
    candidate = (root / package_id).resolve()
    if candidate.parent != root:
        return False
    try:
        return candidate.is_dir()
    except OSError:
        return False
