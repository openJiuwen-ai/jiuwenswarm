# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业级 cron 门控与路由三元组工具。"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.deployment_mode import MODE_DISTRIBUTED, normalize_deployment_mode

logger = logging.getLogger(__name__)

STICKY_IDENTITY_FIELDS = frozenset(
    {"group_id", "bot_id", "user_id", "job_id", "created_at"}
)


def _resolve_deployment_mode() -> str:
    try:
        from jiuwenswarm.common.config import get_config

        mode = (get_config().get("gateway") or {}).get("deployment_mode")
        if mode:
            return normalize_deployment_mode(mode)
    except Exception as exc:
        logger.debug("deployment mode from config unavailable: %s", exc)
    return normalize_deployment_mode(os.getenv("DEPLOYMENT_MODE", "standalone"))


def enterprise_cron_enabled(*, deployment_mode: str | None = None) -> bool:
    """企业 cron 开门：企业版启用。

    standalone / active-standby：保持历史行为。
    distributed：多副本时需 ``GATEWAY_DB_HOST``，由库表认领避免重复调度。
    """
    if not is_enterprise():
        return False
    mode = normalize_deployment_mode(
        deployment_mode if deployment_mode is not None else _resolve_deployment_mode()
    )
    if mode == MODE_DISTRIBUTED:
        return bool(os.getenv("GATEWAY_DB_HOST", "").strip())
    return True


def coerce_routing_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_routing_triple(*sources: Any) -> tuple[str | None, str | None, str | None]:
    """从若干 dict / 对象按优先级解析 group_id / bot_id / user_id。

    优先顶层 ``user_id`` + ``metadata.routing``；其次已 merge 过的 params
    （Gateway 本地 cron handler）。
    """
    from jiuwenswarm.common.request_identity import web_routing_identity

    group_id: str | None = None
    bot_id: str | None = None
    user_id: str | None = None

    def _merge_from_mapping(mapping: dict[str, Any]) -> None:
        nonlocal group_id, bot_id, user_id
        if group_id is None:
            group_id = coerce_routing_id(mapping.get("group_id"))
        if bot_id is None:
            bot_id = coerce_routing_id(mapping.get("bot_id"))
        if user_id is None:
            user_id = coerce_routing_id(mapping.get("user_id"))

    for source in sources:
        if source is None:
            continue
        if isinstance(source, dict):
            identity = web_routing_identity(source)
            if identity:
                _merge_from_mapping(identity)
            # 本地 handler 的扁平 params（merge_routing_into_params 副本 / UT）：
            # 无 routing 键时继续从顶层补 group/bot/user。
            if not isinstance(source.get("routing"), Mapping):
                _merge_from_mapping(source)
            continue
        metadata = getattr(source, "metadata", None)
        if isinstance(metadata, dict):
            identity = web_routing_identity(metadata)
            if identity:
                _merge_from_mapping(identity)
        params = getattr(source, "params", None)
        if isinstance(params, dict):
            _merge_from_mapping(params)

    return group_id, bot_id, user_id


def routing_triple_complete(
    group_id: str | None,
    bot_id: str | None,
    user_id: str | None,
) -> bool:
    return bool(group_id and bot_id and user_id)


def strip_sticky_identity_fields(patch: dict[str, Any]) -> dict[str, Any]:
    """从 update patch 中剥离禁止改动的身份字段。"""
    out = dict(patch or {})
    for key in STICKY_IDENTITY_FIELDS:
        out.pop(key, None)
    return out


def job_matches_routing(
    job: Any,
    *,
    group_id: str | None,
    bot_id: str | None,
    user_id: str | None,
    match: str = "and",
    include_unbound: bool = False,
) -> bool:
    """按维过滤单条任务。"""
    jg = coerce_routing_id(
        getattr(job, "group_id", None) if not isinstance(job, dict) else job.get("group_id")
    )
    jb = coerce_routing_id(
        getattr(job, "bot_id", None) if not isinstance(job, dict) else job.get("bot_id")
    )
    ju = coerce_routing_id(
        getattr(job, "user_id", None) if not isinstance(job, dict) else job.get("user_id")
    )

    if not (jg or jb or ju):
        return bool(include_unbound)

    wanted = [
        (group_id, jg),
        (bot_id, jb),
        (user_id, ju),
    ]
    active = [(w, j) for w, j in wanted if w]
    if not active:
        return True

    if str(match or "and").strip().lower() == "or":
        return any(w == j for w, j in active)
    return all(w == j for w, j in active)


__all__ = (
    "STICKY_IDENTITY_FIELDS",
    "coerce_routing_id",
    "enterprise_cron_enabled",
    "extract_routing_triple",
    "job_matches_routing",
    "routing_triple_complete",
    "strip_sticky_identity_fields",
)
