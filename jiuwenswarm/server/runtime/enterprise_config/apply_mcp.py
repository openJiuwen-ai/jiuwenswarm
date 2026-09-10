"""将 ``EffectiveEnterpriseConfig`` 中的 MCP 模板写入 config 快照。

企业版一旦走企业配置合并路径，即用管理端下发结果 **整表替换** ``mcp.servers``：
- 有模板实体 → 只保留这些
- 无槽位 / 空列表 / 未加载到企业配置 → ``servers=[]``，禁止本地 config.yaml MCP 继续生效
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from .schemas import EffectiveEnterpriseConfig

logger = logging.getLogger(__name__)


def _template_id_label(entity: Any) -> str:
    if not isinstance(entity, dict):
        return "<unknown>"
    tid = str(entity.get("template_id") or "").strip()
    return tid or "<unknown>"


def clear_local_mcp_servers(config_base: dict[str, Any]) -> dict[str, Any]:
    """深拷贝并将 ``mcp.servers`` 置为空列表（企业版禁用本地 MCP）。"""
    merged = copy.deepcopy(config_base)
    mcp_section = merged.get("mcp")
    if not isinstance(mcp_section, dict):
        mcp_section = {}
        merged["mcp"] = mcp_section
    merged["mcp"]["servers"] = []
    return merged


def mcp_entity_to_server_entry(entity: dict[str, Any]) -> dict[str, Any] | None:
    """将 ``mcp_template`` 行转为 ``config.yaml`` ``mcp.servers[]`` 单条。

    只认模板行 ``enabled``；``mcp_entry.enabled`` 忽略。写入 servers 时固定
    ``enabled=True``，供运行时 ``extract_enabled_mcp_server_entries`` 使用。

    模板行 ``enabled=False`` 为正常跳过，不打告警。entity 非 dict、``mcp_entry``
    缺失/非 dict、``name`` 为空视为数据损坏，打 ``warning`` 便于运维排查。
    """
    if not isinstance(entity, dict):
        logger.warning(
            "[enterprise_mcp] skip MCP template: entity is not a dict, got %s",
            type(entity).__name__,
        )
        return None
    template_id = _template_id_label(entity)
    if not bool(entity.get("enabled", True)):
        return None
    entry = entity.get("mcp_entry")
    if not isinstance(entry, dict):
        logger.warning(
            "[enterprise_mcp] skip MCP template template_id=%s: "
            "mcp_entry missing or not a dict, got %s",
            template_id,
            type(entry).__name__,
        )
        return None
    name = str(entry.get("name", "")).strip()
    if not name:
        logger.warning(
            "[enterprise_mcp] skip MCP template template_id=%s: mcp_entry.name is empty",
            template_id,
        )
        return None
    normalized = copy.deepcopy(entry)
    normalized.pop("enabled", None)
    normalized["name"] = name
    transport = str(normalized.get("transport", "")).strip().lower()
    if transport:
        normalized["transport"] = transport
    # 运行时 servers[] 仍需要 enabled 字段；企业侧开关已由模板行决定
    normalized["enabled"] = True
    return normalized


def apply_enterprise_mcp_to_config(
    config_base: dict[str, Any],
    enterprise: EffectiveEnterpriseConfig,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base`` 并写入企业 MCP 槽位；返回 ``(merged, applied_any)``。

    ``enterprise.mcp is None`` 与空列表同等：清空本地 ``mcp.servers``。
    """
    mcp_entities = getattr(enterprise, "mcp", None)
    if mcp_entities is None:
        mcp_entities = []

    enterprise_entries: list[dict[str, Any]] = []
    for entity in mcp_entities:
        entry = mcp_entity_to_server_entry(entity)
        if entry is not None:
            enterprise_entries.append(entry)

    # 同名后者覆盖，保持确定性顺序（首次出现顺序）
    by_name: dict[str, dict[str, Any]] = {}
    for entry in enterprise_entries:
        name = str(entry.get("name", "")).strip()
        if name:
            by_name[name] = entry

    merged = copy.deepcopy(config_base)
    mcp_section = merged.get("mcp")
    if not isinstance(mcp_section, dict):
        mcp_section = {}
        merged["mcp"] = mcp_section
    merged["mcp"]["servers"] = list(by_name.values())
    return merged, True
