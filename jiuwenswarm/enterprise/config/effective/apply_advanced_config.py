# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""将企业高级配置写入 config 快照。

企业版不依赖 ``config.yaml`` 中的这五个开关：先套用代码写死的默认值
（数值对齐当前包内 yaml 默认，但不读取 yaml），再在有启用模板时用
``advanced_config_template`` 列覆盖。本阶段不合并模板 ``data``。
个人版不进入本函数。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 与 resources/config.yaml 当前默认对齐；企业未绑模板时以此为准，不读 yaml。
DEFAULT_ADVANCED_CONFIG_SWITCHES: dict[str, bool] = {
    "progressive_tool_enabled": True,
    "progressive_tool_optimize": False,
    "a4p_enabled": False,
    "auto_memory_enabled": False,
    "enable_kv_cache_affinity": False,
}


def resolve_advanced_config_row(enterprise: Any | None) -> dict[str, Any] | None:
    """取出企业生效快照上首个启用的高级配置模板行。"""
    entities = (
        getattr(enterprise, "advanced_config", None) if enterprise is not None else None
    )
    if not entities:
        return None

    if isinstance(entities, (list, tuple)) and len(entities) > 1:
        logger.warning(
            "[enterprise_advanced_config] multiple advanced config templates "
            "configured, only the first one takes effect: ids=%s",
            [
                entity.get("template_id")
                for entity in entities
                if isinstance(entity, dict)
            ],
        )
    entity = entities[0] if isinstance(entities, (list, tuple)) else entities
    if not isinstance(entity, dict) or entity.get("enabled") is False:
        return None
    return dict(entity)


def _apply_switches_to_config(
    merged: dict[str, Any],
    switches: dict[str, bool],
) -> None:
    """把五开关写入 ``merged``（就地修改）。"""
    merged["progressive_tool_enabled"] = bool(
        switches["progressive_tool_enabled"]
    )
    merged["progressive_tool_optimize"] = bool(
        switches["progressive_tool_optimize"]
    )
    merged["auto_memory_enabled"] = bool(switches["auto_memory_enabled"])

    a4p = merged.get("a4p")
    a4p_cfg = dict(a4p) if isinstance(a4p, dict) else {}
    a4p_cfg["enabled"] = bool(switches["a4p_enabled"])
    merged["a4p"] = a4p_cfg

    kv = merged.get("kv_cache_affinity_config")
    kv_cfg = dict(kv) if isinstance(kv, dict) else {}
    kv_cfg["enable_kv_cache_affinity"] = bool(
        switches["enable_kv_cache_affinity"]
    )
    merged["kv_cache_affinity_config"] = kv_cfg


def _switches_from_template_row(row: dict[str, Any]) -> dict[str, bool]:
    """模板行优先；缺列回落到代码默认（不读 yaml）。"""
    out = dict(DEFAULT_ADVANCED_CONFIG_SWITCHES)
    for key in DEFAULT_ADVANCED_CONFIG_SWITCHES:
        if key in row:
            out[key] = bool(row[key])
    return out


def apply_enterprise_advanced_config_to_config(
    config_base: dict[str, Any],
    enterprise: Any | None,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base``，用代码默认值铺底，再用模板列覆盖。

    Returns:
        ``(merged, applied)``：企业路径总会写入五开关，``applied`` 恒为 ``True``。
    """
    merged = copy.deepcopy(config_base)
    row = resolve_advanced_config_row(enterprise)
    if row is None:
        switches = dict(DEFAULT_ADVANCED_CONFIG_SWITCHES)
        _apply_switches_to_config(merged, switches)
        logger.info(
            "[enterprise_advanced_config] applied code defaults "
            "(no enabled advanced_config template) "
            "progressive_tool_enabled=%s progressive_tool_optimize=%s "
            "a4p_enabled=%s auto_memory_enabled=%s "
            "enable_kv_cache_affinity=%s",
            switches["progressive_tool_enabled"],
            switches["progressive_tool_optimize"],
            switches["a4p_enabled"],
            switches["auto_memory_enabled"],
            switches["enable_kv_cache_affinity"],
        )
        return merged, True

    switches = _switches_from_template_row(row)
    _apply_switches_to_config(merged, switches)
    logger.info(
        "[enterprise_advanced_config] applied template_id=%s "
        "progressive_tool_enabled=%s progressive_tool_optimize=%s "
        "a4p_enabled=%s auto_memory_enabled=%s "
        "enable_kv_cache_affinity=%s",
        row.get("template_id"),
        switches["progressive_tool_enabled"],
        switches["progressive_tool_optimize"],
        switches["a4p_enabled"],
        switches["auto_memory_enabled"],
        switches["enable_kv_cache_affinity"],
    )
    return merged, True


def code_default_advanced_config_snapshot() -> dict[str, Any]:
    """仅含五开关的最小 config 快照（代码写死默认，不读 yaml）。"""
    merged: dict[str, Any] = {}
    _apply_switches_to_config(merged, dict(DEFAULT_ADVANCED_CONFIG_SWITCHES))
    return merged


def enterprise_bare_runtime_config_snapshot() -> dict[str, Any]:
    """企业版无 adapter 缓存时的运行时 config。

    以进程 ``get_config()`` 为全量基底（保留 modes/memory、a4p 其它键等），
    再用代码写死的五开关覆盖——五开关仍不采信 yaml 值。
    """
    from jiuwenswarm.common.config import get_config

    cfg = get_config()
    merged = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    _apply_switches_to_config(merged, dict(DEFAULT_ADVANCED_CONFIG_SWITCHES))
    return merged


def _adapter_config_base_cache(
    adapter: Any | None,
    *,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Prefer the session-scoped child adapter cache when present."""
    if adapter is None:
        return None
    if session_id:
        get_session = getattr(adapter, "_get_cached_session_adapter", None)
        if callable(get_session):
            try:
                session_adapter = get_session(session_id)
            except Exception:
                session_adapter = None
            if session_adapter is not None:
                cache = getattr(session_adapter, "_config_base_cache", None)
                if isinstance(cache, dict):
                    return cache
    cache = getattr(adapter, "_config_base_cache", None)
    return cache if isinstance(cache, dict) else None


def resolve_runtime_config_for_advanced_switches(
    *,
    adapter: Any | None = None,
    config: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """解析读取五开关时应使用的 config 字典。

    优先级：
    1. 显式传入的 ``config``
    2. adapter / session adapter 的 ``_config_base_cache``
       （企业版已由 ``apply_enterprise_advanced_config_to_config`` 写入 DB 或代码默认）
    3. 企业版：进程 config 全量基底 + 五开关代码默认覆盖（不采信 yaml 五开关）
    4. 个人版：``get_config()`` / ``config.yaml``
    """
    if isinstance(config, dict):
        return config

    cached = _adapter_config_base_cache(adapter, session_id=session_id)
    if cached is not None:
        return cached

    from jiuwenswarm.enterprise.edition import is_enterprise

    if is_enterprise():
        return enterprise_bare_runtime_config_snapshot()

    from jiuwenswarm.common.config import get_config

    cfg = get_config()
    return cfg if isinstance(cfg, dict) else {}


__all__ = (
    "DEFAULT_ADVANCED_CONFIG_SWITCHES",
    "apply_enterprise_advanced_config_to_config",
    "code_default_advanced_config_snapshot",
    "enterprise_bare_runtime_config_snapshot",
    "resolve_advanced_config_row",
    "resolve_runtime_config_for_advanced_switches",
)
