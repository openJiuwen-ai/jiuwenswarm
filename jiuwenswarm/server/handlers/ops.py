# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""运维请求：主动推荐、浏览器重启、清缓存、重载配置、预热、登录凭据。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)

_AGENT_RELOAD_SCOPES = {
    "model",
    "multimodal",
    "search",
    "team",
    "permissions",
    "agent_runtime",
}


async def _reset_active_browser_runtimes_if_available(browser_move: Any) -> int:
    """当前安装的浏览器工具支持时，重置正在用的浏览器。"""
    reset_runtimes = getattr(browser_move, "reset_active_browser_runtimes", None)
    if not callable(reset_runtimes):
        logger.warning(
            "[AgentWebSocketServer] installed openjiuwen does not support "
            "reset_active_browser_runtimes; restarting the local browser "
            "runtime server only"
        )
        return 0
    return await reset_runtimes()


async def _reset_requested_browser_runtime_if_available(
    browser_move: Any,
    params: dict[str, Any],
) -> int:
    """优先按指定身份重置；旧版本没有这个能力时，退回重置全部。"""
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


async def handle_proactive_tick(ctx: RequestContext) -> None:
    """定时器触发一次主动推荐。会遵守冷却时间和每天次数上限。"""
    request = ctx.request
    engine = ctx.services.proactive_engine
    if engine is None:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "ProactiveEngine not initialized"},
        )
    else:
        try:
            params = request.params or {}
            target_channel = params.get("target_channel")
            success = await engine.tick_now(target_channel=target_channel)
            status = "tick_executed" if success else "no_recommendation"
            last_tick = engine.last_tick_at
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
    await ctx.sink.send_response(resp)


async def handle_proactive_feedback(ctx: RequestContext) -> None:
    """记下用户对一条主动推荐的喜欢或不喜欢。"""
    request = ctx.request
    try:
        params = request.params or {}
        rec_id = params.get("rec_id")
        feedback_type = params.get("feedback_type")
        rec_type = str(params.get("rec_type") or params.get("proactive_type") or "")
        rec_target = str(params.get("rec_target") or params.get("proactive_target") or "")

        if not rec_id or feedback_type not in ("explicit_like", "explicit_dislike"):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": (
                        "Invalid params: rec_id and feedback_type "
                        "(explicit_like|explicit_dislike) required"
                    ),
                },
            )
        else:
            from jiuwenswarm.agents.harness.common.recommendation.feedback_collector import (
                RecMeta,
                record_explicit_feedback,
            )

            record_explicit_feedback(
                rec_id,
                feedback_type,
                meta=RecMeta(rec_type=rec_type, rec_target=rec_target),
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"status": "feedback_recorded", "rec_id": rec_id},
            )
    except Exception as e:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await ctx.sink.send_response(resp)


async def handle_browser_runtime_restart(ctx: RequestContext) -> None:
    """重启本机浏览器运行时。"""
    request = ctx.request
    try:
        from openjiuwen.harness.tools import browser_move

        reset_runtimes = await _reset_requested_browser_runtime_if_available(
            browser_move,
            request.params or {},
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
    await ctx.sink.send_response(resp)


async def handle_config_cache_clear(ctx: RequestContext) -> None:
    """清掉内存里的配置缓存。"""
    request = ctx.request
    try:
        from jiuwenswarm.agents.harness.common.memory.config import clear_config_cache

        clear_config_cache()
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
    await ctx.sink.send_response(resp)


async def handle_agent_prewarm_sync(ctx: RequestContext) -> None:
    """按网关当前开着的通道，对齐后台预热。"""
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
    await ctx.sink.send_response(resp)


async def handle_auth_credentials_update(ctx: RequestContext) -> None:
    """网关推回来的登录凭据：续期后的新令牌，或注销后的撤销。"""
    from jiuwenswarm.common.auth.login_credentials import apply_credential_update

    request = ctx.request
    applied = apply_credential_update(request.params)
    await ctx.sink.send_response(
        AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=applied,
            payload={"applied": applied},
        )
    )


async def handle_agent_reload_config(ctx: RequestContext) -> None:
    """配置变了之后，按范围重载 Agent、预热和主动推荐。"""
    from jiuwenswarm.common.config import get_config

    request = ctx.request
    try:
        params = request.params or {}
        config_payload = params.get("config")
        env_overrides = params.get("env")
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
        if isinstance(config_payload, dict) and "a4p" in config_payload:
            from jiuwenswarm.agents.harness.common.a4p_runtime import get_a4p_config

            # 网关带来的快照可能比本进程里刚改过的 a4p 配置旧，以本进程为准。
            config_payload = {**config_payload, "a4p": get_a4p_config()}
        should_reload_agents = not reload_scopes or bool(reload_scopes & _AGENT_RELOAD_SCOPES)

        should_warm_tokenizers = True
        if reload_scopes:
            should_warm_tokenizers = "model" in reload_scopes
        if should_warm_tokenizers:
            ctx.services.schedule_tokenizer_warmup(
                config_payload if isinstance(config_payload, dict) else get_config(),
                reason="model config change",
            )

        should_refresh_image_modality = should_warm_tokenizers
        if should_refresh_image_modality:
            ctx.services.schedule_image_modality_warmup(
                reason="model config change",
                reset_cache=True,
            )
        if should_refresh_image_modality:
            from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models

            asyncio.create_task(
                warm_zen_free_models(reason="agent.reload_config")
            )
        if should_reload_agents:
            await ctx.services.agent_manager.reload_agents_config(
                config_payload,
                env_overrides,
                **reload_kwargs,
            )
            try:
                from jiuwenswarm.agents.harness.team import (
                    stop_all_paused_team_session_runtimes_across_managers,
                )

                stopped = await stop_all_paused_team_session_runtimes_across_managers(
                    reason="agent.reload_config: ",
                )
                if stopped:
                    logger.info(
                        "[AgentWebSocketServer] stopped paused team runtimes after agent.reload_config: "
                        "count=%s request_id=%s reload_scopes=%s",
                        stopped,
                        request.request_id,
                        sorted(reload_scopes),
                    )
            except Exception as exc:  # noqa: BLE001 - cleanup must not reject config reload
                logger.warning(
                    "[AgentWebSocketServer] failed to stop paused team runtimes after agent.reload_config: %s",
                    exc,
                )

        should_reload_proactive = not reload_scopes or bool(
            reload_scopes & {"model", "proactive", "agent_runtime"}
        )
        engine = ctx.services.proactive_engine
        if engine is not None and should_reload_proactive:
            cfg = get_config()
            proactive_cfg = cfg.get("proactive_recommendation", {})
            engine.reload_config(proactive_cfg)
            try:
                from jiuwenswarm.server.runtime.proactive_adapter import build_proactive_agent

                engine.rebuild_proactive_agent(build_proactive_agent)
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
    await ctx.sink.send_response(resp)
