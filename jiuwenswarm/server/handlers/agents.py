# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agents.list / get / create / update / delete / enable / disable / tools_list。"""

from __future__ import annotations

import json
import logging
import re

from jiuwenswarm.common.config import (
    get_config,
    remove_subagent_from_config,
    upsert_subagent_in_config,
)
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)

_AGENT_CREATION_SYSTEM_PROMPT = """\
You are an elite AI agent architect. When given an agent name and description, your job is to design a high-performance agent that EXECUTES tasks to completion — not just analyzes and reports.

The agent will have access to tools (Read, Write, Edit, Bash, etc.) to complete tasks. Design it as an autonomous expert capable of handling its designated tasks with minimal additional guidance. The system prompt you write is the agent's complete operational manual.

1. **whenToUse**: A precise description of when the main assistant should dispatch to this agent.
   - Start with "Use this agent when..."
   - Include concrete triggering conditions
   - Add 2-3 <example> blocks showing specific scenarios where the assistant uses the Agent tool to fully delegate the task
   - Each <example> should show: user says X → assistant dispatches to this agent with the Agent tool, passing the complete task
   - Write in the same language as the agent description (Chinese description → Chinese whenToUse)

2. **systemPrompt**: The complete system prompt governing the agent's behavior.
   - Define expert persona and role
   - Specify workflow and methodology — end-to-end, from analysis through execution
   - Establish clear behavioral boundaries and operational parameters
   - Provide specific methodologies and best practices for task execution
   - Define output format expectations when relevant
   - Include self-verification steps
   - Write in the same language as the agent description

Key principles:
- Be specific rather than generic — avoid vague instructions
- Include concrete examples when they would clarify behavior
- Balance comprehensiveness with clarity — every instruction should add value
- Ensure the agent has enough context to handle variations of the core task
- Build in quality assurance and self-correction mechanisms

Return ONLY a JSON object:
{"whenToUse": "...", "systemPrompt": "..."}
"""


async def _send(ctx: RequestContext, response: AgentResponse) -> None:
    await ctx.sink.send_response(response)


async def handle_agents_list(ctx: RequestContext) -> None:
    from dataclasses import asdict as dataclass_asdict

    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    request = ctx.request
    try:
        workspace_dir = request.params.get("workspace_dir") if request.params else None
        service = AgentConfigService(workspace_dir)
        agents = service.list_agents()
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"agents": [dataclass_asdict(a) for a in agents]},
        )
    except Exception as e:
        logger.exception("[AgentWebSocketServer] agents.list failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_agents_get(ctx: RequestContext) -> None:
    from dataclasses import asdict as dataclass_asdict

    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    request = ctx.request
    try:
        params = request.params or {}
        name = params.get("name", "")
        workspace_dir = params.get("workspace_dir")
        service = AgentConfigService(workspace_dir)
        agent = service.get_agent(name)
        if agent is None:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": f"Agent 不存在: {name}"},
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"agent": dataclass_asdict(agent)},
            )
    except Exception as e:
        logger.exception("[AgentWebSocketServer] agents.get failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def generate_agent_with_llm(
    ctx: RequestContext, name: str, description: str
) -> tuple[str, str] | None:
    """调用 LLM 生成 agent 的 whenToUse 和 systemPrompt。

    Returns:
        (when_to_use, system_prompt) 或 None（生成失败时回退到模板）
    """
    model = ctx.services.resolve_model(None)
    if model is None:
        logger.warning("[agents.create] no model available for LLM generation")
        return None

    from openjiuwen.core.foundation.llm.schema.message import UserMessage

    full_prompt = f"""{_AGENT_CREATION_SYSTEM_PROMPT}

---
请为以下 agent 生成配置：

名称: {name}
描述: {description}

返回 JSON 对象，包含 whenToUse 和 systemPrompt 两个字段。不要返回其他内容。"""

    try:
        result = await model.invoke(
            [UserMessage(content=full_prompt)],
            max_tokens=2000,
            temperature=0.3,
        )
        text = getattr(result, "content", None) or str(result)
    except Exception:
        logger.exception("[agents.create] LLM generation failed")
        return None

    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            logger.warning("[agents.create] no JSON found in LLM response: %s", text[:200])
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("[agents.create] JSON parse failed: %s", text[:200])
            return None

    when_to_use = (data.get("whenToUse") or "").strip()
    system_prompt = (data.get("systemPrompt") or "").strip()

    if not when_to_use or not system_prompt:
        logger.warning("[agents.create] incomplete LLM response: %s", data)
        return None

    return when_to_use, system_prompt


