# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved

"""Web 会话历史库：配置与类型解析（个人版纯内存；企业版 mysql/pg）。

本模块不 import foundation，个人版加载安全。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_DB_NAME = "web"


def _env(*names: str, default: str = "") -> str:
    for name in names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return default


def resolve_history_db_type() -> str:
    """解析 Web 会话历史库类型。

    优先级：``WEB_DB_TYPE`` → ``DB_TYPE`` → 已配 ``WEB_DB_HOST`` 则 ``mysql`` →
    按 ``DEPLOYMENT_MODE``（企业 active-standby/distributed ``mysql``，standalone ``memory``）。

    sqlite 已废弃：两端均不支持（个人版纯内存、企业版仅 mysql/pg）。
    若显式配置 ``sqlite``，由 ``ChatHistoryStore.for_db_type`` 回退内存并告警。
    """
    explicit = _env("WEB_DB_TYPE") or _env("DB_TYPE")
    if explicit:
        return explicit.strip().lower()
    if _env("WEB_DB_HOST"):
        return "mysql"
    try:
        from jiuwenswarm.deployment_mode import (
            history_storage_backend,
            normalize_deployment_mode,
        )

        mode = normalize_deployment_mode(os.getenv("DEPLOYMENT_MODE", "standalone"))
        return history_storage_backend(mode)
    except Exception:
        return "memory"


@dataclass(frozen=True)
class WebHistoryDbSettings:
    """Web 历史库远程连接（MySQL / PostgreSQL，独立 database ``web``）。"""

    host: str
    port: int
    user: str
    password: str
    database: str = _DEFAULT_DB_NAME
    pg_schema: str = "public"

    @classmethod
    def from_env(cls) -> "WebHistoryDbSettings | None":
        host = _env("WEB_DB_HOST")
        if not host:
            return None
        port_raw = _env("WEB_DB_PORT", default="3306")
        try:
            port = int(port_raw)
        except ValueError:
            port = 3306
        return cls(
            host=host,
            port=port,
            user=_env("WEB_DB_USER", default="root"),
            password=_env("WEB_DB_PASSWORD"),
            database=_env("WEB_DB_NAME", default=_DEFAULT_DB_NAME) or _DEFAULT_DB_NAME,
            pg_schema=_env("WEB_PG_SCHEMA", default="public") or "public",
        )
