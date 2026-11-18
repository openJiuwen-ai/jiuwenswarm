# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register a Swarm-owned request without changing the Gateway package."""

from typing import Any

from agent_gateway.extensions.sdk import ApplicationPluginExtension
from agent_gateway.routing.e2a_proxy import proxy_unary_request

from jiuwenswarm.common.schema.message import ReqMethod


class ExpertTeamsExtension(ApplicationPluginExtension):
    plugin_id = "expert-teams"

    async def initialize(self, config: Any) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def bind_web_channel(self, channel: Any, services: Any) -> None:
        async def add_teams(ws: Any, req_id: str, params: dict, session_id: str | None) -> None:
            await proxy_unary_request(
                channel=channel, ws=ws, req_id=req_id,
                agent_client=services.require_agent_client(),
                req_method=ReqMethod.ORG_EXPERT_TEAMS_ADD,
                params=params, session_id=session_id,
                user_id=channel.connection_user_id(ws),
                timeout_seconds=180, label="org.expert_teams.add",
            )

        channel.register_method("org.expert_teams.add", add_teams, local_only=True)


async def register_extensions(registry: Any) -> list[ExpertTeamsExtension]:
    extension = ExpertTeamsExtension()
    registry.register_application_plugin(extension)
    return [extension]
