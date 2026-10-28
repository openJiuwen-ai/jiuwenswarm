# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply a persistent expert-Team model at the next native member model call."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from openjiuwen.core.foundation.llm import Model, ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.server.runtime.organization_team_models import load_team_model, resolve_team_model

logger = logging.getLogger(__name__)


class OrganizationModelRail(DeepAgentRail):
    # After model planning (90), before runtime model identity (5).
    priority = 6

    def __init__(self, team_id: str, session_id: str, role: str) -> None:
        super().__init__()
        self.team_id = team_id
        self.session_id = session_id
        self.role = role
        self._signature = ""
        self._model: Any = None
        self._builder = None

    def init(self, agent: Any) -> None:
        self._builder = getattr(agent, "system_prompt_builder", None)
        if self.role != "leader" or self._builder is None:
            return
        self._builder.add_section(PromptSection(
            name="organization_team_model_policy", priority=5,
            content={
                "cn": (
                    "设置团队模型前，先根据创建/邀请结果或 org_view_organization 核对目标身份与已有设置。"
                    "Owner 主团不是子团，禁止对 owner_team_id 调用 org_set_team_model；"
                    "主团当前模型已符合要求时直接执行，不要为了确认而调用切换工具。"
                    "若主团模型不符合要求，应明确说明此工具不能修改主团，不要尝试对子团工具传入主团 ID。"
                    "仅当用户为真实子团指定了模型，且该子团尚无对应的整团持续设置或要求变更时，"
                    "才调用 org_set_team_model，使用真实 team_id 和已配置的模型名。"
                    "子团已有相同 execution_model_selection/持续模型时复用，不要由 Owner 和子团 Leader 重复设置。"
                    "仅当前模型相同但尚无整团持续设置，不等于完成了子团持久化切换。"
                    "该设置覆盖子团 Leader、现有及新建执行成员，持续到下一次明确变更。"
                    "不要只把模型要求写进任务描述，也不要仅创建指定模型的成员来代替整团切换。"
                    "切换失败时明确报告，不能声称已经使用指定模型。"
                ),
                "en": (
                    "Before changing a Team model, check its identity and saved selection from creation/invitation "
                    "results or org_view_organization. Never pass owner_team_id to org_set_team_model: "
                    "the Owner is not a child Team. If the Owner already uses the requested model, proceed without "
                    "a switch. Otherwise explain that this tool cannot change the Owner model. "
                    "For a real child Team, call org_set_team_model only when the user requests a model and "
                    "the matching persistent Team selection is missing or must change. Reuse a matching "
                    "execution_model_selection; the Owner and child Leader must not set it twice. "
                    "A matching current member model alone does not establish a persistent Team selection. "
                    "This persists for the Leader, existing and new native members until explicitly changed. "
                    "Writing a task constraint or spawning one member is not a Team-wide model switch. "
                    "Report a failed switch instead of claiming the requested model was applied."
                ),
            },
        ))

    def uninit(self, agent: Any) -> None:
        if self._builder is not None:
            self._builder.remove_section("organization_team_model_policy")
            self._builder.remove_section("organization_team_model_selection")
        self._builder = None
        self._model = None
        self._signature = ""

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        setting = load_team_model(self.team_id, self.session_id)
        if self._builder is not None:
            self._builder.remove_section("organization_team_model_selection")
            if setting is not None and self.role == "leader":
                content = json.dumps({
                    "team_id": self.team_id, "persistent_team_model": setting.model_name,
                    "execution_model_selection": setting.selection.model_dump(exclude_none=True),
                    "guidance": "Already set for the entire Team. Reuse it unless a different model is requested.",
                }, ensure_ascii=False)
                self._builder.add_section(PromptSection(
                    name="organization_team_model_selection", content={"cn": content, "en": content}, priority=5,
                ))
        if setting is None:
            return
        # Resolve on every call so removed/disabled models never silently run
        # on the old default. Credentials are held only in the live Model.
        compiled = resolve_team_model(setting.selection)
        payload = compiled.model_dump_json(exclude={"model_client_config": {"client_id"}})
        signature = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        agent = ctx.agent
        get_llm = getattr(agent, "get_llm", None)
        set_llm = getattr(agent, "set_llm", None)
        if not callable(set_llm):
            raise ValueError("Persistent Team model requires an agent with a callable set_llm")
        live_model = None
        if callable(get_llm):
            try:
                live_model = get_llm()
            except ValueError:
                pass
        if self._signature == signature and live_model is self._model:
            return
        model = Model(
            model_client_config=compiled.model_client_config,
            model_config=compiled.model_request_config,
        )
        set_llm(model)
        self._model = model
        self._signature = signature

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return
        if inputs.tool_name not in {"spawn_external_cli", "spawn_bridge_agent"}:
            return
        if load_team_model(self.team_id, self.session_id) is None:
            return
        error = "This Team has a persistent native model selection; external CLI/bridge members are unsupported."
        inputs.tool_result = {"success": False, "error": error}
        inputs.tool_msg = ToolMessage(content=error, tool_call_id=str(getattr(inputs.tool_call, "id", "")))
        ctx.extra["_skip_tool"] = True

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Expose ownership and saved child models before the Leader decides to switch."""
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
        owner_id = data.get("owner_team_id")
        for leader in data.get("leaders") or []:
            if not isinstance(leader, dict) or not leader.get("team_id"):
                continue
            for key in ("execution_model_name", "execution_model_selection", "execution_model_error"):
                leader.pop(key, None)
            leader["is_owner"] = leader["team_id"] == owner_id
            if leader["is_owner"]:
                continue
            try:
                setting = load_team_model(leader["team_id"], self.session_id)
            except (ValueError, OSError):
                leader["execution_model_error"] = "saved Team model is unavailable"
                logger.warning("Cannot read organization Team model for %s", leader["team_id"], exc_info=True)
                continue
            if setting is not None:
                leader["execution_model_name"] = setting.model_name
                leader["execution_model_selection"] = setting.selection.model_dump(exclude_none=True)
        data["model_selection_guidance"] = (
            "Never call org_set_team_model for owner_team_id. Reuse a child's matching persistent "
            "execution_model_selection; only set a child model when missing or explicitly changed."
        )
        if inputs.tool_msg is not None:
            dump = getattr(result, "model_dump", None)
            body = result if isinstance(result, dict) else {"success": success, "data": data}
            if not isinstance(result, dict) and callable(dump):
                body = dump()
            try:
                inputs.tool_msg.content = json.dumps(body, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                logger.warning("Cannot serialize organization model facts", exc_info=True)
