# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Root tool identity rail and missing-sibling permission latch."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentRail,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionCard,
    RootPermissionQueue,
    RootPermissionQueueError,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    current_root_decision_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import (
    ToolInvocationKeyV1,
    normalize_tool_invocation_text,
)

ROOT_PERMISSION_QUEUE_PRIORITY = 100
ROOT_PERMISSION_COMPLETION_PRIORITY = 89
ROOT_PERMISSION_QUEUE_USER_INPUT_KEY = "_jiuwenswarm_root_permission_resume_v1"
ROOT_NON_PERMISSION_RESUME_DTO_KEY = "jiuwenswarm.root_non_permission_resume.v1"
TOOL_INVOCATION_CONTEXT_ATTRIBUTE = "_jiuwenswarm_tool_invocation_key_v1"
ROOT_PERMISSION_RESUME_ATTRIBUTE = "_jiuwenswarm_root_permission_resume_v1"
ROOT_NON_PERMISSION_RESUME_ATTRIBUTE = "_jiuwenswarm_root_non_permission_resume_v1"
PERMISSION_INTERRUPT_REQUEST_ATTRIBUTE = "_jiuwenswarm_permission_interrupt_request_v1"

_UNBOUND = object()


@dataclass(frozen=True, slots=True)
class RootPermissionRequestBinding:
    """Trusted root identity selected at the Host adapter boundary."""

    root_session_id: str
    request_id: str
    queue: RootPermissionQueue | None = None

    def __post_init__(self) -> None:
        for name in ("root_session_id", "request_id"):
            object.__setattr__(
                self,
                name,
                normalize_tool_invocation_text(name, getattr(self, name)),
            )
        if self.queue is not None and not isinstance(self.queue, RootPermissionQueue):
            raise TypeError("root permission queue is invalid")


@dataclass(frozen=True, slots=True)
class RootPermissionResume:
    """Exact pending-card state bound before Auto/Base permission consumes input."""

    card: RootPermissionCard
    user_input: Any


@dataclass(frozen=True, slots=True)
class RootNonPermissionResume:
    """Exact Core continuation admitted outside the Permission lifecycle."""

    root_session_id: str
    tool_call_id: str
    tool_name: str

    def __post_init__(self) -> None:
        for name in ("root_session_id", "tool_call_id", "tool_name"):
            object.__setattr__(
                self,
                name,
                normalize_tool_invocation_text(name, getattr(self, name)),
            )


_ROOT_BINDING: ContextVar[RootPermissionRequestBinding | None | object] = ContextVar(
    "jiuwenswarm_root_permission_request",
    default=_UNBOUND,
)


def bind_root_permission_request(
    *,
    root_session_id: str,
    request_id: str,
    enabled: bool,
    queue: RootPermissionQueue | None = None,
) -> Token[RootPermissionRequestBinding | None | object]:
    binding = (
        RootPermissionRequestBinding(
            root_session_id=root_session_id,
            request_id=request_id,
            queue=queue,
        )
        if enabled
        else None
    )
    return _ROOT_BINDING.set(binding)


def reset_root_permission_request(
    token: Token[RootPermissionRequestBinding | None | object],
) -> None:
    _ROOT_BINDING.reset(token)


def current_root_permission_queue() -> RootPermissionQueue | None:
    binding = _ROOT_BINDING.get()
    if binding is _UNBOUND:
        raise RootPermissionQueueError("root_permission_request_unbound")
    return binding.queue if isinstance(binding, RootPermissionRequestBinding) else None


def optional_root_permission_queue() -> RootPermissionQueue | None:
    """Return a queue only when the current request installed one."""

    binding = _ROOT_BINDING.get()
    return binding.queue if isinstance(binding, RootPermissionRequestBinding) else None


def tool_invocation_key_from_context(ctx: Any) -> ToolInvocationKeyV1 | None:
    key = getattr(ctx, TOOL_INVOCATION_CONTEXT_ATTRIBUTE, None)
    return key if isinstance(key, ToolInvocationKeyV1) else None


def root_permission_resume_from_context(ctx: Any) -> RootPermissionResume | None:
    value = getattr(ctx, ROOT_PERMISSION_RESUME_ATTRIBUTE, None)
    return value if isinstance(value, RootPermissionResume) else None


def root_nonpermission_resume_from_context(
    ctx: Any,
) -> RootNonPermissionResume | None:
    value = getattr(ctx, ROOT_NON_PERMISSION_RESUME_ATTRIBUTE, None)
    return value if isinstance(value, RootNonPermissionResume) else None


