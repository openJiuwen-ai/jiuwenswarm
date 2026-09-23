# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""运维域 handler"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

logger = logging.getLogger(__name__)


async def _reset_active_browser_runtimes_if_available(browser_move: Any) -> int:
    """Reset active browser runtimes when supported by the installed SDK."""
    reset_runtimes = getattr(
        browser_move,
        "reset_active_browser_runtimes",
        None,
    )
    if not callable(reset_runtimes):
        logger.warning(
            "[AgentWebSocketServer] installed openjiuwen does not support "
            "reset_active_browser_runtimes; restarting the local browser "
            "runtime server only"
        )
        return 0
    return await reset_runtimes()


async def handle_proactive_tick(ctx: RequestContext) -> None:
    """Handle proactive.tick request from CronScheduler.

    This is called by Gateway's CronScheduler to trigger a recommendation tick.
    Respects cooldown and daily limits.
    """
    request = ctx.request
    if ctx.services.proactive_engine is None:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "ProactiveEngine not initialized"},
        )
    else:
        try:
            # Extract target_channel from params
            params = request.params or {}
            target_channel = params.get("target_channel")

            # Run the tick (respects cooldown and daily limits)
            success = await ctx.services.proactive_engine.tick_now(target_channel=target_channel)

            status = "tick_executed" if success else "no_recommendation"
            last_tick = ctx.services.proactive_engine.last_tick_at
            if last_tick > 0:
                status = f"{status} (last_tick_at={last_tick:.0f})"

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"status": status, "success": success},
            )
        except Exception as e:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_browser_runtime_restart(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        from openjiuwen.harness.tools import browser_move

        reset_runtimes = await _reset_active_browser_runtimes_if_available(
            browser_move
        )
        result = browser_move.restart_local_browser_runtime_server()
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "result": result,
                "reset_runtimes": reset_runtimes,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] browser.runtime_restart failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_config_cache_clear(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        from jiuwenswarm.agents.harness.common.memory.config import (
            clear_config_cache,
            clear_embed_config_db_cache,
        )

        clear_config_cache()
        clear_embed_config_db_cache()
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"cleared": True},
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] config.cache_clear failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_agent_reload_config(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        params = request.params or {}
        config_payload = params.get("config")
        env_overrides = params.get("env")

        # 触发 AGENT_RELOAD_CONFIG hook（扩展可原地修改 config/env 后再生效）
        try:
            from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
            from jiuwenswarm.extensions.hooks_context import AgentReloadConfigHookContext
            from jiuwenswarm.extensions.registry import ExtensionRegistry

            hook_ctx = AgentReloadConfigHookContext(
                request_id=request.request_id,
                channel_id=request.channel_id,
                config=config_payload,
                env=env_overrides,
            )
            await ExtensionRegistry.get_instance().trigger(
                AgentServerHookEvents.AGENT_RELOAD_CONFIG, hook_ctx
            )
            # 允许扩展修改配置
            config_payload = hook_ctx.config
            env_overrides = hook_ctx.env
        except RuntimeError:
            # ExtensionRegistry 未初始化，跳过
            pass

        target_channel_id = str(params.get("target_channel_id") or "").strip() or None
        target_session_id = str(params.get("target_session_id") or "").strip() or None
        raw_reload_scopes = params.get("reload_scopes")
        reload_scopes = {
            str(scope)
            for scope in raw_reload_scopes
            if isinstance(scope, str) and scope
        } if isinstance(raw_reload_scopes, list) else set()

        reload_kwargs = {}
        if target_channel_id:
            reload_kwargs["target_channel_id"] = target_channel_id
        if target_session_id:
            reload_kwargs["target_session_id"] = target_session_id
        if reload_scopes:
            reload_kwargs["reload_scopes"] = reload_scopes
        agent_reload_scopes = {"model", "team", "permissions", "agent_runtime"}
        should_reload_agents = not reload_scopes or bool(reload_scopes & agent_reload_scopes)
        if should_reload_agents:
            if request.channel_id == "officeclaw":
                guard = TenantAgentPool.require_officeclaw_agent(request)
                if guard is not None:
                    wire = encode_agent_response_for_wire(
                        guard, response_id=request.request_id
                    )
                    await ctx.sink.send_wire(wire)
                    return

            raw_agent = getattr(request, "agent_id", None)
            agent_id, service_id, _workspace_key = TenantAgentPool.extract_ids(request)
            if (
                request.channel_id == "officeclaw"
                or (raw_agent is not None and str(raw_agent).strip())
            ):
                await ctx.services.tenant_pool().reload_tenant_config(
                    agent_id,
                    service_id,
                    config=config_payload,
                    env=env_overrides,
                )
            else:
                await ctx.services.agent_manager.reload_agents_config(
                    config_payload,
                    env_overrides,
                    **reload_kwargs,
                )

        # Hot-reload ProactiveEngine config if available
        should_reload_proactive = not reload_scopes or bool(reload_scopes & {"model", "proactive", "agent_runtime"})
        if ctx.services.proactive_engine is not None and should_reload_proactive:
            cfg = get_config()
            proactive_cfg = cfg.get("proactive_recommendation", {})
            ctx.services.proactive_engine.reload_config(proactive_cfg)
            # 重建 proactive agent——它启动时建一次，模型配置固化在实例里。
            # 用户改模型后主 agent 会热更新，但 proactive agent 不在主 agent
            # 链路里，不重建会继续用旧模型（可能已失效/欠费）。
            try:
                from jiuwenswarm.server.runtime.proactive_adapter import build_proactive_agent
                ctx.services.proactive_engine.rebuild_proactive_agent(build_proactive_agent)
            except Exception as exc:
                logger.warning("[AgentWebSocketServer] proactive agent rebuild failed: %s", exc)

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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_sync_agents_configs(ctx: RequestContext) -> None:
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    try:
        payload = await ctx.services.tenant_pool().sync_agents_configs(params)
        agents = payload.get("agents") if isinstance(payload, dict) else []
        all_ok = (
            isinstance(agents, list)
            and all(isinstance(item, dict) and item.get("ok") for item in agents)
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=all_ok,
            payload={
                "event_type": EventType.SYNC_AGENTS_CONFIGS_RESULT.value,
                **payload,
            },
        )
    except ValueError as exc:
        logger.warning(
            "[AgentWebSocketServer] sync_agents_configs rejected: %s", exc
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "event_type": EventType.SYNC_AGENTS_CONFIGS_RESULT.value,
                "error": str(exc),
            },
        )
    except Exception as exc:
        logger.exception(
            "[AgentWebSocketServer] sync_agents_configs failed: %s", exc
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "event_type": EventType.SYNC_AGENTS_CONFIGS_RESULT.value,
                "error": str(exc),
            },
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_agent_prewarm_sync(ctx: RequestContext) -> None:
    """Reconcile background prewarming for the Gateway's live channels."""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    raw_channels = params.get("enabled_channels")
    if not isinstance(raw_channels, list):
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "enabled_channels must be a list", "code": "BAD_REQUEST"},
        )
    else:
        stats = await ctx.services.agent_manager.sync_prewarm_channels(
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
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)
