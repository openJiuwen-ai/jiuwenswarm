# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Authorization management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
from jiuwenswarm.common.schema.message import ReqMethod
import asyncio
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.hooks_config import load_hooks_config

logger = logging.getLogger(__name__)


class AuthorizationManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_permissions_config(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 permissions.* E2A 请求（与 Web ``register_method`` 同名 method）。"""
        host = self.host
        from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
            dispatch_permissions_config_request,
        )

        resp = dispatch_permissions_config_request(request)
        read_only_methods = {
            ReqMethod.PERMISSIONS_TOOLS_GET,
            ReqMethod.PERMISSIONS_RULES_GET,
            ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET,
        }
        if resp.ok and request.req_method not in read_only_methods:
            host._agent_manager.schedule_permissions_reload(get_config())
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_a4p_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle a4p.* requests forwarded from the Web User Authorizer."""
        host = self.host
        from jiuwenswarm.agents.harness.common.a4p.rpc import dispatch_a4p_request
    
        resp = await dispatch_a4p_request(request)
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_auth_credentials_update(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Gateway 推回的登录模型凭据：续期后的新 token，或会话注销后的撤销。"""
        host = self.host
        from jiuwenswarm.common.auth.login_credentials import apply_credential_update
    
        applied = apply_credential_update(request.params)
        proactive = getattr(host, "_proactive_engine", None)
        if applied and proactive is not None:
            from jiuwenswarm.server.runtime.proactive_adapter import (
                build_proactive_agent,
            )
    
            proactive.rebuild_proactive_agent(build_proactive_agent)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=applied,
            payload={"applied": applied},
        )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_hooks_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """获取当前 hooks 配置（供 TUI /hooks 命令浏览）."""
        host = self.host
        try:
            config_base = get_config()
            hooks_config = load_hooks_config(config_base)
            summary = hooks_config.get_event_summary()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "events": summary,
                    "disable_all_hooks": hooks_config.disable_all_hooks,
                    "source": "config.yaml",
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] hooks.list failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)
