# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent tools for discovering and messaging other product Sessions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageSource,
    SessionMessagingError,
)


@dataclass(slots=True)
class SessionMessagingRoute:
    """Trusted request-local source inherited by parallel tool Tasks."""

    session_id: str
    request_id: str
    user_id: str = ""
    chain_id: str = ""
    parent_message_id: str = ""
    hop_count: int = 0

    def source_for_call(
        self,
        target_session_id: str,
        message: str,
        *,
        tool_call_id: str = "",
    ) -> SessionMessageSource:
        if not tool_call_id:
            raise SessionMessagingError(
                "MISSING_TOOL_CALL_ID",
                "session_send_message requires a host-provided tool call id",
            )
        idempotency_key = hashlib.sha256(
            f"{self.session_id}\0{self.request_id}\0{tool_call_id}".encode("utf-8")
        ).hexdigest()
        return SessionMessageSource(
            session_id=self.session_id,
            request_id=self.request_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            user_id=self.user_id,
            chain_id=self.chain_id,
            parent_message_id=self.parent_message_id,
            hop_count=self.hop_count,
        )

    def source_for_list(self) -> SessionMessageSource:
        return SessionMessageSource(
            session_id=self.session_id,
            request_id=self.request_id,
            tool_call_id="session_list",
            idempotency_key="",
            user_id=self.user_id,
            chain_id=self.chain_id,
            parent_message_id=self.parent_message_id,
            hop_count=self.hop_count,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "user_id": self.user_id,
            "chain_id": self.chain_id,
            "parent_message_id": self.parent_message_id,
            "hop_count": self.hop_count,
        }


_SESSION_MESSAGING_ROUTE: ContextVar[SessionMessagingRoute | None] = ContextVar(
    "jiuwenswarm_session_messaging_route", default=None
)
_SESSION_MESSAGING_TOOL_CALL_ID: ContextVar[str] = ContextVar(
    "jiuwenswarm_session_messaging_tool_call_id", default=""
)
SESSION_MESSAGING_ROUTE_EXTRA_KEY = "jiuwenswarm.session_messaging_route.v1"
_SESSION_MESSAGING_TOOL_NAMES = frozenset(
    {
        "session_list",
        "session_send_message",
        "session_message_list",
        "session_message_resolve",
    }
)


def bind_session_messaging_route(
    *,
    session_id: str | None,
    request_id: str | None,
    user_id: str | None,
    cross_session: dict[str, Any] | None = None,
) -> Token[SessionMessagingRoute | None]:
    inherited = cross_session if isinstance(cross_session, dict) else {}
    try:
        hop_count = max(0, int(inherited.get("hop_count") or 0))
    except (TypeError, ValueError):
        hop_count = 0
    route = SessionMessagingRoute(
        session_id=str(session_id or "").strip(),
        request_id=str(request_id or "").strip(),
        user_id=str(user_id or "").strip(),
        chain_id=str(inherited.get("chain_id") or "").strip(),
        parent_message_id=str(inherited.get("message_id") or "").strip(),
        hop_count=hop_count,
    )
    return _SESSION_MESSAGING_ROUTE.set(route)


def reset_session_messaging_route(token: Token[SessionMessagingRoute | None]) -> None:
    _SESSION_MESSAGING_ROUTE.reset(token)


def current_session_messaging_route() -> SessionMessagingRoute | None:
    return _SESSION_MESSAGING_ROUTE.get()


def session_messaging_route_context(request: Any) -> dict[str, Any] | None:
    """Resolve Host-authenticated routing without changing prompt provenance."""

    params = (
        request.params if isinstance(getattr(request, "params", None), dict) else {}
    )
    cross_session = params.get(SESSION_MESSAGE_INTERNAL_KEY)
    if isinstance(cross_session, dict):
        return cross_session
    trusted = getattr(request, "trusted_session_message_route", None)
    return trusted if isinstance(trusted, dict) else None


def with_session_messaging_route(
    inputs: dict[str, Any], route: SessionMessagingRoute | None
) -> dict[str, Any]:
    """Carry an immutable request route through the DeepAgent supervisor."""

    if route is None:
        return inputs
    updated = dict(inputs)
    raw_run = updated.get("run")
    run = dict(raw_run) if isinstance(raw_run, Mapping) else {}
    raw_context = run.get("context")
    context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
    raw_extra = context.get("extra")
    extra = dict(raw_extra) if isinstance(raw_extra, Mapping) else {}
    extra[SESSION_MESSAGING_ROUTE_EXTRA_KEY] = route.to_wire()
    context["extra"] = extra
    run["context"] = context
    run.setdefault("kind", "normal")
    updated["run"] = run
    return updated