async def handle_agents_create(ctx: RequestContext) -> None:
    from dataclasses import asdict as dataclass_asdict

    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService, CreateAgentParams

    request = ctx.request
    try:
        params = dict(request.params or {})
        workspace_dir = params.pop("workspace_dir", None)
        generate = params.pop("generate", True)

        generated = False
        if generate:
            name = params.get("name", "")
            description = params.get("description", "")
            if name and description:
                llm_result = await generate_agent_with_llm(ctx, name, description)
                if llm_result:
                    params["when_to_use"] = llm_result[0]
                    params["prompt"] = llm_result[1]
                    generated = True

        create_params = CreateAgentParams(**{
            key: value
            for key, value in params.items()
            if key in CreateAgentParams.__dataclass_fields__
        })
        service = AgentConfigService(workspace_dir)
        agent = service.create_agent(create_params)
        applied = True
        reload_error = ""
        try:
            upsert_subagent_in_config(agent.name, enabled=True)
            await ctx.services.agent_manager.reload_agents_config(get_config(), None)
        except Exception as reload_exc:
            applied = False
            reload_error = str(reload_exc)
            logger.warning("[AgentWebSocketServer] agents.create reload failed: %s", reload_exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "agent": dataclass_asdict(agent),
                "generated": generated,
                "applied": applied,
                "reload_error": reload_error or None,
            },
        )
    except Exception as e:
        logger.exception("[AgentWebSocketServer] agents.create failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_agents_update(ctx: RequestContext) -> None:
    from dataclasses import asdict as dataclass_asdict

    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService, UpdateAgentParams

    request = ctx.request
    try:
        params = dict(request.params or {})
        name = params.pop("name", "")
        workspace_dir = params.pop("workspace_dir", None)
        generate = params.pop("generate", False)

        generated = False
        if generate and name and params.get("description"):
            llm_result = await generate_agent_with_llm(ctx, name, params["description"])
            if llm_result:
                params["when_to_use"] = llm_result[0]
                params["prompt"] = llm_result[1]
                generated = True

        update_params = UpdateAgentParams(**{
            key: value
            for key, value in params.items()
            if key in UpdateAgentParams.__dataclass_fields__
        })
        service = AgentConfigService(workspace_dir)
        agent = service.update_agent(name, update_params)

        applied = True
        reload_error = ""
        try:
            await ctx.services.agent_manager.reload_agents_config(get_config(), None)
        except Exception as reload_exc:
            applied = False
            reload_error = str(reload_exc)
            logger.warning("[AgentWebSocketServer] agents.update reload failed: %s", reload_exc)

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "agent": dataclass_asdict(agent),
                "generated": generated,
                "applied": applied,
                "reload_error": reload_error or None,
            },
        )
    except Exception as e:
        logger.exception("[AgentWebSocketServer] agents.update failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_agents_delete(ctx: RequestContext) -> None:
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    request = ctx.request
    try:
        params = request.params or {}
        name = params.get("name", "")
        workspace_dir = params.get("workspace_dir")
        service = AgentConfigService(workspace_dir)
        ok = service.delete_agent(name)
        applied = True
        reload_error = ""
        try:
            remove_subagent_from_config(name)
            await ctx.services.agent_manager.reload_agents_config(get_config(), None)
        except Exception as reload_exc:
            applied = False
            reload_error = str(reload_exc)
            logger.warning("[AgentWebSocketServer] agents.delete reload failed: %s", reload_exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"ok": ok, "applied": applied, "reload_error": reload_error or None},
        )
    except Exception as e:
        logger.exception("[AgentWebSocketServer] agents.delete failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_agents_set_enabled(ctx: RequestContext, enabled: bool) -> None:
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    request = ctx.request
    action = "enable" if enabled else "disable"
    try:
        params = request.params or {}
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("agent name is required")
        workspace_dir = params.get("workspace_dir")
        service = AgentConfigService(workspace_dir)
        agent = service.get_agent(name)
        if agent is None:
            raise ValueError(f"Agent 不存在: {name}")
        if agent.source == "builtin":
            raise ValueError(f"不能启用/禁用内置 agent: {name}")

        upsert_subagent_in_config(name, enabled=enabled)
        applied = True
        reload_error = ""
        try:
            await ctx.services.agent_manager.reload_agents_config(get_config(), None)
        except Exception as reload_exc:
            applied = False
            reload_error = str(reload_exc)
            logger.warning(
                "[AgentWebSocketServer] agents.%s reload failed: %s", action, reload_exc
            )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "name": name,
                "enabled": enabled,
                "applied": applied,
                "reload_error": reload_error or None,
            },
        )
    except Exception as e:
        logger.exception("[AgentWebSocketServer] agents.%s failed: %s", action, e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_agents_enable(ctx: RequestContext) -> None:
    await handle_agents_set_enabled(ctx, True)


async def handle_agents_disable(ctx: RequestContext) -> None:
    await handle_agents_set_enabled(ctx, False)


async def handle_agents_tools_list(ctx: RequestContext) -> None:
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    request = ctx.request
    try:
        params = request.params or {}
        workspace_dir = params.get("workspace_dir")
        service = AgentConfigService(workspace_dir)
        result = service.list_available_tools()
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=result,
        )
    except Exception as e:
        logger.exception("[AgentWebSocketServer] agents.tools_list failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)
