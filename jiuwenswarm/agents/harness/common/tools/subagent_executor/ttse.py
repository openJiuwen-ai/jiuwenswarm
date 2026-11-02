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


def install_subagent_ttse_wiring(agent: Any, provider: TTSEConsultProvider) -> bool:
    if getattr(agent, _MARKER, False):
        return True
    original = getattr(agent, "create_subagent", None)
    if not callable(original):
        return False

    def create_subagent(*args: Any, **kwargs: Any) -> Any:
        child = original(*args, **kwargs)
        if getattr(child, _CHILD_MARKER, None) is not None:
            return child
        try:
            rail = SubagentTTSEConsultRail(
                provider,
                subagent_type=str(args[0] if args else kwargs.get("subagent_type", "")),
                subsession_id=str(
                    args[1] if len(args) > 1 else kwargs.get("subsession_id", "")
                ),
            )
            if getattr(child, "_initialized", False):
                # DeepAgent 实例也可直接作为 spec。已初始化实例的 pending rail
                # 不会再装配，推迟到下一次初始化边界注册，避免后台注册竞态。
                original_ensure = child._ensure_initialized
                lock = asyncio.Lock()

                async def ensure_initialized() -> None:
                    await original_ensure()
                    async with lock:
                        if rail._agent is None:
                            await child.register_rail(rail)

                child._ensure_initialized = ensure_initialized
            else:
                child.add_rail(rail)
            setattr(child, _CHILD_MARKER, rail)
        except Exception:
            logger.warning("[subagent.ttse] rail attachment failed", exc_info=True)
        return child

    agent.create_subagent = create_subagent
    setattr(agent, _MARKER, True)
    return True
