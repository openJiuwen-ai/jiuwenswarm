# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""专家包允许名单的线上约定。

只规范化 kind、id、体积和报文。不读产品形态，不校验 match_expr 能不能求值。
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

METADATA_KEY = "package_allowlist"
HEADER_NAME = "X-Jiuwenswarm-Package-Allowlist"
MAX_ALLOWLIST_JSON_BYTES = 16 * 1024


def _hub_asset_id(encoded: str) -> str:
    """Decode a base64-wrapped Hub asset id (catalog key, not a secret)."""
    return base64.b64decode(encoded.encode("ascii")).hex()


# 企业版目录不展示的 Hub asset id。公共层只保留这一份，不判断产品形态。
HIDDEN_HUB_ASSET_IDS = frozenset(
    (
        _hub_asset_id("eU2xv+iITWiX1i01hIlKWQ=="),  # 鸿蒙 App 开发专家
        _hub_asset_id("9jOxRcOrTkK4njb6NpssAA=="),  # 健康生活顾问
        _hub_asset_id("MsIj1puDTDW0C8OTXG+fZw=="),  # 旅游规划专家团
        _hub_asset_id("NZ846g53QWuNltI64sEr5Q=="),  # 身材管理专家团
    )
)

_CANONICAL_KINDS = {
    "agent_template": "agent_template",
    "agent_templates": "agent_template",
    "agent_group": "agent_group",
    "agent_groups": "agent_group",
}


def canonical_kind(kind: str) -> str | None:
    return _CANONICAL_KINDS.get(str(kind or "").strip())


def normalize_packages(raw: Any) -> list[dict[str, Any]]:
    """收成 ``[{kind, id, match_expr?}]``。重复的 kind+id 去掉，非法结构拒绝。"""
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
        match_expr = _structural_match_expr(item.get("match_expr"))
        if match_expr is not None:
            record["match_expr"] = match_expr
        packages.append(record)
    return packages


def _structural_match_expr(raw: Any) -> Any:
    """空范围保持为空。非空范围只保留字符串或字符串列表，不求值。"""
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    if isinstance(raw, list):
        items = []
        for item in raw:
            text = str(item or "").strip()
            if text:
                items.append(text)
        return items or None
    raise ValueError("match_expr is invalid")


def _packages_json(packages: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        packages,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def ensure_packages_fit(packages: list[dict[str, Any]]) -> None:
    """名单 JSON 超过 16KiB 时拒绝。"""
    if len(_packages_json(packages)) > MAX_ALLOWLIST_JSON_BYTES:
        raise ValueError("package allowlist exceeds 16384 bytes")


def encode_package_allowlist_header(packages: list[dict[str, Any]]) -> str:
    """把名单编成内部 HTTP 头。结构非法或过大时改发空名单。"""
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
    """还原内部 HTTP 头。缺失或结构损坏时为空名单。不校验 match_expr。"""
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
