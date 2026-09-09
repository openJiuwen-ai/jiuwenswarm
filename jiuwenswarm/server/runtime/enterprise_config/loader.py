"""从 Gateway DB 按实例 Agent 资源加载企业级生效配置。"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.utils import logger

from . import db_queries
from .schemas import (
    DEFAULT_AGENT_LOAD_SLOTS,
    MODEL_SLOT_KEYS,
    EffectiveEnterpriseConfig,
    RoutingContext,
    TemplateRefSlot,
    normalize_template_ref,
)


def routing_context_from_request(request: AgentRequest | Any) -> RoutingContext:
    """从顶层 ``user_id`` + ``metadata.routing`` 解析路由上下文。"""
    from jiuwenswarm.common.request_identity import web_routing_identity

    meta = getattr(request, "metadata", None)
    identity = web_routing_identity(meta if isinstance(meta, dict) else None)
    return RoutingContext(
        group_id=str(identity.get("group_id") or "").strip(),
        bot_id=str(identity.get("bot_id") or "").strip(),
        user_id=str(identity.get("user_id") or "").strip(),
    )


def _apply_slot_entities(
    result: EffectiveEnterpriseConfig,
    slot: str,
    entities: list[dict[str, Any]],
) -> None:
    if slot in {s.value for s in MODEL_SLOT_KEYS}:
        result.models[slot] = entities
    elif slot == TemplateRefSlot.EMBEDDING_MODEL:
        result.embedding = entities
    elif slot == TemplateRefSlot.SKILL_PREBUILT:
        result.skill_prebuilt = entities
    elif slot == TemplateRefSlot.EXTENSION_CONFIG:
        result.extension_config = entities
    elif slot == TemplateRefSlot.MCP:
        result.mcp = entities
    elif slot == TemplateRefSlot.PERMISSIONS:
        result.permissions = entities
    elif slot == TemplateRefSlot.A2A_ACCESS_POLICY:
        result.a2a_access_policy = entities


def _any_requested_slot_loaded(
    result: EffectiveEnterpriseConfig,
    load_slots: frozenset[str],
) -> bool:
    for slot in load_slots:
        if slot in {s.value for s in MODEL_SLOT_KEYS} and result.models.get(slot):
            return True
        if slot == TemplateRefSlot.EMBEDDING_MODEL and result.embedding:
            return True
        if slot == TemplateRefSlot.SKILL_PREBUILT and result.skill_prebuilt:
            return True
        if slot == TemplateRefSlot.EXTENSION_CONFIG and result.extension_config:
            return True
        if slot == TemplateRefSlot.MCP and result.mcp is not None:
            return True
        if slot == TemplateRefSlot.PERMISSIONS and result.permissions:
            return True
        if (
            slot == TemplateRefSlot.A2A_ACCESS_POLICY
            and result.a2a_access_policy is not None
        ):
            return True
    return False


async def _fetch_slot_entities(
    slot: str,
    template_ids: list[str],
) -> list[dict[str, Any]]:
    entities = await db_queries.fetch_templates_by_slot(slot, template_ids)
    requested = {str(tid or "").strip() for tid in template_ids} - {""}
    id_field = (
        "policy_id"
        if slot == TemplateRefSlot.A2A_ACCESS_POLICY
        else "template_id"
    )
    found = {str(row.get(id_field) or "").strip() for row in entities} - {""}
    missing = requested - found
    if missing:
        logger.warning(
            "[enterprise_config] templates not found: slot=%r template_ids=%s",
            slot,
            sorted(missing),
        )
    return entities


async def _fetch_instance_agent_resource(resource_id: str) -> dict[str, Any] | None:
    rid = str(resource_id or "").strip()
    if not rid:
        return None
    rows = await db_queries.list_records(
        "instance_agent_resource",
        filters={"enabled": True, "resource_id": rid},
    )
    return rows[0] if rows else None


async def _fetch_agent_template_row(template_id: str) -> dict[str, Any] | None:
    tid = str(template_id or "").strip()
    if not tid:
        return None
    rows = await db_queries.list_records(
        "agent_template",
        filters={"enabled": True, "template_id": tid},
    )
    return rows[0] if rows else None


def _literal_slot_template_id_map(
    refs: dict[str, list[str]],
) -> dict[str, list[str]]:
    """A2A 策略仅接受一个字面 policy_id，其余槽位保持原引用。"""
    slot_template_id_map = dict(refs)
    policy_refs = slot_template_id_map.get(TemplateRefSlot.A2A_ACCESS_POLICY)
    if policy_refs is None:
        return slot_template_id_map
    if (
        len(policy_refs) != 1
        or policy_refs[0].startswith("${")
        or " or " in policy_refs[0].lower()
    ):
        slot_template_id_map.pop(TemplateRefSlot.A2A_ACCESS_POLICY, None)
    return slot_template_id_map


async def load_effective_enterprise_config(
    request: AgentRequest | Any,
    slots: Collection[TemplateRefSlot],
) -> EffectiveEnterpriseConfig | None:
    """按 ``request.bot_id``（即 ``instance_agent_resource.resource_id``）加载 Agent 实例生效配置。

    读取实例 Agent 资源 → ``agent_template`` → 按 ``template_ref`` 中的
    ``template_id`` 加载模型等模板实体。
    """
    if not is_enterprise():
        return None

    ctx = routing_context_from_request(request)
    if not slots:
        raise ValueError("slots must not be empty")

    rid = str(ctx.bot_id or "").strip()
    if not rid:
        logger.warning(
            "[enterprise_config] bot_id(resource_id) missing in request context %s",
            ctx.as_dict(),
        )
        return None

    load_slots = frozenset(slot.value for slot in slots)

    resource_row = await _fetch_instance_agent_resource(rid)
    if resource_row is None:
        logger.warning(
            "[enterprise_config] instance_agent_resource not found or disabled: "
            "resource_id=%r",
            rid,
        )
        return None

    ref_template_id = str(resource_row.get("ref_template_id") or "").strip()
    if not ref_template_id:
        logger.warning(
            "[enterprise_config] instance_agent_resource missing ref_template_id: resource_id=%r",
            rid,
        )
        return None

    agent_template_row = await _fetch_agent_template_row(ref_template_id)
    if agent_template_row is None:
        logger.warning(
            "[enterprise_config] agent_template not found or disabled: "
            "resource_id=%r ref_template_id=%r",
            rid,
            ref_template_id,
        )
        return None

    merged_refs = normalize_template_ref(agent_template_row.get("template_ref"))
    filtered_refs = {
        slot: refs
        for slot, refs in merged_refs.items()
        if slot in load_slots
    }

    if not filtered_refs:
        logger.warning(
            "[enterprise_config] agent_template has no template_ref for resource_id=%r slots=%s",
            rid,
            sorted(load_slots),
        )
        return None

    slot_template_id_map = _literal_slot_template_id_map(filtered_refs)
    if not slot_template_id_map:
        logger.warning(
            "[enterprise_config] agent template_ref has no valid references "
            "for resource_id=%r refs=%s",
            rid,
            filtered_refs,
        )
        return None

    result = EffectiveEnterpriseConfig(
        routing=ctx,
        resource_id=rid,
        instance_agent_resource=resource_row,
        ref_template_id=ref_template_id,
        agent_template=agent_template_row,
        template_ref=slot_template_id_map,
        debug={
            "load_slots": sorted(load_slots),
        },
    )

    for slot, template_ids in slot_template_id_map.items():
        try:
            entities = await _fetch_slot_entities(slot, template_ids)
        except Exception as exc:
            if slot != TemplateRefSlot.A2A_ACCESS_POLICY:
                raise
            logger.warning(
                "[enterprise_config] A2A policy load failed: resource_id=%r "
                "template_ids=%s error_type=%s",
                rid,
                template_ids,
                type(exc).__name__,
            )
            continue
        if entities:
            _apply_slot_entities(result, slot, entities)

    if not _any_requested_slot_loaded(result, load_slots):
        logger.warning(
            "[enterprise_config] no template entities loaded for resource_id=%r "
            "template_ref=%s ctx=%s slots=%s",
            rid,
            slot_template_id_map,
            ctx.as_dict(),
            sorted(load_slots),
        )
        return None

    logger.info(
        "[enterprise_config] loaded enterprise config by resource_id=%r slots=%s",
        rid,
        sorted(load_slots),
    )
    return result


__all__ = (
    "DEFAULT_AGENT_LOAD_SLOTS",
    "EffectiveEnterpriseConfig",
    "TemplateRefSlot",
    "load_effective_enterprise_config",
    "routing_context_from_request",
)
