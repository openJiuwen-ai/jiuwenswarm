# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""ThirdAgent - Gateway 侧第三方 Agent list/switch 能力接口."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ThirdAgent(ABC):
    """第三方 Agent 目录 / 切换接口（Gateway 域）。"""

    def normalize_agent_type(self, raw: Any) -> str:
        """Normalize agent_type; default accepts any non-empty value."""
        agent_type = str(raw or "jiuwenswarm").strip().lower()
        return agent_type or "jiuwenswarm"

    @abstractmethod
    async def thirdagent_list(
        self,
        *,
        user_id: str,
        current_agent_type: str = "",
    ) -> dict[str, Any]:
        """Handle ``3rdagent.list`` for a user."""
        ...

    @abstractmethod
    async def thirdagent_switch(
        self,
        *,
        user_id: str,
        agent_type: str,
        session_id: str = "",
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Handle ``3rdagent.switch`` for (user_id, agent_type)."""
        ...


class UnsupportedThirdAgent(ThirdAgent):
    """Default implementation when no ThirdAgent extension is registered."""

    async def thirdagent_list(
        self,
        *,
        user_id: str,
        current_agent_type: str = "",
    ) -> dict[str, Any]:
        del user_id, current_agent_type
        return {
            "ok": False,
            "error": "3rdagent.list requires an AgentOS Router extension",
            "code": "UNSUPPORTED",
        }

    async def thirdagent_switch(
        self,
        *,
        user_id: str,
        agent_type: str,
        session_id: str = "",
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del user_id, agent_type, session_id, params
        return {
            "ok": False,
            "error": "3rdagent.switch requires an AgentOS Router extension",
            "code": "UNSUPPORTED",
        }


_UNSUPPORTED_THIRD_AGENT = UnsupportedThirdAgent()


def get_unsupported_third_agent() -> ThirdAgent:
    return _UNSUPPORTED_THIRD_AGENT
