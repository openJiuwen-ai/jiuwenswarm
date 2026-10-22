# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Register TaskTool subagent thinking hook with openjiuwen core.

Core TaskTool calls ``apply_subagent_thinking`` after ``create_subagent``.
When this host hook is registered, semantic ``thinking``
(default|off|on|low|medium|high) is adapted to vendor kwargs and attached via
:class:`ThinkingInjectRail`. ``default`` is no-op except GLM-5.3 family
(auto-inject product default depth).
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.thinking.adapter import adapt_thinking
from jiuwenswarm.common.thinking.rail import ThinkingInjectRail
from jiuwenswarm.common.utils import logger

_REGISTERED = False


def _on_subagent_thinking(subagent: Any, *, thinking: str, model: Any = None) -> None:
    """Attach ThinkingInjectRail when vendor-mapped kwargs are available."""
    profile = adapt_thinking(thinking, model=model)
    if not getattr(profile, "injected", False):
        # default (non-GLM-5.3) / unsupported / invalid → leave as-is
        if getattr(profile, "degraded", False):
            logger.info(
                "[Thinking] skip inject degraded reason=%s thinking=%r model=%r",
                getattr(profile, "reason", ""),
                getattr(profile, "thinking", ""),
                getattr(profile, "model_name", ""),
            )
        return

    role_id = ""
    agent_id = ""
    card = getattr(subagent, "card", None)
    if card is not None:
        agent_id = str(getattr(card, "id", "") or "")
        role_id = str(getattr(card, "name", "") or agent_id)

    rail = ThinkingInjectRail(profile, role_id=role_id, agent_id=agent_id)
    add_rail = getattr(subagent, "add_rail", None)
    if not callable(add_rail):
        logger.warning("[Thinking] subagent has no add_rail; skip inject")
        return
    add_rail(rail)
    logger.info(
        "[Thinking] rail attached role_id=%s agent_id=%s thinking=%s model=%r",
        role_id,
        agent_id,
        profile.thinking,
        profile.model_name,
    )


def register_thinking_hook() -> None:
    """Idempotent registration of the core TaskTool thinking hook."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from openjiuwen.harness.tools.subagent.thinking_hook import (
            register_subagent_thinking_hook,
        )
    except ImportError as exc:
        logger.warning(
            "[Thinking] core thinking_hook unavailable (upgrade openjiuwen): %s",
            exc,
        )
        return

    register_subagent_thinking_hook(_on_subagent_thinking)
    _REGISTERED = True
    logger.info("[Thinking] TaskTool thinking hook registered")


__all__ = ["register_thinking_hook"]
