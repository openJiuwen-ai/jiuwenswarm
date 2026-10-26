# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import AgentRuntime
from jiuwenswarm.gateway.routing.third_agent import ThirdAgent

if TYPE_CHECKING:
    from jiuwenswarm.extensions.agentos.agentos_router.router_client import (
        AgentOSRouterClient,
    )


class AgentOSThirdAgent(ThirdAgent):
    """AgentOS-backed third-party agent list/switch capability."""

    def __init__(self, router_client: AgentOSRouterClient) -> None:
        self._router = router_client

    def normalize_agent_type(self, raw: Any) -> str:
        return AgentRuntime.normalize_agent_type(raw)

    async def thirdagent_list(
        self,
        *,
        user_id: str,
        current_agent_type: str = "",
    ) -> dict[str, Any]:
        return await self._router.thirdagent_list(
            user_id=user_id,
            current_agent_type=current_agent_type,
        )

    async def thirdagent_switch(
        self,
        *,
        user_id: str,
        agent_type: str,
        session_id: str = "",
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del params
        return await self._router.thirdagent_switch(
            user_id=user_id,
            agent_type=agent_type,
            session_id=session_id,
        )
