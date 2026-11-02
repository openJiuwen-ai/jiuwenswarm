# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# 真源自已迁：纯契约部分自 jiuwenswarm/enterprise/tenant/tenant_paths.py 拆出，
# 母仓侧原路径经 re-export 继续提供，server/gateway 两侧恒为同一实现。
"""多租户 tenant_key 纯契约：md5("{group_id}::{bot_id}::{user_id}")。"""

from __future__ import annotations

import hashlib

_TENANT_KEY_SEP = "::"

__all__ = [
    "normalize_tenant_key_part",
    "build_tenant_key",
]


def normalize_tenant_key_part(value: str | None, *, default: str = "default") -> str:
    text = default if value is None else str(value).strip() or default
    return text


def build_tenant_key(
    group_id: str | None = None,
    bot_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Compose a stable opaque ``tenant_key``.

    先归一三元组并按 ``group_id::bot_id::user_id`` 拼接，再做 MD5 hex，
    用作池缓存键与企业版 ``workspace_{tenant_key}`` 目录名，避免明文落盘/打日志。
    """
    raw = _TENANT_KEY_SEP.join(
        (
            normalize_tenant_key_part(group_id),
            normalize_tenant_key_part(bot_id),
            normalize_tenant_key_part(user_id),
        )
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
