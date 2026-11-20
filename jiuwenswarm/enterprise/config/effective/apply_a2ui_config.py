# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""将 ``EffectiveEnterpriseConfig.a2ui_config`` 写入 config 快照。

有行时覆盖 ``config.a2ui`` 的核心开关；无行时 fail closed：强制
``a2ui.enabled=false``，不回落 yaml 残留 true。
``data`` 仅合并非保留键，避免冲掉独立列。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SUPPORTED_PROTOCOL_VERSIONS = frozenset({"0.8"})
_A2UI_COLUMN_KEYS = frozenset(
    {
        "enabled",
        "protocol_version",
        "stream_validation_enabled",
        "dev_smoke_tools_enabled",
    }
)
# data / 表行里不应写回 a2ui 段的键
_A2UI_DATA_BLOCKLIST = frozenset(
    {
        *_A2UI_COLUMN_KEYS,
        "non_web_fallback_enabled",
        "agent_template_id",
        "id",
        "created_at",
        "updated_at",
        "data",
    }
)


def resolve_a2ui_config_row(enterprise: Any | None) -> dict[str, Any] | None:
    """取出企业生效快照上的 a2ui_config 行（dict）。"""
    if enterprise is None:
        return None
    row = getattr(enterprise, "a2ui_config", None)
    return dict(row) if isinstance(row, dict) else None


def _coerce_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_protocol_version(raw: Any) -> str:
    value = str(raw if raw is not None else "0.8").strip() or "0.8"
    if value not in _SUPPORTED_PROTOCOL_VERSIONS:
        return "0.8"
    return value


def _merge_a2ui_data(a2ui_cfg: dict[str, Any], data: Any) -> None:
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        name = str(key or "").strip()
        if not name or name in _A2UI_DATA_BLOCKLIST:
            continue
        a2ui_cfg[name] = value


def _ensure_a2ui_section(merged: dict[str, Any]) -> dict[str, Any]:
    existing = merged.get("a2ui")
    a2ui_cfg = dict(existing) if isinstance(existing, dict) else {}
    merged["a2ui"] = a2ui_cfg
    return a2ui_cfg


def apply_enterprise_a2ui_config_to_config(
    config_base: dict[str, Any],
    enterprise: Any | None,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base`` 并按 a2ui_config 覆盖 ``a2ui`` 段。

    Returns:
        ``(merged, applied)``：企业路径总会写入 enabled（有行或 fail closed）。
    """
    merged = copy.deepcopy(config_base)
    a2ui_cfg = _ensure_a2ui_section(merged)
    row = resolve_a2ui_config_row(enterprise)

    if row is None:
        a2ui_cfg["enabled"] = False
        # 保留/补齐安全默认，避免后续 get_a2ui_config 因缺字段或脏协议炸
        a2ui_cfg.setdefault("protocol_version", "0.8")
        a2ui_cfg["protocol_version"] = _normalize_protocol_version(
            a2ui_cfg.get("protocol_version")
        )
        a2ui_cfg.setdefault("stream_validation_enabled", True)
        a2ui_cfg.setdefault("dev_smoke_tools_enabled", False)
        logger.info(
            "[enterprise_a2ui_config] fail-closed: no a2ui_config row, enabled=false"
        )
        return merged, True

    a2ui_cfg["enabled"] = _coerce_bool(row.get("enabled"), False)
    a2ui_cfg["protocol_version"] = _normalize_protocol_version(
        row.get("protocol_version")
    )
    a2ui_cfg["stream_validation_enabled"] = _coerce_bool(
        row.get("stream_validation_enabled"), True
    )
    a2ui_cfg["dev_smoke_tools_enabled"] = _coerce_bool(
        row.get("dev_smoke_tools_enabled"), False
    )
    _merge_a2ui_data(a2ui_cfg, row.get("data"))

    logger.info(
        "[enterprise_a2ui_config] applied agent_template_id=%s enabled=%s "
        "protocol_version=%s",
        row.get("agent_template_id"),
        a2ui_cfg.get("enabled"),
        a2ui_cfg.get("protocol_version"),
    )
    return merged, True


__all__ = (
    "apply_enterprise_a2ui_config_to_config",
    "resolve_a2ui_config_row",
)
