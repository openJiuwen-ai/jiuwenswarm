# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版按本次请求的允许名单决定专家包能不能看、能装、能用。

个人版先看产品形态并放行，不读名单，不组装租户平面。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from jiuwenswarm.common.schema.package_allowlist import (
    HIDDEN_HUB_ASSET_IDS,
    METADATA_KEY,
    canonical_kind,
    ensure_packages_fit,
    normalize_packages,
)

logger = logging.getLogger(__name__)

# (user_id, group_id)。未绑定表示这次调用没有请求身份。
_SUBJECT: ContextVar[tuple[str, str] | None] = ContextVar(
    "package_allowlist_subject", default=None
)
# 本次请求的 packages。None 表示还没有绑定。
_ALLOWLIST: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "package_allowlist_packages", default=None
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
    """结构非法、超体积或任一 match_expr 不能求值时，整份按空名单。"""
    if not isinstance(metadata, Mapping) or METADATA_KEY not in metadata:
        return []
    try:
        packages = normalize_packages(metadata.get(METADATA_KEY))
        ensure_packages_fit(packages)
        _require_valid_match_exprs(packages)
    except ValueError:
        logger.warning("[package_allowlist] request packages rejected, treating as empty")
        return []
    return packages


def _require_valid_match_exprs(packages: list[dict[str, Any]]) -> None:
    from gateway.config.enterprise.expressions import validate_match_expr

    for item in packages:
        match_expr = item.get("match_expr")
        if match_expr is not None:
            validate_match_expr(match_expr)


@contextmanager
def bind_request_subject(metadata: Mapping[str, Any] | None) -> Iterator[None]:
    """把本次请求的用户和名单绑定到允许名单判断。不写入业务 params。"""
    from jiuwenswarm.enterprise.edition import is_enterprise

    if not is_enterprise():
        subject_token = _SUBJECT.set(("", ""))
        allowlist_token = _ALLOWLIST.set([])
        try:
            yield
        finally:
            _ALLOWLIST.reset(allowlist_token)
            _SUBJECT.reset(subject_token)
        return
    from jiuwenswarm.common.request_identity import web_routing_identity

    identity = web_routing_identity(metadata if isinstance(metadata, Mapping) else None)
    subject_token = _SUBJECT.set((identity.get("user_id", ""), identity.get("group_id", "")))
    allowlist_token = _ALLOWLIST.set(
        _packages_from_metadata(metadata if isinstance(metadata, Mapping) else None)
    )
    try:
        yield
    finally:
        _ALLOWLIST.reset(allowlist_token)
        _SUBJECT.reset(subject_token)


def _current_identity() -> dict[str, str]:
    subject = _SUBJECT.get()
    if not subject:
        return {"user_id": "", "group_id": ""}
    return {"user_id": subject[0], "group_id": subject[1]}


def _request_packages() -> list[dict[str, Any]]:
    bound = _ALLOWLIST.get()
    if not bound:
        return []
    return bound


def _scope_allows(match_expr: Any, identity: dict[str, str]) -> bool:
    """空范围对任何身份放行。限定范围在没有身份或求值失败时拒绝。"""
    if match_expr is None or match_expr == "" or match_expr == []:
        return True
    from gateway.config.enterprise.expressions import matches

    try:
        return matches(match_expr, identity)
    except ValueError:
        logger.warning("[package_allowlist] match_expr rejected")
        return False


def allows(kind: str, package_id: str) -> bool:
    """个人版放行。企业版放行用户自己的包，以及范围命中的受控包。"""
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
    identity = _current_identity()
    for item in _request_packages():
        item_kind = canonical_kind(str(item.get("kind") or ""))
        package_id_value = str(item.get("id") or "").strip()
        if item_kind != canonical or package_id_value not in candidates:
            continue
        if _scope_allows(item.get("match_expr"), identity):
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
        _hub_install_state_store,
    )

    store = _hub_install_state_store(_kind_dir(kind))
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
    """用户 local 根下、且不是 Hub 安装的包。"""
    record = _hub_record(kind, package_id)
    if record is not None:
        return False
    if _marketplace_source(kind, package_id) == "hub":
        return False
    from jiuwenswarm.server.runtime.extension_package_manager import _local_root

    root = _local_root(_kind_dir(kind)).resolve()
    candidate = (root / package_id).resolve()
    if candidate.parent != root:
        return False
    try:
        return candidate.is_dir()
    except OSError:
        return False