class SessionMessagingRouteRail(DeepAgentRail):
    """Bind the route belonging to the exact tool invocation task."""

    priority = 97
    _ROUTE_TOKEN_ATTR = "_jiuwenswarm_session_message_route_token"
    _CALL_TOKEN_ATTR = "_jiuwenswarm_session_message_call_token"

    @staticmethod
    def _tool_name(inputs: ToolCallInputs) -> str:
        tool_call = inputs.tool_call
        return str(
            inputs.tool_name
            or (
                tool_call.get("name")
                if isinstance(tool_call, Mapping)
                else getattr(tool_call, "name", "")
            )
            or ""
        ).strip()

    @staticmethod
    def _tool_call_id(inputs: ToolCallInputs) -> str:
        tool_call = inputs.tool_call
        return str(
            (
                tool_call.get("id")
                if isinstance(tool_call, Mapping)
                else getattr(tool_call, "id", "")
            )
            or ""
        ).strip()

    @staticmethod
    def _route(ctx: AgentCallbackContext) -> SessionMessagingRoute | None:
        run_context = ctx.extra.get("run_context")
        if isinstance(run_context, Mapping):
            extra = run_context.get("extra")
        else:
            extra = getattr(run_context, "extra", None)
        if not isinstance(extra, Mapping):
            return None
        raw = extra.get(SESSION_MESSAGING_ROUTE_EXTRA_KEY)
        if not isinstance(raw, Mapping):
            return None
        try:
            return SessionMessagingRoute(
                session_id=str(raw.get("session_id") or "").strip(),
                request_id=str(raw.get("request_id") or "").strip(),
                user_id=str(raw.get("user_id") or "").strip(),
                chain_id=str(raw.get("chain_id") or "").strip(),
                parent_message_id=str(raw.get("parent_message_id") or "").strip(),
                hop_count=max(0, int(raw.get("hop_count") or 0)),
            )
        except (TypeError, ValueError):
            return None

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        if not isinstance(inputs, ToolCallInputs):
            return
        if self._tool_name(inputs) not in _SESSION_MESSAGING_TOOL_NAMES:
            return
        route = self._route(ctx)
        tool_call_id = self._tool_call_id(inputs)
        if route is None or not route.session_id or not route.request_id:
            return
        setattr(ctx, self._ROUTE_TOKEN_ATTR, _SESSION_MESSAGING_ROUTE.set(route))
        setattr(
            ctx,
            self._CALL_TOKEN_ATTR,
            _SESSION_MESSAGING_TOOL_CALL_ID.set(tool_call_id),
        )

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        call_token = getattr(ctx, self._CALL_TOKEN_ATTR, None)
        if call_token is not None:
            _SESSION_MESSAGING_TOOL_CALL_ID.reset(call_token)
            setattr(ctx, self._CALL_TOKEN_ATTR, None)
        route_token = getattr(ctx, self._ROUTE_TOKEN_ATTR, None)
        if route_token is not None:
            _SESSION_MESSAGING_ROUTE.reset(route_token)
            setattr(ctx, self._ROUTE_TOKEN_ATTR, None)

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        await self.after_tool_call(ctx)


