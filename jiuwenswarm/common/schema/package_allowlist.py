# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""专家包允许名单的线上约定。

实现已收口到 ``openjiuwen_protocol.package_allowlist``（gateway 与 AgentServer
共用的唯一来源）。本模块仅 re-export，保留既有导入路径兼容；只规范化 kind、
id、体积和报文，不读产品形态，不校验 match_expr 能不能求值。
"""

from __future__ import annotations

from openjiuwen_protocol.package_allowlist import (
    HIDDEN_HUB_ASSET_IDS,
    MAX_PACKAGE_ALLOWLIST_JSON_BYTES as MAX_ALLOWLIST_JSON_BYTES,
    PACKAGE_ALLOWLIST_HEADER_NAME as HEADER_NAME,
    PACKAGE_ALLOWLIST_METADATA_KEY as METADATA_KEY,
    PACKAGE_KIND_ALIASES,
    PACKAGE_KIND_DIRS,
    apply_allowlist_to_envelope,
    canonical_kind,
    decode_package_allowlist_header,
    encode_package_allowlist_header,
    ensure_packages_fit,
    normalize_packages,
)

__all__ = [
    "HEADER_NAME",
    "HIDDEN_HUB_ASSET_IDS",
    "MAX_ALLOWLIST_JSON_BYTES",
    "METADATA_KEY",
    "PACKAGE_KIND_ALIASES",
    "PACKAGE_KIND_DIRS",
    "apply_allowlist_to_envelope",
    "canonical_kind",
    "decode_package_allowlist_header",
    "encode_package_allowlist_header",
    "ensure_packages_fit",
    "normalize_packages",
]
