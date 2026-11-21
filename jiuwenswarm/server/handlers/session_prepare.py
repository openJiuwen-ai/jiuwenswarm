# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""session.prepare：首条消息路由后，按随后 chat.send 的身份提前构建会话实例。"""
# 只读解析模式与工作目录，立即应答，构建放到后台；不调模型、不写元数据与历史。

from __future__ import annotations

import logging

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._default import (
    resolve_chat_turn_routing,
    resolve_readonly_project_dir,
)
from jiuwenswarm.server.handlers._shared import resolve_request_project_dir
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

logger = logging.getLogger(__name__)

_PREPARE_CHANNEL = "officeclaw"
_PREPARABLE_MODES = frozenset({"agent", "flash"})


class _InvalidPrepare(Exception):
    def __init__(self, code: str, error: str) -> None:
        super().__init__(error)
        self.code = code
        self.error = error


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate(request: AgentRequest) -> None:
    if request.channel_id != _PREPARE_CHANNEL:
        raise _InvalidPrepare("INVALID_PARAMS", f"session.prepare only supports channel {_PREPARE_CHANNEL}")
    if not _non_empty_str(request.session_id):
        raise _InvalidPrepare("INVALID_PARAMS", "session_id is required")
    if not _non_empty_str(request.agent_id):
        raise _InvalidPrepare("INVALID_PARAMS", "agent_id is required")
    params = request.params
    if not isinstance(params, dict) or not _non_empty_str(params.get("mode")):
        raise _InvalidPrepare("INVALID_PARAMS", "params.mode is required")
    if request.metadata is not None and not isinstance(request.metadata, dict):
        raise _InvalidPrepare("INVALID_PARAMS", "metadata must be an object")


def _status(status: str, *, accepted: bool = False) -> dict:
    return {"accepted": accepted, "status": status}


def _prepare(ctx: RequestContext, request: AgentRequest) -> dict:
    _validate(request)
    if not TenantAgentPool.session_prepare_enabled():
        return _status("disabled")
    guard = TenantAgentPool.require_officeclaw_agent(request)
    if guard is not None:
        guard_payload = guard.payload or {}
        if guard_payload.get("code") == "tenant_not_registered":
            return _status("unknown_agent")
        raise _InvalidPrepare(
            str(guard_payload.get("code") or "INVALID_PARAMS"),
            str(guard_payload.get("error") or ""),
        )

    try:
        routing = resolve_chat_turn_routing(request)
    except ValueError as exc:
        logger.info(
            "[session.prepare] mode not supported: session_id=%s error=%s",
            request.session_id,
            exc,
        )
        return _status("mode_not_supported")
    adapter_mode = request.resolved_adapter_mode
    if (
        routing.mode not in _PREPARABLE_MODES
        or routing.sub_mode is not None
        or adapter_mode != routing.mode
    ):
        return _status("mode_not_supported")

    pool = ctx.services.tenant_pool()
    sid = routing.session_id
    if pool.session_prewarm_running(sid):
        return _status("already_running", accepted=True)
    if pool.find_agent_for_session_nowait(sid) is not None:
        return _status("already_ready")

    # 与对话路径同口径，否则两边算出的工作目录不同、准备好的实例对话用不上。
    project_dir = resolve_readonly_project_dir(
        request,
        routing.sessions_root,
        resolve_request_project_dir(
            request, include_legacy_fallbacks=routing.mode not in {"agent", "code"},
        ),
        prefer_locked=True,
    )
    project_dir = project_dir.strip() if isinstance(project_dir, str) and project_dir.strip() else None
    if project_dir:
        request.params["project_dir"] = project_dir
        request.metadata = {**(request.metadata or {}), "project_dir": project_dir}

    from jiuwenswarm.server.agent_ws_server import (
        ensure_interface_deep_and_checkpointer,
    )

    status = pool.prepare_session_in_background(
        request=request,
        mode=routing.canonical_mode or routing.mode,
        adapter_mode=adapter_mode,
        project_dir=project_dir,
        before_build=ensure_interface_deep_and_checkpointer,
    )
    if status == "scheduled":
        logger.info(
            "[session.prepare] scheduled: session_id=%s mode=%s adapter_mode=%s",
            sid,
            routing.canonical_mode,
            adapter_mode,
        )
    return _status(status, accepted=status != "queue_full")


async def handle_session_prepare(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        payload = {"session_id": request.session_id, **_prepare(ctx, request)}
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except _InvalidPrepare as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": exc.error, "code": exc.code},
        )
    except Exception as exc:
        logger.exception("[session.prepare] failed: session_id=%s", request.session_id)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "INTERNAL_ERROR"},
        )
    await ctx.sink.send_wire(encode_agent_response_for_wire(resp, response_id=request.request_id))
