# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agents management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
import asyncio
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.config import remove_subagent_from_config
from jiuwenswarm.common.config import upsert_subagent_in_config

logger = logging.getLogger(__name__)


async def _reset_active_browser_runtimes_if_available(browser_move: Any) -> int:
    """Reset active browser runtimes when supported by the installed SDK."""
    reset_runtimes = getattr(browser_move, "reset_active_browser_runtimes", None)
    if not callable(reset_runtimes):
        logger.warning(
            "[AgentWebSocketServer] installed openjiuwen does not support reset_active_browser_runtimes; restarting the local browser runtime server only"
        )
        return 0
    return await reset_runtimes()


async def _reset_requested_browser_runtime_if_available(
    browser_move: Any, params: dict[str, Any]
) -> int:
    """Prefer an identity-scoped reset and retain compatibility with older SDKs."""
    reset_runtime = getattr(browser_move, "reset_managed_browser_runtime", None)
    display_mode = str(params.get("display_mode") or "").strip().lower()
    profile_name = str(params.get("profile_name") or "").strip()
    if callable(reset_runtime) and display_mode and profile_name:
        return await reset_runtime(
            browser_key=str(params.get("browser_key") or "").strip(),
            profile_name=profile_name,
            display_mode=display_mode,
            browser_binary=str(params.get("browser_binary") or "").strip(),
        )
    return await _reset_active_browser_runtimes_if_available(browser_move)


_AGENT_CREATION_SYSTEM_PROMPT = 'You are an elite AI agent architect. When given an agent name and description, your job is to design a high-performance agent that EXECUTES tasks to completion — not just analyzes and reports.\n\nThe agent will have access to tools (Read, Write, Edit, Bash, etc.) to complete tasks. Design it as an autonomous expert capable of handling its designated tasks with minimal additional guidance. The system prompt you write is the agent\'s complete operational manual.\n\n1. **whenToUse**: A precise description of when the main assistant should dispatch to this agent.\n   - Start with "Use this agent when..."\n   - Include concrete triggering conditions\n   - Add 2-3 <example> blocks showing specific scenarios where the assistant uses the Agent tool to fully delegate the task\n   - Each <example> should show: user says X → assistant dispatches to this agent with the Agent tool, passing the complete task\n   - Write in the same language as the agent description (Chinese description → Chinese whenToUse)\n\n2. **systemPrompt**: The complete system prompt governing the agent\'s behavior.\n   - Define expert persona and role\n   - Specify workflow and methodology — end-to-end, from analysis through execution\n   - Establish clear behavioral boundaries and operational parameters\n   - Provide specific methodologies and best practices for task execution\n   - Define output format expectations when relevant\n   - Include self-verification steps\n   - Write in the same language as the agent description\n\nKey principles:\n- Be specific rather than generic — avoid vague instructions\n- Include concrete examples when they would clarify behavior\n- Balance comprehensiveness with clarity — every instruction should add value\n- Ensure the agent has enough context to handle variations of the core task\n- Build in quality assurance and self-correction mechanisms\n\nReturn ONLY a JSON object:\n{"whenToUse": "...", "systemPrompt": "..."}\n'


class AgentsManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_browser_runtime_restart(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            from openjiuwen.harness.tools import browser_move

            reset_runtimes = await _reset_requested_browser_runtime_if_available(
                browser_move, request.params or {}
            )
            result = browser_move.restart_local_browser_runtime_server()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"result": result, "reset_runtimes": reset_runtimes},
            )
        except Exception as e:
            logger.exception(
                "[AgentWebSocketServer] browser.runtime_restart failed: %s", e
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_agents_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from dataclasses import asdict as dataclass_asdict
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

        try:
            workspace_dir = (
                request.params.get("workspace_dir") if request.params else None
            )
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_agents_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from dataclasses import asdict as dataclass_asdict
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _generate_agent_with_llm(
        self, name: str, description: str
    ) -> tuple[str, str] | None:
        """调用 LLM 生成 agent 的 whenToUse 和 systemPrompt。

        Returns:
            (when_to_use, system_prompt) 或 None（生成失败时回退到模板）
        """
        host = self.host
        model = host._resolve_model(None)
        if model is None:
            logger.warning("[agents.create] no model available for LLM generation")
            return None
        from openjiuwen.core.foundation.llm.schema.message import UserMessage

        full_prompt = f"{_AGENT_CREATION_SYSTEM_PROMPT}\n\n---\n请为以下 agent 生成配置：\n\n名称: {name}\n描述: {description}\n\n返回 JSON 对象，包含 whenToUse 和 systemPrompt 两个字段。不要返回其他内容。"
        try:
            result = await model.invoke(
                [UserMessage(content=full_prompt)], max_tokens=2000, temperature=0.3
            )
            text = getattr(result, "content", None) or str(result)
        except Exception:
            logger.exception("[agents.create] LLM generation failed")
            return None

        # 解析 JSON 响应
        import json as _json
        import re as _re
        try:
            data = _json.loads(text.strip())
        except _json.JSONDecodeError:
            match = _re.search("\\{[\\s\\S]*\\}", text)
            if not match:
                logger.warning(
                    "[agents.create] no JSON found in LLM response: %s", text[:200]
                )
                return None
            try:
                data = _json.loads(match.group(0))
            except _json.JSONDecodeError:
                logger.warning("[agents.create] JSON parse failed: %s", text[:200])
                return None
        when_to_use = (data.get("whenToUse") or "").strip()
        system_prompt = (data.get("systemPrompt") or "").strip()
        if not when_to_use or not system_prompt:
            logger.warning("[agents.create] incomplete LLM response: %s", data)
            return None
        return (when_to_use, system_prompt)

    async def _handle_agents_create(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from dataclasses import asdict as dataclass_asdict
        from jiuwenswarm.server.runtime.agent_config_service import (
            AgentConfigService,
            CreateAgentParams,
        )

        try:
            params = dict(request.params or {})
            workspace_dir = params.pop("workspace_dir", None)
            generate = params.pop("generate", True)
            generated = False
            if generate:
                name = params.get("name", "")
                description = params.get("description", "")
                if name and description:
                    llm_result = await host._generate_agent_with_llm(name, description)
                    if llm_result:
                        params["when_to_use"] = llm_result[0]
                        params["prompt"] = llm_result[1]
                        generated = True
            p = CreateAgentParams(
                **{
                    k: v
                    for k, v in params.items()
                    if k in CreateAgentParams.__dataclass_fields__
                }
            )
            service = AgentConfigService(workspace_dir)
            agent = service.create_agent(p)
            applied = True
            reload_error = ""
            try:
                upsert_subagent_in_config(agent.name, enabled=True)
                await host._agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:
                applied = False
                reload_error = str(reload_exc)
                logger.warning(
                    "[AgentWebSocketServer] agents.create reload failed: %s", reload_exc
                )
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_agents_update(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from dataclasses import asdict as dataclass_asdict
        from jiuwenswarm.server.runtime.agent_config_service import (
            AgentConfigService,
            UpdateAgentParams,
        )

        try:
            params = dict(request.params or {})
            name = params.pop("name", "")
            workspace_dir = params.pop("workspace_dir", None)
            generate = params.pop("generate", False)
            generated = False
            if generate and name and params.get("description"):
                llm_result = await host._generate_agent_with_llm(
                    name, params["description"]
                )
                if llm_result:
                    params["when_to_use"] = llm_result[0]
                    params["prompt"] = llm_result[1]
                    generated = True
            p = UpdateAgentParams(
                **{
                    k: v
                    for k, v in params.items()
                    if k in UpdateAgentParams.__dataclass_fields__
                }
            )
            service = AgentConfigService(workspace_dir)
            agent = service.update_agent(name, p)
            applied = True
            reload_error = ""
            try:
                await host._agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:
                applied = False
                reload_error = str(reload_exc)
                logger.warning(
                    "[AgentWebSocketServer] agents.update reload failed: %s", reload_exc
                )
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_agents_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

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
                await host._agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:
                applied = False
                reload_error = str(reload_exc)
                logger.warning(
                    "[AgentWebSocketServer] agents.delete reload failed: %s", reload_exc
                )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "ok": ok,
                    "applied": applied,
                    "reload_error": reload_error or None,
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agents.delete failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_agents_set_enabled(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock, enabled: bool
    ) -> None:
        host = self.host
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

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
                await host._agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:
                applied = False
                reload_error = str(reload_exc)
                logger.warning(
                    "[AgentWebSocketServer] agents.%s reload failed: %s",
                    action,
                    reload_exc,
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_agents_tools_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_config_cache_clear(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            from jiuwenswarm.agents.harness.common.memory.config import (
                clear_config_cache,
            )

            clear_config_cache()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"cleared": True},
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] config.cache_clear failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_agent_prewarm_sync(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Reconcile background prewarming for the Gateway's live channels."""
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        raw_channels = params.get("enabled_channels")
        if not isinstance(raw_channels, list):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "enabled_channels must be a list",
                    "code": "BAD_REQUEST",
                },
            )
        else:
            stats = await host._agent_manager.sync_prewarm_channels(
                [str(channel) for channel in raw_channels],
                config=params.get("config"),
                env=params.get("env"),
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=stats,
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_agent_reload_config(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            params = request.params or {}
            config_payload = params.get("config")
            env_overrides = params.get("env")
            target_channel_id = (
                str(params.get("target_channel_id") or "").strip() or None
            )
            target_session_id = (
                str(params.get("target_session_id") or "").strip() or None
            )
            raw_reload_scopes = params.get("reload_scopes")
            reload_scopes = (
                {
                    str(scope)
                    for scope in raw_reload_scopes
                    if isinstance(scope, str) and scope
                }
                if isinstance(raw_reload_scopes, list)
                else set()
            )
            reload_kwargs = {}
            if target_channel_id:
                reload_kwargs["target_channel_id"] = target_channel_id
            if target_session_id:
                reload_kwargs["target_session_id"] = target_session_id
            if reload_scopes:
                reload_kwargs["reload_scopes"] = reload_scopes
            if isinstance(config_payload, dict) and "a4p" in config_payload:
                from jiuwenswarm.agents.harness.common.a4p.runtime import (
                    get_a4p_config,
                )
    
                config_payload = {**config_payload, "a4p": get_a4p_config()}
            agent_reload_scopes = {
                "model",
                "multimodal",
                "search",
                "team",
                "permissions",
                "agent_runtime",
            }
            should_reload_agents = not reload_scopes or bool(
                reload_scopes & agent_reload_scopes
            )
            should_warm_tokenizers = True
            if reload_scopes:
                should_warm_tokenizers = "model" in reload_scopes
            if should_warm_tokenizers:
                host._schedule_tokenizer_warmup(
                    config_payload
                    if isinstance(config_payload, dict)
                    else get_config(),
                    reason="model config change",
                )
            should_refresh_image_modality = should_warm_tokenizers
            if should_refresh_image_modality:
                host.schedule_image_modality_warmup(
                    reason="model config change", reset_cache=True
                )
            if should_refresh_image_modality:
                from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models
    
                host.spawn_background(
                    warm_zen_free_models(reason="agent.reload_config"), name="zen-models-reload"
                )
            if should_reload_agents:
                await host._agent_manager.reload_agents_config(
                    config_payload, env_overrides, **reload_kwargs
                )
                try:
                    from jiuwenswarm.agents.harness.team import (
                        stop_all_paused_team_session_runtimes_across_managers,
                    )
    
                    stopped = (
                        await stop_all_paused_team_session_runtimes_across_managers(
                            reason="agent.reload_config: "
                        )
                    )
                    if stopped:
                        logger.info(
                            "[AgentWebSocketServer] stopped paused team runtimes after agent.reload_config: count=%s request_id=%s reload_scopes=%s",
                            stopped,
                            request.request_id,
                            sorted(reload_scopes),
                        )
                except Exception as exc:
                    logger.warning(
                        "[AgentWebSocketServer] failed to stop paused team runtimes after agent.reload_config: %s",
                        exc,
                    )
            should_reload_proactive = not reload_scopes or bool(
                reload_scopes & {"model", "proactive", "agent_runtime"}
            )
            if host._proactive_engine is not None and should_reload_proactive:
                cfg = get_config()
                proactive_cfg = cfg.get("proactive_recommendation", {})
                host._proactive_engine.reload_config(proactive_cfg)
                try:
                    from jiuwenswarm.server.runtime.proactive_adapter import (
                        build_proactive_agent,
                    )
    
                    host._proactive_engine.rebuild_proactive_agent(
                        build_proactive_agent
                    )
                except Exception as exc:
                    logger.warning(
                        "[AgentWebSocketServer] proactive agent rebuild failed: %s", exc
                    )
            prewarmer = getattr(host, "_profile_prewarmer", None)
            if prewarmer is not None:
                try:
                    prewarmer.start()
                except Exception as exc:  # optional preparation never rejects a config reload
                    logger.warning("[Worker] profile prewarm reload skipped: %s", type(exc).__name__)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"reloaded": True},
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agent.reload_config failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)
