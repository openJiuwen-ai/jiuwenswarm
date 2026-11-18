# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""将 ``EffectiveEnterpriseConfig.advanced_config`` 写入 config 快照。

``advanced_config`` 一行绑定一个 ``agent_template_id``，列与本地
``config.yaml`` 同名键对应；当前优先落地 ``a4p.enabled``。
无行时保留 yaml 默认（企业版开箱 a4p 仍关闭）。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)


def resolve_advanced_config_row(enterprise: Any | None) -> dict[str, Any] | None:
    """取出企业生效快照上的 advanced_config 行（dict）。"""
    if enterprise is None:
        return None
    row = getattr(enterprise, "advanced_config", None)
    return dict(row) if isinstance(row, dict) else None


def apply_enterprise_advanced_config_to_config(
    config_base: dict[str, Any],
    enterprise: Any | None,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base`` 并按 advanced_config 覆盖对应开关。

    Returns:
        ``(merged, applied)``：``applied=True`` 表示至少写入了一项。
    """
    row = resolve_advanced_config_row(enterprise)
    if row is None:
        return config_base, False

    merged = copy.deepcopy(config_base)
    applied = False

    if "a4p_enabled" in row:
        a4p = merged.get("a4p")
        a4p_cfg = dict(a4p) if isinstance(a4p, dict) else {}
        a4p_cfg["enabled"] = bool(row.get("a4p_enabled"))
        merged["a4p"] = a4p_cfg
        applied = True

    if "progressive_tool_enabled" in row:
        merged["progressive_tool_enabled"] = bool(row.get("progressive_tool_enabled"))
        applied = True
    if "progressive_tool_optimize" in row:
        merged["progressive_tool_optimize"] = bool(row.get("progressive_tool_optimize"))
        applied = True
    if "auto_memory_enabled" in row:
        merged["auto_memory_enabled"] = bool(row.get("auto_memory_enabled"))
        applied = True
    if "enable_kv_cache_affinity" in row:
        kv = merged.get("kv_cache_affinity_config")
        kv_cfg = dict(kv) if isinstance(kv, dict) else {}
        kv_cfg["enable_kv_cache_affinity"] = bool(row.get("enable_kv_cache_affinity"))
        merged["kv_cache_affinity_config"] = kv_cfg
        applied = True

    if applied:
        logger.info(
            "[enterprise_advanced_config] applied agent_template_id=%s a4p_enabled=%s",
            row.get("agent_template_id"),
            (merged.get("a4p") or {}).get("enabled")
            if isinstance(merged.get("a4p"), dict)
            else None,
        )
    return merged, applied


__all__ = (
    "apply_enterprise_advanced_config_to_config",
    "resolve_advanced_config_row",
)
