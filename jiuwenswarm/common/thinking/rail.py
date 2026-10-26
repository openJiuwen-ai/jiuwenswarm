# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Rail that injects frozen thinking kwargs into each model call."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.thinking.types import (
    ThinkingProfile,
    kwargs_digest,
    thaw_llm_call_kwargs,
)
from jiuwenswarm.common.utils import logger


class ThinkingInjectRail(DeepAgentRail):
    """Replay frozen thinking llm_call_kwargs before each model call.

    Profile is resolved once at spawn/fork; this rail only injects a deep copy
    of the same kwargs every iteration (KV-cache friendly). Compatible with
    older cores that ignore ``llm_call_kwargs`` (injection is a no-op until
    core is upgraded).
    """

    priority = 15  # early, before most model-call rails

    def __init__(
        self,
        profile: ThinkingProfile | None,
        *,
        role_id: str = "",
        agent_id: str = "",
    ) -> None:
        super().__init__()
        self._profile = profile
        self._role_id = role_id or ""
        self._agent_id = agent_id or ""
        self._inject_logged = False

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        profile = self._profile
        if profile is None:
            return
        kwargs = getattr(profile, "llm_call_kwargs", None)
        if not kwargs:
            return
        try:
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict):
                return
            # Thaw to a deep mutable copy so inject mutations cannot affect profile.
            injected: dict[str, Any] = thaw_llm_call_kwargs(kwargs)
            extra["llm_call_kwargs"] = injected
            if not self._inject_logged:
                self._inject_logged = True
                logger.info(
                    "[Thinking] inject_once role_id=%s agent_id=%s model=%r "
                    "thinking=%s digest=%s",
                    self._role_id,
                    self._agent_id,
                    getattr(profile, "model_name", "") or "",
                    getattr(profile, "thinking", ""),
                    kwargs_digest(injected),
                )
        except Exception as exc:
            logger.warning(
                "[ThinkingInjectRail] inject skipped role_id=%s agent_id=%s: %s",
                self._role_id,
                self._agent_id,
                exc,
            )
