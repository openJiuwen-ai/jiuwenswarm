# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版专家包允许名单。

个人版不读名单。企业版里，用户 local 根下且来源不是 hub 的包始终可用。
Hub、内置和 resources 包必须出现在本次请求带上的名单里。没有名单或名单为空时，这些包不可用。
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

METADATA_KEY = "package_allowlist"
HEADER_NAME = "X-Jiuwenswarm-Package-Allowlist"
MAX_ALLOWLIST_JSON_BYTES = 16 * 1024

# (user_id, group_id)。未绑定表示这次调用没有请求身份。
_SUBJECT: ContextVar[tuple[str, str] | None] = ContextVar(
    "package_allowlist_subject", default=None
)
# 本次请求的 packages。None 表示还没有绑定。
_ALLOWLIST: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "package_allowlist_packages", default=None
)
# Gateway 组 HTTP 头时的名单。None 表示这次发送没有绑定。
_OUTBOUND: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "package_allowlist_outbound", default=None
)

_CANONICAL_KINDS = {
    "agent_template": "agent_template",
    "agent_templates": "agent_template",
    "agent_group": "agent_group",
    "agent_groups": "agent_group",
}
_KIND_DIRS = {
    "agent_template": "agent_templates",
    "agent_group": "agent_groups",
}


def canonical_kind(kind: str) -> str | None:
    return _CANONICAL_KINDS.get(str(kind or "").strip())


def normalize_packages(raw: Any) -> list[dict[str, Any]]:
    """收成 ``[{kind, id, match_expr?}]``。重复的 kind+id 去掉，非法项拒绝。"""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("packages must be a list")
    seen: set[tuple[str, str]] = set()
    packages: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("package allowlist entry must be an object")
        kind = canonical_kind(str(item.get("kind") or ""))
        if kind is None:
            raise ValueError("kind must be agent_template or agent_group")
        package_id = str(item.get("id") or "").strip()
        has_separator = "/" in package_id or "\\" in package_id
        id_ok = (
            bool(package_id)
            and package_id not in {".", ".."}
            and not has_separator
            and len(package_id) <= 128
        )
        if not id_ok:
            raise ValueError("package id is invalid")
        key = (kind, package_id)
        if key in seen:
            continue
        seen.add(key)
        record: dict[str, Any] = {"kind": kind, "id": package_id}
        match_expr = _normalize_match_expr(item.get("match_expr"))
        if match_expr is not None:
            record["match_expr"] = match_expr
        packages.append(record)
    return packages


def _normalize_match_expr(raw: Any) -> Any:
    """空范围保持为空。非空范围必须是可求值的 user_id / group_id 表达式。"""
    if raw is None or raw == "" or raw == []:
        return None
    from gateway.config.enterprise.expressions import validate_match_expr

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        validate_match_expr(text)
        return text
    if isinstance(raw, list):
        items = []
        for item in raw:
            text = str(item or "").strip()
            if text:
                items.append(text)
        if not items:
            return None
        validate_match_expr(items)
        return items
    raise ValueError("match_expr is invalid")


def _packages_json(packages: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        packages,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def ensure_packages_fit(packages: list[dict[str, Any]]) -> None:
    """名单 JSON 超过 16KiB 时拒绝写入。"""
    if len(_packages_json(packages)) > MAX_ALLOWLIST_JSON_BYTES:
        raise ValueError("package allowlist exceeds 16384 bytes")


def encode_package_allowlist_header(packages: list[dict[str, Any]]) -> str:
    """把名单编成内部 HTTP 头。过大或非法时改发空名单。"""
    try:
        payload = normalize_packages(packages)
        raw = _packages_json(payload)
        if len(raw) > MAX_ALLOWLIST_JSON_BYTES:
            raise ValueError("package allowlist exceeds 16384 bytes")
    except ValueError:
        logger.warning("[package_allowlist] allowlist header encode failed, sending empty")
        raw = b"[]"
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_package_allowlist_header(value: str | None) -> list[dict[str, Any]]:
    """还原内部 HTTP 头。缺失或损坏时为空名单。"""
    if value is None or not str(value).strip():
        return []
    encoded = str(value).strip()
    try:
        encoded_bytes = encoded.encode("ascii")
        padding = b"=" * (-len(encoded_bytes) % 4)
        raw = base64.b64decode(encoded_bytes + padding, altchars=b"-_", validate=True)
        if len(raw) > MAX_ALLOWLIST_JSON_BYTES:
            raise ValueError("package allowlist exceeds 16384 bytes")
        decoded = json.loads(raw.decode("utf-8"))
        return normalize_packages(decoded)
    except ValueError:
        # json.JSONDecodeError / UnicodeError 都是 ValueError 子类，勿并列捕获 (G.ERR.09)。
        logger.warning("[package_allowlist] allowlist header rejected, treating as empty")
        return []


def _packages_from_metadata(metadata: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(metadata, Mapping) or METADATA_KEY not in metadata:
        return []
    try:
        packages = normalize_packages(metadata.get(METADATA_KEY))
        ensure_packages_fit(packages)
    except ValueError:
        logger.warning("[package_allowlist] request packages rejected, treating as empty")
        return []
    return packages


@contextmanager
def bind_outbound_allowlist(packages: list[dict[str, Any]]) -> Iterator[None]:
    """Gateway 组 HTTP 头期间持有刚从库里读出的名单。"""
    token = _OUTBOUND.set(list(packages))
    try:
        yield
    finally:
        _OUTBOUND.reset(token)


def outbound_allowlist_packages() -> list[dict[str, Any]] | None:
    """未绑定时为 None。调用方不得改用客户端自带的名单。"""
    current = _OUTBOUND.get()
    if current is None:
        return None
    return list(current)


def apply_allowlist_to_envelope(envelope: Any, packages: list[dict[str, Any]]) -> None:
    """覆盖 channel_context 里的名单，不沿用客户端字段。"""
    ctx = getattr(envelope, "channel_context", None)
    copied = dict(ctx) if isinstance(ctx, dict) else {}
    copied[METADATA_KEY] = list(packages)
    envelope.channel_context = copied


@contextmanager
def bind_request_subject(metadata: Mapping[str, Any] | None) -> Iterator[None]:
    """把本次请求的用户和名单绑定到允许名单判断。不写入业务 params。"""
    from jiuwenswarm.common.request_identity import web_routing_identity

    identity = web_routing_identity(metadata if isinstance(metadata, Mapping) else None)
    subject_token = _SUBJECT.set((identity.get("user_id", ""), identity.get("group_id", "")))
    allowlist_token = _ALLOWLIST.set(
        _packages_from_metadata(metadata if isinstance(metadata, Mapping) else None)
    )
    try:
        yield
    finally:
        for var, token in ((_ALLOWLIST, allowlist_token), (_SUBJECT, subject_token)):
            try:
                var.reset(token)
            except ValueError:
                var.set(None)


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
    from jiuwenswarm.enterprise import is_enterprise

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
