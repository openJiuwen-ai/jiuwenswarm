# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Leader-only host tool for persistent, isolated expert-Team model selection."""

import logging
from typing import Any

from openjiuwen.agent_teams.tools.tool_base import TeamTool
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.tool.base import ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.server.runtime.organization_team_models import publish_team_model, set_organization_team_model

logger = logging.getLogger(__name__)


class OrgSetTeamModelTool(TeamTool):
    def __init__(self, team_id: str, session_id: str, runtime: Any = None) -> None:
        super().__init__(ToolCard(
            id="swarm.org_set_team_model", name="org_set_team_model",
            description=(
                "Set a child expert Team's execution model for its Leader, existing and future native members. "
                "Only the organization Owner or that child Team's Leader may change it. "
                "Never target the organization Owner/owner_team_id, including when confirming its current model. "
                "Check org_view_organization first: reuse a child's matching execution_model_selection; "
                "call only when the requested persistent child selection is missing or must change. "
                "Takes effect on each member's next model call and persists until explicitly changed, including "
                "after task completion and recovery. Other Teams and the session model are unchanged. "
                "External CLI/bridge members are unsupported; unavailable or ambiguous models are rejected."
            ),
            input_params={
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string"},
                    "team_id": {
                        "type": "string",
                        "description": "Real child Team ID from org_view_organization. Never owner_team_id.",
                    },
                    "model_name": {
                        "type": "string",
                        "description": "Exact configured model name/alias, or model_id to disambiguate endpoints.",
                    },
                },
                "required": ["organization_id", "team_id", "model_name"],
            },
        ))
        self.team_id = team_id
        self.session_id = session_id
        self.runtime = runtime

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime = self.runtime or getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
        if runtime is None:
            return ToolOutput(success=False, error="Team runtime is unavailable")
        try:
            setting = await set_organization_team_model(
                runtime=runtime, caller_team_id=self.team_id, session_id=self.session_id,
                organization_id=inputs.get("organization_id", ""), target_team_id=inputs.get("team_id", ""),
                model_name=inputs.get("model_name", ""),
            )
        except (ValueError, OSError, BaseError) as exc:
            return ToolOutput(success=False, error=str(exc))
        try:
            await publish_team_model(setting)
        except Exception:
            logger.warning("Team model saved but notification failed for %s", setting.team_id, exc_info=True)
        return ToolOutput(success=True, data={
            "team_id": setting.team_id, "model_name": setting.model_name,
            "model_selection": setting.selection.model_dump(exclude_none=True),
            "applies_to": "leader_and_all_native_members", "effective": "next_model_call",
            "persistent": True,
        })
