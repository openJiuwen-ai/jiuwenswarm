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
        self._agent_groups: dict[str, str] = {}

    def init(self, agent: Any) -> None:
        self.builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        if self.builder is not None:
            self.builder.remove_section(_SECTION)
        self.builder = None
        self._agent_groups.clear()

    async def _team_availability(self, team_id: str, agent_group_name: str = "") -> dict[str, Any]:
        """Use current package facts across prompts, roster results, and dispatch guards."""
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
        entry = await runtime.pool.get(team_id) if runtime is not None else None
        metadata = {}
        if entry is not None and entry.current_session_id == self.session_id:
            metadata = getattr(getattr(entry.agent, "spec", None), "metadata", None) or {}
        group = str(
            metadata.get("agent_group_name") or agent_group_name or self._agent_groups.get(team_id, "")
        ).strip()
        status = team_package_availability(team_id, group)
        resolved_group = str(status.get("agent_group_name") or group).strip()
        if resolved_group or metadata.get("expert_team"):
            # Retain only identity hints, never an installed/uninstalled verdict.
            self._agent_groups[team_id] = resolved_group
        known_expert = team_id in self._agent_groups
        if "available" not in status and known_expert:
            return {"available": False, "unavailable_reason": "AGENT_GROUP_IDENTITY_UNKNOWN"}
        # No package identity on an ordinary, Summary, or legacy Team is not
        # evidence of removal. Its existing tool/runtime validation still applies.
        return status

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        target_fields = {
            "org_delegate_task": "to_team_id", "org_create_task": "delegated_to_team_id",
            "org_send_leader_message": "to_team_id", "org_invite_team": "team_id",
            "org_activate_and_invite_team": "team_id",
        }
        if not isinstance(inputs, ToolCallInputs) or inputs.tool_name not in target_fields:
            return
        args = inputs.tool_args
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                return
        if not isinstance(args, dict):
            return
        target = str(args.get(target_fields[inputs.tool_name]) or "")
        if not target:
            return
        status = await self._team_availability(target)
        if status.get("available") is not False:
            return
        error = (
            "Target expert Team is unavailable or its AgentGroup identity cannot be verified. "
            "Reinstall its package or restore its identity before retrying."
        )
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
                status = await self._team_availability(leader.team_id)
                if "available" in status:
                    availability.append({"team_id": leader.team_id, **status})
        except Exception as exc:
            logger.warning("Organization availability lookup failed: %s", exc)
            return
        if availability:
            notice = (
                "Current AgentGroup availability for this organization; this snapshot supersedes earlier messages. "
                "Members with available=false must not receive new work; ask the user to reinstall their package or "
                "restore its identity. Membership and history remain for reference. available=true means the package "
                "is installed again, not that its Team is necessarily idle or already loaded.\n"
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
                status = await self._team_availability(leader["team_id"], leader.get("agent_group_name", ""))
                leader.update(status)
        data["next_action"] = (
            "Membership is historical, not proof of availability. Teams with available=false cannot execute or claim "
            "new work until their package is reinstalled or its identity is restored. "
            "Use only currently available teams."
        )
        if inputs.tool_msg is not None:
            try:
                body = result if isinstance(result, dict) else {"success": success, "data": data}
                dump = getattr(result, "model_dump", None)
                if not isinstance(result, dict) and callable(dump):
                    body = dump()
                content = json.dumps(body, ensure_ascii=False, default=str)
            except Exception as exc:
                logger.warning("Cannot serialize organization roster tool result: %s", exc)
                return
            inputs.tool_msg.content = content