class SessionMessagingToolkit:
    """Stable tools whose request ownership is resolved at invocation time."""

    def __init__(self, service: Any | None = None) -> None:
        self._service = service
        self._tools: list[Tool] | None = None

    def set_service(self, service: Any | None) -> None:
        """Refresh the host service after an AgentServer Runtime rebuild."""

        self._service = service

    def _service_and_route(self) -> tuple[Any, SessionMessagingRoute]:
        runtime = get_current_runtime()
        route = current_session_messaging_route()
        service = self._service
        if service is None and runtime is not None:
            service = getattr(runtime, "session_message_service", None)
        route_ready = route is not None and route.session_id and route.request_id
        if service is None or not route_ready:
            raise SessionMessagingError(
                "HOST_CAPABILITY_UNAVAILABLE",
                "cross-Session messaging requires a resident AgentServer",
            )
        return service, route

    async def list_sessions(
        self,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        try:
            service, route = self._service_and_route()
            return await service.list_targets(
                route.source_for_list(), query=query, limit=limit, offset=offset
            )
        except SessionMessagingError as exc:
            return {"accepted": False, "code": exc.code, "error": str(exc)}

    async def send_message(
        self,
        target_session_id: str,
        message: str,
    ) -> dict[str, Any]:
        try:
            service, route = self._service_and_route()
            source = route.source_for_call(
                target_session_id,
                message,
                tool_call_id=_SESSION_MESSAGING_TOOL_CALL_ID.get(),
            )
            return await service.send_message(
                source,
                target_session_id=target_session_id,
                message=message,
            )
        except SessionMessagingError as exc:
            return {"accepted": False, "code": exc.code, "error": str(exc)}

    async def list_messages(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        try:
            service, route = self._service_and_route()
            return await service.list_messages(
                route.source_for_list(), limit=limit, offset=offset
            )
        except SessionMessagingError as exc:
            return {"accepted": False, "code": exc.code, "error": str(exc)}

    async def resolve_message(
        self,
        message_id: str,
        resolution: str,
    ) -> dict[str, Any]:
        try:
            service, route = self._service_and_route()
            return await service.resolve_unknown(
                route.source_for_list(),
                message_id=message_id,
                resolution=resolution,
            )
        except SessionMessagingError as exc:
            return {"accepted": False, "code": exc.code, "error": str(exc)}

    def get_tools(self) -> list[Tool]:
        if self._tools is None:
            self._tools = [
                LocalFunction(
                    card=ToolCard(
                        name="session_list",
                        description=(
                            "列出当前用户可接收 Agent 消息的其他持久化会话。"
                            "返回会话 ID、标题、模式、运行状态和待处理消息数。"
                        ),
                        input_params={
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "可选的会话标题关键词。",
                                },
                                "limit": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 50,
                                    "default": 20,
                                },
                                "offset": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "default": 0,
                                },
                            },
                        },
                    ),
                    func=self.list_sessions,
                ),
                LocalFunction(
                    card=ToolCard(
                        name="session_send_message",
                        description=(
                            "向同一用户的另一个持久化会话发送文本，让目标 Agent 异步处理。"
                            "成功只表示消息已保存并排队，不表示目标已经完成。"
                        ),
                        input_params={
                            "type": "object",
                            "properties": {
                                "target_session_id": {
                                    "type": "string",
                                    "description": "session_list 返回的目标会话 ID。",
                                },
                                "message": {
                                    "type": "string",
                                    "description": "交给目标 Agent 处理的完整文本。",
                                },
                            },
                            "required": ["target_session_id", "message"],
                        },
                    ),
                    func=self.send_message,
                ),
                LocalFunction(
                    card=ToolCard(
                        name="session_message_list",
                        description=(
                            "列出当前会话发送或接收的跨会话消息及其状态。"
                            "当状态为 unknown 时，需要用户决定如何解析。"
                        ),
                        input_params={
                            "type": "object",
                            "properties": {
                                "limit": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 100,
                                    "default": 50,
                                },
                                "offset": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "default": 0,
                                },
                            },
                        },
                    ),
                    func=self.list_messages,
                ),
                LocalFunction(
                    card=ToolCard(
                        name="session_message_resolve",
                        description=(
                            "按用户明确判断解析一条 unknown 跨会话消息。"
                            "succeeded 表示确认此前已完成；cancelled 表示不重放并取消。"
                        ),
                        input_params={
                            "type": "object",
                            "properties": {
                                "message_id": {"type": "string"},
                                "resolution": {
                                    "type": "string",
                                    "enum": ["succeeded", "cancelled"],
                                },
                            },
                            "required": ["message_id", "resolution"],
                        },
                    ),
                    func=self.resolve_message,
                ),
            ]
        return list(self._tools)


__all__ = [
    "SessionMessagingRoute",
    "SessionMessagingRouteRail",
    "SessionMessagingToolkit",
    "SESSION_MESSAGING_ROUTE_EXTRA_KEY",
    "bind_session_messaging_route",
    "current_session_messaging_route",
    "reset_session_messaging_route",
    "session_messaging_route_context",
    "with_session_messaging_route",
]