def put_root_nonpermission_resume_in_inputs(
    inputs: Mapping[str, Any], prepared: RootNonPermissionResume | None
) -> dict[str, Any]:
    """Install one Host-validated callback marker in shared RunContext state."""

    result = dict(inputs)
    run = dict(result.get("run")) if isinstance(result.get("run"), Mapping) else {}
    context = (
        dict(run.get("context")) if isinstance(run.get("context"), Mapping) else {}
    )
    extra = (
        dict(context.get("extra")) if isinstance(context.get("extra"), Mapping) else {}
    )
    extra.pop(ROOT_NON_PERMISSION_RESUME_DTO_KEY, None)
    if prepared is not None:
        extra[ROOT_NON_PERMISSION_RESUME_DTO_KEY] = prepared
    context["extra"] = extra
    run["context"] = context
    result["run"] = run
    return result


def mark_permission_interrupt_request(ctx: Any, request: Any) -> None:
    existing = getattr(ctx, PERMISSION_INTERRUPT_REQUEST_ATTRIBUTE, None)
    if existing is not None and existing is not request:
        raise RootPermissionQueueError("permission_interrupt_request_mismatch")
    setattr(ctx, PERMISSION_INTERRUPT_REQUEST_ATTRIBUTE, request)


def is_marked_permission_interrupt(ctx: Any, exc: BaseException | None) -> bool:
    interrupt = _tool_interrupt(exc)
    return bool(
        interrupt is not None
        and getattr(ctx, PERMISSION_INTERRUPT_REQUEST_ATTRIBUTE, None)
        is interrupt.request
    )


class RootPermissionQueueRail(AgentRail):
    """Bind one root call and latch missing pending siblings before routing."""

    priority = ROOT_PERMISSION_QUEUE_PRIORITY

    def __init__(
        self,
        queue: RootPermissionQueue,
        *,
        answer_claimed: Callable[[ToolInvocationKeyV1], None] | None = None,
    ) -> None:
        super().__init__()
        self.queue = queue
        self._answer_claimed = answer_claimed

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        if not isinstance(inputs, ToolCallInputs):
            return
        binding = _ROOT_BINDING.get()
        if binding is None:
            return
        if not isinstance(binding, RootPermissionRequestBinding):
            raise RootPermissionQueueError("root_permission_request_missing")
        if binding.queue is not self.queue:
            raise RootPermissionQueueError("root_permission_queue_mismatch")
        if hasattr(ctx, TOOL_INVOCATION_CONTEXT_ATTRIBUTE):
            raise RootPermissionQueueError("tool_invocation_context_already_bound")
        execution_session_id = _session_id(ctx)
        if execution_session_id != binding.root_session_id:
            raise RootPermissionQueueError("root_execution_session_mismatch")
        tool_call_id = _tool_call_id(inputs)
        tool_name = _tool_name(inputs)
        user_input = _resume_input(ctx, tool_call_id)
        marker_present, marker = _pop_nonpermission_resume(ctx)
        if user_input is _UNBOUND:
            if marker_present:
                raise RootPermissionQueueError("nonpermission_resume_input_missing")
            card = self.queue.begin(
                root_session_id=binding.root_session_id,
                request_id=binding.request_id,
                execution_session_id=execution_session_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )
        else:
            if marker_present:
                if not isinstance(marker, RootNonPermissionResume):
                    raise RootPermissionQueueError(
                        "nonpermission_resume_marker_invalid"
                    )
                if (
                    marker.root_session_id != binding.root_session_id
                    or marker.tool_call_id != tool_call_id
                    or marker.tool_name != tool_name
                ):
                    raise RootPermissionQueueError(
                        "nonpermission_resume_identity_mismatch"
                    )
                if self.queue.has_live(root_session_id=binding.root_session_id):
                    raise RootPermissionQueueError(
                        "nonpermission_resume_permission_conflict"
                    )
                if hasattr(ctx, ROOT_NON_PERMISSION_RESUME_ATTRIBUTE):
                    raise RootPermissionQueueError("nonpermission_resume_already_bound")
                setattr(ctx, ROOT_NON_PERMISSION_RESUME_ATTRIBUTE, marker)
                return
            claim = self.queue.claim_answer_for_call(
                root_session_id=binding.root_session_id,
                execution_session_id=execution_session_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )
            card = claim.card
            if self._answer_claimed is not None:
                self._answer_claimed(card.key)
        setattr(ctx, TOOL_INVOCATION_CONTEXT_ATTRIBUTE, card.key)
        if user_input is not _UNBOUND:
            if claim.quarantined:
                self.queue.finish(card.key)
                raise RootPermissionQueueError("permission_queue_resume_superseded")
            setattr(
                ctx,
                ROOT_PERMISSION_RESUME_ATTRIBUTE,
                RootPermissionResume(card, user_input),
            )
            return
        if card.state == "pending":
            if card.request is None:
                raise RootPermissionQueueError("permission_queue_request_missing")
            request = deepcopy(card.request)
            mark_permission_interrupt_request(ctx, request)
            raise AbortError(
                reason=f"Tool execution interrupted: {card.tool_name}",
                cause=ToolInterruptException(
                    request=request,
                    tool_call=inputs.tool_call,
                ),
            )

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        key = tool_invocation_key_from_context(ctx)
        if key is None:
            return
        if not is_marked_permission_interrupt(ctx, getattr(ctx, "exception", None)):
            self.queue.finish(key)
            return
        interrupt = _tool_interrupt(getattr(ctx, "exception", None))
        if interrupt is None:
            return
        _attach_key(interrupt.request, key)
        metadata = getattr(interrupt.request, "metadata", None)
        auto_manual = bool(
            isinstance(metadata, Mapping)
            and metadata.get("auto_permission_manual") is True
        )
        self.queue.mark_pending(
            key,
            request=interrupt.request,
            auto_manual=auto_manual,
            root_context=current_root_decision_context(),
        )


