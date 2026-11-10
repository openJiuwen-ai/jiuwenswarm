# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""把管理面按 Agent 模板下发的能力开关盖到当次配置上。

本地 YAML 仍是底座。管理面写了的字段覆盖它，没写的字段（尤其是本机路径）保留。
"""

from __future__ import annotations

import copy
import logging
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

# 当前这次请求已经合并好的 Agent 配置。没经过企业配置加载时为空，读法退回 YAML。
_RESOLVED_AGENT_CONFIG: ContextVar[dict[str, Any] | None] = ContextVar(
    "resolved_agent_config",
    default=None,
)

_SECTION_TABLES: tuple[tuple[str, str], ...] = (
    ("a2ui_config", "a2ui"),
)


def bind_resolved_agent_config(config: dict[str, Any] | None) -> None:
    """让同一次请求里直接读总配置的代码，看到这份已经盖过开关的配置。"""
    _RESOLVED_AGENT_CONFIG.set(config if isinstance(config, dict) else None)


def current_resolved_agent_config() -> dict[str, Any] | None:
    value = _RESOLVED_AGENT_CONFIG.get()
    return value if isinstance(value, dict) else None


def resolved_agent_config_or_yaml() -> dict[str, Any]:
    """企业请求优先用已合并的 Agent 配置，否则用本地 YAML。"""
    current = current_resolved_agent_config()
    if current is not None:
        return current
    from jiuwenswarm.common.config import get_config

    loaded = get_config()
    return loaded if isinstance(loaded, dict) else {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        current = base.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _deep_merge(current, value)
            continue
        base[key] = copy.deepcopy(value)


def apply_capability_bodies(
    config: dict[str, Any],
    bodies: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """把各张表的 body 盖进配置副本。"""
    merged = copy.deepcopy(config)
    for section, body in bodies.items():
        if not isinstance(body, dict) or not body:
            continue
        current = merged.get(section)
        target = copy.deepcopy(current) if isinstance(current, dict) else {}
        _deep_merge(target, body)
        merged[section] = target
    return merged


def _body_of(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    body = row.get("body")
    return dict(body) if isinstance(body, dict) else {}


async def apply_agent_capability_configs(
    config: dict[str, Any],
    loaded: Any,
) -> dict[str, Any]:
    """按这份生效配置的 Agent 模板，读取能力表并覆盖。"""
    from jiuwenswarm.enterprise import is_enterprise

    if not is_enterprise() or loaded is None or not isinstance(config, dict):
        return config
    template_id = str(getattr(loaded, "ref_template_id", "") or "").strip()
    if not template_id:
        return config

    from jiuwenswarm.enterprise.config.effective import db_queries

    bodies: dict[str, dict[str, Any]] = {}
    for table, section in _SECTION_TABLES:
        try:
            rows = await db_queries.list_records(
                table,
                filters={"agent_template_id": template_id},
            )
        except Exception as exc:  # noqa: BLE001 - 单表失败不挡整次对话
            logger.warning(
                "[enterprise_config] capability read failed table=%s template=%s error=%s",
                table,
                template_id,
                exc,
            )
            continue
        body = _body_of(rows[0] if rows else None)
        if body:
            bodies[section] = body
    if not bodies:
        return config
    return apply_capability_bodies(config, bodies)
