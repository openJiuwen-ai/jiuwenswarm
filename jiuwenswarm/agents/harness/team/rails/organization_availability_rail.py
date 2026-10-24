# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep model-visible organization membership separate from executable availability."""

from __future__ import annotations

import json
import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.prompts import PromptSection

from jiuwenswarm.server.runtime.organization_packages import team_package_availability

logger = logging.getLogger(__name__)
_SECTION = "organization_package_availability"


class OrganizationAvailabilityRail(DeepAgentRail):
    """Refresh uninstall facts before each model call and enrich roster tool results."""

    def __init__(self, team_id: str, session_id: str) -> None:
        super().__init__()
        self.team_id = team_id
        self.session_id = session_id
        self.builder = None

    def init(self, agent: Any) -> None:
        self.builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        if self.builder is not None:
            self.builder.remove_section(_SECTION)
        self.builder = None

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        target_fields = {
            "org_delegate_task": "to_team_id", "org_create_task": "delegated_to_team_id",
            "org_send_leader_message": "to_team_id", "org_invite_team": "team_id",
            "org_activate_and_invite_team": "team_id",
        }
        if not isinstance(inputs, ToolCallInputs) or inputs.tool_name not in target_fields:
            return
        args = inputs.tool_args if isinstance(inputs.tool_args, dict) else {}
        target = str(args.get(target_fields[inputs.tool_name]) or "")
        if not target or team_package_availability(target).get("available") is not False:
            return
        error = "Target expert Team is unavailable because its AgentGroup package was removed. Reinstall it first."
        ctx.extra["_skip_tool"] = True
        inputs.tool_result = {"success": False, "error": error}
        inputs.tool_msg = ToolMessage(content=error, tool_call_id=str(getattr(inputs.tool_call, "id", "")))

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self.builder is None:
            return
        self.builder.remove_section(_SECTION)
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
        if runtime is None:
            return
        entry = await runtime.pool.get(self.team_id)
        if entry is None or entry.current_session_id != self.session_id:
            return
        manager = getattr(getattr(entry.agent, "team_backend", None), "org_task_manager", None)
        if manager is None:
            return
        try:
            organization = await manager.get_organization()
            availability = []
            for leader in organization.leaders if organization is not None else []:
                status = team_package_availability(leader.team_id)
                if "available" in status:
                    availability.append({"team_id": leader.team_id, **status})
        except Exception as exc:
            logger.warning("Organization availability lookup failed: %s", exc)
            return
        if availability:
            notice = (
                "Current AgentGroup availability for this organization; this snapshot supersedes earlier messages. "
                "Members with available=false are unavailable and must not receive new work; ask the user to reinstall "
                "their package. Membership and history remain for reference. available=true means the package is "
                "installed again, not that its Team is necessarily idle or already loaded.\n"
                + json.dumps(availability, ensure_ascii=False)
            )
            self.builder.add_section(PromptSection(
                name=_SECTION,
                content={"en": notice, "cn": notice},
                priority=5,
            ))

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return
        if inputs.tool_name != "org_view_organization" or ctx.exception is not None:
            return
        result = inputs.tool_result
        success = result.get("success") if isinstance(result, dict) else getattr(result, "success", False)
        data = result.get("data") if isinstance(result, dict) else getattr(result, "data", None)
        if success is not True or not isinstance(data, dict):
            return
        for leader in data.get("leaders") or []:
            if isinstance(leader, dict) and leader.get("team_id"):
                leader.update(team_package_availability(leader["team_id"]))
        data["next_action"] = (
            "Membership is historical, not proof of availability. Teams with available=false cannot execute or claim "
            "new work until their AgentGroup package is reinstalled. Use only currently available teams."
        )
        if inputs.tool_msg is not None:
            body = result if isinstance(result, dict) else result.model_dump()
            inputs.tool_msg.content = json.dumps(body, ensure_ascii=False, default=str)