class RootPermissionCompletionRail(AgentRail):
    """End queue ownership immediately after the permission boundary allows."""

    priority = ROOT_PERMISSION_COMPLETION_PRIORITY

    def __init__(self, queue: RootPermissionQueue) -> None:
        super().__init__()
        self.queue = queue

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        key = tool_invocation_key_from_context(ctx)
        if key is not None:
            self.queue.finish(key)


def _resume_input(ctx: AgentCallbackContext, tool_call_id: str) -> Any:
    extra = getattr(ctx, "extra", None)
    if not isinstance(extra, Mapping):
        return _UNBOUND
    for name in (ROOT_PERMISSION_QUEUE_USER_INPUT_KEY, RESUME_USER_INPUT_KEY):
        if name not in extra:
            continue
        value = extra[name]
        user_inputs = getattr(value, "user_inputs", None)
        if isinstance(user_inputs, Mapping):
            return user_inputs.get(tool_call_id, _UNBOUND)
        if isinstance(value, Mapping) and tool_call_id in value:
            return value[tool_call_id]
        return value
    return _UNBOUND


def _pop_nonpermission_resume(ctx: AgentCallbackContext) -> tuple[bool, Any]:
    run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
    if run_context is None and isinstance(getattr(ctx, "extra", None), Mapping):
        run_context = ctx.extra.get("run_context")
    extra = (
        run_context.get("extra")
        if isinstance(run_context, Mapping)
        else getattr(run_context, "extra", None)
    )
    if not isinstance(extra, dict) or ROOT_NON_PERMISSION_RESUME_DTO_KEY not in extra:
        return False, None
    return True, extra.pop(ROOT_NON_PERMISSION_RESUME_DTO_KEY)


def _attach_key(request: Any, key: ToolInvocationKeyV1) -> None:
    metadata = getattr(request, "metadata", None)
    if metadata is not None and not isinstance(metadata, Mapping):
        raise RootPermissionQueueError("permission_interrupt_metadata_invalid")
    request.metadata = {
        **(dict(metadata) if isinstance(metadata, Mapping) else {}),
        "tool_invocation_key": key.to_wire(),
    }


def _tool_interrupt(exc: BaseException | None) -> ToolInterruptException | None:
    if isinstance(exc, ToolInterruptException):
        return exc
    cause = getattr(exc, "cause", None) if isinstance(exc, AbortError) else None
    return cause if isinstance(cause, ToolInterruptException) else None


def _session_id(ctx: AgentCallbackContext) -> str:
    session = getattr(ctx, "session", None)
    getter = getattr(session, "get_session_id", None)
    value = getter() if callable(getter) else getattr(session, "session_id", "")
    return normalize_tool_invocation_text("execution_session_id", value)


def _tool_call_id(inputs: ToolCallInputs) -> str:
    tool_call = inputs.tool_call
    value = (
        tool_call.get("id")
        if isinstance(tool_call, Mapping)
        else getattr(tool_call, "id", "")
    )
    return normalize_tool_invocation_text("tool_call_id", value)


def _tool_name(inputs: ToolCallInputs) -> str:
    tool_call = inputs.tool_call
    value = inputs.tool_name or (
        tool_call.get("name")
        if isinstance(tool_call, Mapping)
        else getattr(tool_call, "name", "")
    )
    return normalize_tool_invocation_text("tool_name", value)


__all__ = [
    "ROOT_NON_PERMISSION_RESUME_DTO_KEY",
    "ROOT_PERMISSION_QUEUE_USER_INPUT_KEY",
    "RootNonPermissionResume",
    "RootPermissionCompletionRail",
    "RootPermissionQueueRail",
    "RootPermissionResume",
    "bind_root_permission_request",
    "current_root_permission_queue",
    "is_marked_permission_interrupt",
    "mark_permission_interrupt_request",
    "put_root_nonpermission_resume_in_inputs",
    "reset_root_permission_request",
    "root_permission_resume_from_context",
    "root_nonpermission_resume_from_context",
    "tool_invocation_key_from_context",
]
