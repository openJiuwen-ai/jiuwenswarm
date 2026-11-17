# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""在统一子 Agent 创建入口追加 TTSE 查询 Rail。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.agents.harness.common.rails.subagent_ttse_consult_rail import (
    SubagentTTSEConsultRail,
    TTSEConsultProvider,
)

logger = logging.getLogger(__name__)
_MARKER = "_jiuwenswarm_ttse_wiring_installed"
_CHILD_MARKER = "_jiuwenswarm_ttse_consult_rail"


def _register_before_execution(child: Any, rail: SubagentTTSEConsultRail) -> None:
    """在公开执行入口补挂 Rail，避免创建时后台注册的竞态。"""
    lock = asyncio.Lock()

    async def ensure_registered() -> None:
        async with lock:
            try:
                if child.is_registered_rail(rail):
                    return
                await child.register_rail(rail)
            except asyncio.CancelledError:
                await rollback_registration()
                raise
            except Exception:
                logger.warning(
                    "[subagent.ttse] deferred rail registration failed",
                    exc_info=True,
                )
                await rollback_registration()

    async def rollback_registration() -> None:
        try:
            await child.unregister_rail(rail)
        except Exception:
            logger.warning(
                "[subagent.ttse] deferred rail registration cleanup failed",
                exc_info=True,
            )

    original_invoke = child.invoke
    original_stream = child.stream

    async def invoke(*args: Any, **kwargs: Any) -> Any:
        await ensure_registered()
        return await original_invoke(*args, **kwargs)

    async def stream(*args: Any, **kwargs: Any) -> Any:
        await ensure_registered()
        source = original_stream(*args, **kwargs)
        try:
            async for chunk in source:
                yield chunk
        finally:
            await source.aclose()

    child.invoke = invoke
    child.stream = stream


def install_subagent_ttse_wiring(agent: Any, provider: TTSEConsultProvider) -> bool:
    if getattr(agent, _MARKER, False):
        return True
    original = getattr(agent, "create_subagent", None)
    if not callable(original):
        return False

    def create_subagent(*args: Any, **kwargs: Any) -> Any:
        child = original(*args, **kwargs)
        subagent_type = str(args[0] if args else kwargs.get("subagent_type", ""))
        subsession_id = str(args[1] if len(args) > 1 else kwargs.get("subsession_id", ""))
        existing_rail = getattr(child, _CHILD_MARKER, None)
        if existing_rail is not None:
            existing_rail.bind_task_context(subagent_type, subsession_id)
            return child
        try:
            rail = SubagentTTSEConsultRail(
                provider,
                subagent_type=subagent_type,
                subsession_id=subsession_id,
            )
            if child.is_initialized:
                # DeepAgent 实例也可直接作为 spec。已初始化实例的 pending rail
                # 不会再装配，推迟到公开执行入口注册，避免后台注册竞态。
                _register_before_execution(child, rail)
            else:
                child.add_rail(rail)
            setattr(child, _CHILD_MARKER, rail)
        except Exception:
            logger.warning("[subagent.ttse] rail attachment failed", exc_info=True)
        return child

    agent.create_subagent = create_subagent
    setattr(agent, _MARKER, True)
    return True
