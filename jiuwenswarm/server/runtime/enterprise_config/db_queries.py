# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""企业配置读库 facade（企业版）。

委托 ``gateway/storage/backends/db/reader`` 访问；每网关独立数据库，查询不加实例行级隔离。
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.utils import logger
from jiuwenswarm.gateway.storage.backends.db import reader as _db_reader

from .schemas import SLOT_ENTITY_TABLE, TemplateRefSlot


def _resolve_slot_storage(slot: str) -> tuple[str, str]:
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


__all__ = (
    "fetch_template_by_slot",
    "fetch_templates_by_slot",
    "list_records",
)
