"""Provider-neutral E2A memory management dispatch, without Agent/MCP startup."""

from typing import Any

from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_response_for_wire, encode_request_error_wire,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.extensions.hooks_context import MemoryProfileHookContext


MEMORY_PROFILE_EVENTS = {
    ReqMethod.MEMORY_PROFILE_SETTINGS_GET: AgentServerHookEvents.MEMORY_PROFILE_SETTINGS_GET,
    ReqMethod.MEMORY_PROFILE_SETTINGS_SET: AgentServerHookEvents.MEMORY_PROFILE_SETTINGS_SET,
    ReqMethod.MEMORY_PROFILE_GET: AgentServerHookEvents.MEMORY_PROFILE_GET,
    ReqMethod.MEMORY_PROFILE_MODIFY: AgentServerHookEvents.MEMORY_PROFILE_MODIFY,
    ReqMethod.MEMORY_PROFILE_RESET: AgentServerHookEvents.MEMORY_PROFILE_RESET,
}


def _validate(request: AgentRequest) -> None:
    params = request.params
    if not isinstance(params, dict) or request.is_stream:
        raise ValueError("memory profile methods require object params and is_stream=false")
    allowed = {"session_id", "project_id", "project_dir"}
    if request.req_method == ReqMethod.MEMORY_PROFILE_SETTINGS_SET:
        allowed.add("enabled")
        if type(params.get("enabled")) is not bool:
            raise ValueError("enabled must be a JSON boolean")
    if request.req_method == ReqMethod.MEMORY_PROFILE_MODIFY:
        allowed.add("instruction")
        text = params.get("instruction")
        if not isinstance(text, str) or not text.strip() or "\0" in text:
            raise ValueError("instruction must be a non-empty string without NUL")
    if set(params) - allowed:
        raise ValueError("unsupported memory profile parameter")
    for key in ("session_id", "project_id", "project_dir"):
        if key in params and (not isinstance(params[key], str) or "\0" in params[key]):
            raise ValueError(f"{key} must be a string without NUL")
    if request.session_id and params.get("session_id") and request.session_id != params["session_id"]:
        raise ValueError("session_id differs between envelope and params")


async def dispatch_memory_profile(request: AgentRequest, registry: Any = None) -> dict[str, Any]:
    """Emit exactly one completion/error; never retry a possibly completed write."""
    error = None
    context = None
    try:
        _validate(request)
    except ValueError as exc:
        error = {"code": "BAD_REQUEST", "message": str(exc)}
    if error is None:
        if registry is None:
            from jiuwenswarm.extensions.registry import ExtensionRegistry

            try:
                registry = ExtensionRegistry.get_instance()
            except RuntimeError:
                error = {"code": "MEMORY_UNAVAILABLE", "message": "Memory provider is unavailable"}
    if error is None:
        context = MemoryProfileHookContext(
            request_id=request.request_id, channel_id=request.channel_id,
            session_id=request.session_id or request.params.get("session_id"),
            req_method=request.req_method.value, params=dict(request.params),
        )
        try:
            await registry.trigger(MEMORY_PROFILE_EVENTS[request.req_method], context)
        except Exception:
            # Provider exceptions may contain profile text: never echo or log them.
            error = {"code": "MEMORY_OPERATION_FAILED", "message": "Memory provider failed",
                     "details": {"outcomeUnknown": True}}
        else:
            error = context.error
            if error is None and not context.handled:
                error = {"code": "MEMORY_UNAVAILABLE", "message": "Memory provider is unavailable"}
            elif error is None and not isinstance(context.result, dict):
                error = {"code": "MEMORY_OPERATION_FAILED", "message": "Memory provider returned no result",
                         "details": {"outcomeUnknown": True}}
    if error is not None:
        code = error.get("code", "MEMORY_OPERATION_FAILED")
        wire = encode_request_error_wire(
            request_id=request.request_id, channel_id=request.channel_id,
            code=code, message=error.get("message", "Memory operation failed"),
            details_kind="memory_profile",
        )
        wire["body"]["details"] = {**(error.get("details") or {}), "code": code}
        return wire
    return encode_agent_response_for_wire(
        AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                      ok=True, payload=context.result),
        response_id=request.request_id,
    )
