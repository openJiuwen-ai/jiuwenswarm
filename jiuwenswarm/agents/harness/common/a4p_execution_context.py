# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host execution context used by the JiuwenSwarm A4P integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class AuthorizerRoute:
    """Logical Web authorizer route; ws_id is only a delivery hint."""

    session_id: str
    app_id: str
    agent_ref_mode: str
    agent_ref_id: str
    ws_id: str = ""

    @property
    def logical_key(self) -> tuple[str, str, str, str]:
        return (
            self.session_id,
            self.app_id,
            self.agent_ref_mode,
            self.agent_ref_id,
        )


@dataclass(frozen=True)
class AuthorizationExecutionContext:
    request_id: str
    session_id: str
    channel_id: str
    agent_id: str
    metadata: dict[str, Any]
    authorizer_route: AuthorizerRoute | None = None
    user_id: str = ""

    @property
    def is_interactive_web(self) -> bool:
        return self.channel_id == "web" and self.authorizer_route is not None


@dataclass(frozen=True)
class A4PAuthorizationAttempt:
    request_id: str
    status: Literal["not_requested", "pending", "approved", "rejected", "failed"]
    error: str = ""


class AuthorizationExecutionContextStore:
    """Cross-task session registry for long-lived DeepAgent schedulers."""

    def __init__(self) -> None:
        self._contexts: dict[str, AuthorizationExecutionContext] = {}
        self._attempts: dict[str, A4PAuthorizationAttempt] = {}

    def activate(
        self,
        context: AuthorizationExecutionContext,
    ) -> AuthorizationExecutionContext:
        current = self._contexts.get(context.session_id)
        if current is not None:
            return current
        self._contexts[context.session_id] = context
        self._attempts[context.session_id] = A4PAuthorizationAttempt(
            request_id=context.request_id,
            status="not_requested",
        )
        return context

    def get(self, session_id: str | None) -> AuthorizationExecutionContext | None:
        return self._contexts.get(str(session_id or "").strip())

    def release(self, session_id: str | None, request_id: str | None) -> None:
        sid = str(session_id or "").strip()
        current = self._contexts.get(sid)
        if current is None or current.request_id != str(request_id or "").strip():
            return
        self._contexts.pop(sid, None)
        attempt = self._attempts.get(sid)
        if attempt is not None and attempt.request_id == current.request_id:
            self._attempts.pop(sid, None)

    def set_attempt(
        self,
        session_id: str,
        *,
        request_id: str,
        status: Literal["not_requested", "pending", "approved", "rejected", "failed"],
        error: str = "",
    ) -> None:
        self._attempts[session_id] = A4PAuthorizationAttempt(
            request_id=request_id,
            status=status,
            error=error,
        )

    def get_attempt(self, session_id: str | None) -> A4PAuthorizationAttempt | None:
        return self._attempts.get(str(session_id or "").strip())

    def clear(self) -> None:
        self._contexts.clear()
        self._attempts.clear()


AUTHORIZATION_EXECUTION_CONTEXTS = AuthorizationExecutionContextStore()


def _agent_ref_parts(agent_ref: Any, mode: str) -> tuple[str, str]:
    if isinstance(agent_ref, dict):
        return (
            str(agent_ref.get("mode") or mode or "agent").strip() or "agent",
            str(agent_ref.get("id") or "default").strip() or "default",
        )
    return (
        str(getattr(agent_ref, "mode", None) or mode or "agent").strip() or "agent",
        str(getattr(agent_ref, "id", None) or "default").strip() or "default",
    )


def build_authorization_execution_context(
    request: Any,
    *,
    agent_id: str,
    mode: str,
) -> AuthorizationExecutionContext:
    metadata = dict(getattr(request, "metadata", None) or {})
    session_id = str(getattr(request, "session_id", None) or "").strip()
    channel_id = str(getattr(request, "channel_id", None) or "").strip()
    app_id = str(metadata.get("app_id") or "default").strip() or "default"
    ref_mode, ref_id = _agent_ref_parts(
        getattr(request, "agent_ref", None) or metadata.get("agent_ref"),
        mode,
    )
    ws_id = str(metadata.get("ws_id") or "").strip()
    params = (
        getattr(request, "params", None)
        if isinstance(getattr(request, "params", None), dict)
        else {}
    )
    req_method = getattr(request, "req_method", None)
    req_method_value = str(getattr(req_method, "value", req_method) or "").strip()
    is_background_request = (
        req_method_value in {"command.goal", "proactive.tick"}
        or params.get("attach_goal") is True
    )
    route = None
    if channel_id == "web" and session_id and not is_background_request:
        route = AuthorizerRoute(
            session_id=session_id,
            app_id=app_id,
            agent_ref_mode=ref_mode,
            agent_ref_id=ref_id,
            ws_id=ws_id,
        )
    return AuthorizationExecutionContext(
        request_id=str(getattr(request, "request_id", None) or "").strip(),
        session_id=session_id,
        channel_id=channel_id,
        agent_id=str(agent_id or "").strip(),
        metadata=metadata,
        authorizer_route=route,
        user_id=str(getattr(request, "user_id", None) or "").strip(),
    )


def authorizer_route_from_request(request: Any) -> AuthorizerRoute | None:
    context = build_authorization_execution_context(
        request,
        agent_id="",
        mode=str((getattr(request, "params", None) or {}).get("mode") or "agent"),
    )
    return context.authorizer_route


__all__ = [
    "A4PAuthorizationAttempt",
    "AUTHORIZATION_EXECUTION_CONTEXTS",
    "AuthorizationExecutionContext",
    "AuthorizationExecutionContextStore",
    "AuthorizerRoute",
    "authorizer_route_from_request",
    "build_authorization_execution_context",
]
