"""Request-local identity and MEMORYSTATE resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .runtime_state import read_memory_state


@dataclass(frozen=True)
class CeliaRuntimeContext:
    tenant_id: str
    user_id: str
    scope_id: str
    conversation_id: str
    session_id: str
    memory_state: bool
    trace_id: str
    request_scope: dict[str, str] = field(default_factory=dict)

    @property
    def tool_session_id(self) -> str:
        return f"tools-{self.user_id}"

    @property
    def store_key(self) -> str:
        return f"{self.tenant_id}:{self.user_id}:{self.conversation_id}"

    @property
    def fixed_context_key(self) -> str:
        return f"{self.tenant_id}:{self.user_id}"


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def resolve_runtime_context(
    *,
    default_tenant_id: str,
    default_user_id: str,
    default_scope_id: str,
    default_session_id: str = "__default__",
    default_request_scope: Mapping[str, str] | None = None,
    default_metadata: Mapping[str, Any] | None = None,
    explicit: Mapping[str, Any] | None = None,
) -> CeliaRuntimeContext:
    explicit = explicit or {}
    request = None
    try:
        from jiuwenswarm.server.request_context import get_current_agent_request

        request = get_current_agent_request()
    except Exception:
        request = None

    metadata = {**dict(default_metadata or {}), **dict(getattr(request, "metadata", None) or {})}
    permission = getattr(request, "permission_context", None)
    request_session = _first_text(getattr(request, "session_id", None))
    request_chat = _first_text(getattr(request, "chat_id", None))
    request_id = _first_text(getattr(request, "request_id", None))

    user_id = _first_text(
        metadata.get("celia_user_id"),
        metadata.get("user_id"),
        metadata.get("xiaoyi_user_id"),
        metadata.get("triggering_user_id"),
        getattr(permission, "triggering_user_id", None),
        getattr(permission, "principal_user_id", None),
        explicit.get("user_id"),
        default_user_id,
    ) or "__default__"
    tenant_id = _first_text(
        metadata.get("celia_tenant_id"),
        explicit.get("tenant_id"),
        default_tenant_id,
    ) or "default"
    scope_id = _first_text(explicit.get("scope_id"), default_scope_id) or "user"
    session_id = _first_text(
        metadata.get("xiaoyi_session_id"),
        explicit.get("session_id"),
        request_session,
        default_session_id,
    ) or "__default__"
    conversation_id = _first_text(
        metadata.get("conversation_id"),
        metadata.get("xiaoyi_conversation_id"),
        request_chat,
        request_session,
        explicit.get("conversation_id"),
        default_session_id,
        user_id,
    )
    # OpenClaw compatibility: .xiaoyiruntime is the only MEMORYSTATE source.
    # Request parameters and process environment must never override it.
    state = read_memory_state(str(explicit.get("runtime_state_path") or ""))
    request_scope = dict(default_request_scope or {})
    incoming_scope = metadata.get("celia_request_scope")
    if isinstance(incoming_scope, Mapping):
        request_scope.update(incoming_scope)
    # Preserve tenant isolation when replacing the removed tenant_id wire field.
    request_scope["tenantId"] = tenant_id
    return CeliaRuntimeContext(
        tenant_id=tenant_id,
        user_id=user_id,
        scope_id=scope_id,
        conversation_id=conversation_id,
        session_id=session_id,
        memory_state=state,
        trace_id=_first_text(metadata.get("trace_id"), metadata.get("_trace_id"), request_id) or "jiuwen-celia",
        request_scope=request_scope,
    )
