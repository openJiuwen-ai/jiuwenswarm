# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""企业配置读库 facade（企业版）。

委托 ``gateway/storage/backends/db/reader`` 访问；每网关独立数据库，查询不加实例行级隔离。
"""

from __future__ import annotations

from typing import Any

from agent_gateway.storage.backends.db import reader as _db_reader

from jiuwenswarm.common.utils import logger
from .schemas import (
    EXECUTION_GUARD_ENTITY_TABLE,
    EXECUTION_GUARD_SLOT,
    REACT_ENTITY_TABLE,
    REACT_SLOT,
    SLOT_ENTITY_TABLE,
    TemplateRefSlot,
)


def _resolve_slot_storage(slot: str) -> tuple[str, str]:
    if slot == REACT_SLOT:
        # e2a 协议包尚未登记 REACT 槽位，仓内字符串接线。
        return REACT_ENTITY_TABLE, "template_id"
    if slot == EXECUTION_GUARD_SLOT:
        # e2a 协议包尚未登记 EXECUTION_GUARD 槽位，仓内字符串接线。
        return EXECUTION_GUARD_ENTITY_TABLE, "template_id"
    try:
        slot_key = TemplateRefSlot(slot)
    except ValueError as exc:
        raise ValueError(
            f"unknown template_ref slot {slot!r} "
            f"(known: {[s.value for s in TemplateRefSlot]})"
        ) from exc
    id_field = (
        "policy_id"
        if slot_key is TemplateRefSlot.A2A_ACCESS_POLICY
        else "template_id"
    )
    return SLOT_ENTITY_TABLE[slot_key], id_field


async def fetch_templates_by_slot(
    slot: str,
    template_ids: list[str],
) -> list[dict[str, Any]]:
    """按 ``template_ref`` 槽位与 ``template_id`` 列表批量加载启用中的模板行。

    ``template_id`` 使用 foundation DB 的 ``IN`` 语义一次查出；返回顺序与入参
    ``template_ids`` 一致（去重后）。未命中的 id 不出现在结果中。
    """
    table, id_field = _resolve_slot_storage(slot)
    refs: list[str] = []
    seen: set[str] = set()
    for raw in template_ids:
        ref = str(raw or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
    if not refs:
        return []
    filters: dict[str, Any] = {"enabled": True, id_field: refs}
    rows = await list_records(table, filters=filters)
    by_id = {
        str(row.get(id_field) or "").strip(): row
        for row in rows
        if str(row.get(id_field) or "").strip()
    }
    return [by_id[ref] for ref in refs if ref in by_id]


async def fetch_template_by_slot(
    slot: str,
    template_id: str,
) -> dict[str, Any] | None:
    """按 ``template_ref`` 槽位与 ``template_id`` 加载一条启用中的模板行。"""
    rows = await fetch_templates_by_slot(slot, [template_id])
    return rows[0] if rows else None


async def list_records(
    table: str,
    *,
    filters: dict[str, Any] | None = None,
    order_by: str = "",
) -> list[dict[str, Any]]:
    """列表查询（每网关独立 DB，不加实例隔离列）。"""
    if not _db_reader.is_safe_ident(table or ""):
        logger.warning("[enterprise_config] invalid table name: %r", table)
        return []
    return await _db_reader.list_records(table, query=filters, order_by=order_by)


async def fetch_advanced_config(
    agent_template_id: str,
) -> dict[str, Any] | None:
    """按 ``agent_template_id`` 读取 advanced_config 一行。"""
    tid = str(agent_template_id or "").strip()
    if not tid:
        return None
    rows = await list_records(
        "advanced_config",
        filters={"agent_template_id": tid},
    )
    if not rows:
        return None
    row = rows[0]
    return dict(row) if isinstance(row, dict) else None


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """Coerce *value* to a bool, falling back to *default*."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_pos_int_or_none(value: Any) -> int | None:
    """Coerce *value* to a positive int, else None."""
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (ValueError, TypeError):
        return None
    return n if n >= 1 else None


def _normalize_swarmflow(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """规范化 ``expert_package_template.swarmflow`` JSON 为 Swarmflow 策略。

    只保留原始 JSON 中出现的键（键：allowed / default_enabled / max_workflows /
    agents_per_run / max_agents_total / swarmflow_budget），并做类型收敛；空对象返回 None。

    数值键统一「无效 / 空 / null 即未下发」（结果里不出现该键）：解析侧
    （team_helpers → team_manager）在键缺失时保留 config.yaml 的
    ``swarmflow_concurrency`` 部署底座，因此表单序列化出的 null 不会把
    部署已收紧的上限放大回引擎默认值。
    """
    if not isinstance(raw, dict) or not raw:
        return None
    policy: dict[str, Any] = {}
    if "allowed" in raw:
        policy["allowed"] = _coerce_bool(raw.get("allowed"), default=True)
    if "default_enabled" in raw:
        policy["default_enabled"] = _coerce_bool(raw.get("default_enabled"), default=False)
    if "max_workflows" in raw:
        policy["max_workflows"] = _coerce_pos_int_or_none(raw.get("max_workflows"))
        if policy["max_workflows"] is None:
            del policy["max_workflows"]
    if "agents_per_run" in raw:
        policy["agents_per_run"] = _coerce_pos_int_or_none(raw.get("agents_per_run"))
        if policy["agents_per_run"] is None:
            del policy["agents_per_run"]
    if "max_agents_total" in raw:
        policy["max_agents_total"] = _coerce_pos_int_or_none(raw.get("max_agents_total"))
        if policy["max_agents_total"] is None:
            del policy["max_agents_total"]
    if "swarmflow_budget" in raw:
        policy["swarmflow_budget"] = _coerce_pos_int_or_none(raw.get("swarmflow_budget"))
        if policy["swarmflow_budget"] is None:
            del policy["swarmflow_budget"]
    return policy or None


async def fetch_expert_package_swarmflow(
    template_ids: list[str],
) -> dict[str, Any] | None:
    """按入参引用顺序读取首个非空且已规范化的 swarmflow 策略。

    ``swarmflow`` 是 expert_package_template 上的 JSON 列（企业最小接入设计
    方案 §4.2）：键为 allowed / default_enabled / max_workflows / agents_per_run /
    max_agents_total / swarmflow_budget。入参顺序即优先级（与
    ``fetch_templates_by_slot`` 的顺序契约一致）：agent_template.template_ref 的
    expert_package 引用列表次序体现优先级，结果不依赖存储层返回顺序。
    无命中行或均为空时返回 None（运行时沿用引擎默认）。
    """
    import json

    refs: list[str] = []
    for raw in template_ids:
        tid = str(raw or "").strip()
        if tid and tid not in refs:
            refs.append(tid)
    if not refs:
        return None
    rows = await list_records(
        "expert_package_template",
        filters={"enabled": True, "template_id": refs},
    )
    by_id = {
        str(row.get("template_id") or "").strip(): row
        for row in rows
        if str(row.get("template_id") or "").strip()
    }
    for ref in refs:
        row = by_id.get(ref)
        if row is None:
            continue
        raw = row.get("swarmflow")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        if isinstance(raw, dict) and raw:
            normalized = _normalize_swarmflow(raw)
            if normalized is not None:
                return normalized
    return None


__all__ = (
    "fetch_advanced_config",
    "fetch_expert_package_swarmflow",
    "fetch_template_by_slot",
    "fetch_templates_by_slot",
    "list_records",
)
