# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bind the compact root permission context inside OpenJiuwen callbacks."""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.sys_operation import OperationMode
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    PermissionContext,
    TOOL_PERMISSION_CONTEXT,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_ask_user import (
    ASK_USER_CONTINUATION_METADATA_KEY,
    ASK_USER_RESUME_DTO_KEY,
    ASK_USER_TOOL_NAME,
    RootAskUserResume,
    apply_ask_user_resume,
    build_ask_user_metadata,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    ROOT_CONTEXT_KEY,
    RootDecisionContext,
    bind_root_decision_context,
    current_root_decision_context,
    reset_root_decision_context,
    root_decision_context_from_extra,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    bind_root_permission_request,
    reset_root_permission_request,
    root_permission_resume_from_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_REQUEST_ID,
)
from jiuwenswarm.agents.harness.common.tools.command_execution_context import (
    bind_command_execution,
    bind_no_command_execution,
    reset_command_execution,
)

ROOT_CONTEXT_RAIL_PRIORITY = 99
ROOT_CONTEXT_FAILURE_ATTRIBUTE = "_jiuwenswarm_root_context_failure_v1"
ROOT_PERMISSION_OWNER_KEY = "jiuwenswarm.root_permission_owner.v1"
_TOKENS_ATTRIBUTE = "_jiuwenswarm_root_context_tokens_v1"
_FAILURE: ContextVar[str | None] = ContextVar(
    "jiuwenswarm_root_context_failure", default=None
)
_FAILURE_REASONS = frozenset(
    {
        "root_context_invalid",
        "root_context_missing",
        "root_context_session_mismatch",
        "root_context_command_unavailable",
        "root_context_resume_invalid",
        "root_context_ask_answer_invalid",
    }
)


@dataclass(frozen=True, slots=True)
class _Tokens:
    context: Any
    failure: Any
    channel: Any
    request: Any
    owner: Any
    root: Any
    command: Any


class RootContextRail(DeepAgentRail):
    """Bind/reset one root context; Core owns continuation identity."""

    priority = ROOT_CONTEXT_RAIL_PRIORITY

    def __init__(
        self,
        *,
        root_permission_queue: RootPermissionQueue,
        sys_operation: Any | None,
        sandboxed: bool | None,
    ) -> None:
        super().__init__()
        if not isinstance(root_permission_queue, RootPermissionQueue):
            raise TypeError("root permission queue is required")
        if sandboxed is not None and not isinstance(sandboxed, bool):
            raise TypeError("sandboxed flag must be bool or None")
        self._root_permission_queue = root_permission_queue
        self._command_sys_operation = sys_operation
        self._sandboxed = sandboxed

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        self._bind_lifecycle(ctx)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        self._discard_ask_resume(ctx)
        self._reset_lifecycle(ctx)

    async def before_task_iteration(self, ctx: AgentCallbackContext) -> None:
        self._bind_lifecycle(ctx)

    async def after_task_iteration(self, ctx: AgentCallbackContext) -> None:
        self._reset_lifecycle(ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        if not isinstance(inputs, ToolCallInputs):
            return
        context, owner, reason = self._from_callback(ctx)
        self._replace(context if reason is None else None, owner, reason=reason)
        resume = root_permission_resume_from_context(ctx)
        if resume is not None:
            retained = resume.card.root_context
            if not isinstance(retained, RootDecisionContext):
                reason = "root_context_resume_invalid"
            elif retained.session_id != _session_id(ctx):
                reason = "root_context_session_mismatch"
            else:
                context = retained
                reason = None
                self._replace(context, _owner_from_callback(ctx))
        elif _tool_name(inputs) == ASK_USER_TOOL_NAME:
            prepared = self._pop_ask_resume(ctx)
            if prepared is not None:
                if (
                    prepared.continuation.tool_call_id != _tool_call_id(inputs)
                    or prepared.continuation.context.session_id != _session_id(ctx)
                ):
                    reason = "root_context_ask_answer_invalid"
                else:
                    context = apply_ask_user_resume(prepared)
                    extra = _callback_extra(ctx)
                    if isinstance(extra, dict):
                        extra[ROOT_CONTEXT_KEY] = context.to_mapping()
                    reason = None
                    self._replace(context, _owner_from_callback(ctx))
        else:
            self._discard_ask_resume(ctx)
        if reason or context is None:
            reason = reason or "root_context_missing"
            self._replace(None, None, reason=reason)
            _skip_tool(ctx, reason)

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(getattr(ctx, "exception", None), ToolInterruptException):
            return
        inputs = getattr(ctx, "inputs", None)
        context = current_root_decision_context()
        if not isinstance(inputs, ToolCallInputs) or context is None:
            return
        metadata = build_ask_user_metadata(
            context=context,
            tool_name=_tool_name(inputs),
            tool_call_id=_tool_call_id(inputs),
            tool_args=inputs.tool_args,
        )
        if metadata is None:
            return
        request = ctx.exception.request
        request.metadata = {
            **(dict(request.metadata) if isinstance(request.metadata, Mapping) else {}),
            ASK_USER_CONTINUATION_METADATA_KEY: metadata,
        }

    def _bind_lifecycle(self, ctx: AgentCallbackContext) -> None:
        if isinstance(getattr(ctx, _TOKENS_ATTRIBUTE, None), _Tokens):
            return
        context, owner, reason = self._from_callback(ctx)
        tokens = self._bind(context if reason is None else None, owner, reason=reason)
        setattr(ctx, _TOKENS_ATTRIBUTE, tokens)
        if reason:
            setattr(ctx, ROOT_CONTEXT_FAILURE_ATTRIBUTE, reason)

    def _reset_lifecycle(self, ctx: AgentCallbackContext) -> None:
        tokens = getattr(ctx, _TOKENS_ATTRIBUTE, None)
        if not isinstance(tokens, _Tokens):
            return
        reset_command_execution(tokens.command)
        reset_root_permission_request(tokens.root)
        TOOL_PERMISSION_CONTEXT.reset(tokens.owner)
        TOOL_PERMISSION_REQUEST_ID.reset(tokens.request)
        TOOL_PERMISSION_CHANNEL_ID.reset(tokens.channel)
        _FAILURE.reset(tokens.failure)
        reset_root_decision_context(tokens.context)
        setattr(ctx, _TOKENS_ATTRIBUTE, None)

    def _from_callback(
        self, ctx: AgentCallbackContext
    ) -> tuple[RootDecisionContext | None, PermissionContext | None, str | None]:
        extra = _callback_extra(ctx)
        try:
            context = root_decision_context_from_extra(extra)
            owner = _owner_from_extra(extra, context.channel_id)
        except (TypeError, ValueError):
            return None, None, "root_context_invalid" if isinstance(extra, Mapping) else "root_context_missing"
        if context.session_id != _session_id(ctx):
            return None, None, "root_context_session_mismatch"
        if self._resolved_sys_operation() is None:
            return None, None, "root_context_command_unavailable"
        return context, owner, None

    def _resolved_sys_operation(self) -> Any | None:
        return self.sys_operation or self._command_sys_operation

    def _bind(
        self,
        context: RootDecisionContext | None,
        owner: PermissionContext | None,
        *,
        reason: str | None,
    ) -> _Tokens:
        context_token = bind_root_decision_context(context)
        failure = _FAILURE.set(reason)
        channel = TOOL_PERMISSION_CHANNEL_ID.set(context.channel_id if context else "")
        request = TOOL_PERMISSION_REQUEST_ID.set(context.request_id if context else "")
        owner_token = TOOL_PERMISSION_CONTEXT.set(owner if context else None)
        root = bind_root_permission_request(
            root_session_id=context.session_id if context else "",
            request_id=context.request_id if context else "",
            enabled=context is not None,
            queue=self._root_permission_queue,
        )
        operation = self._resolved_sys_operation()
        command = (
            bind_command_execution(
                operation,
                sandboxed=(
                    self._sandboxed
                    if self._sandboxed is not None
                    else getattr(operation, "mode", None) == OperationMode.SANDBOX
                ),
            )
            if context is not None and operation is not None
            else bind_no_command_execution()
        )
        return _Tokens(
            context_token,
            failure,
            channel,
            request,
            owner_token,
            root,
            command,
        )

    def _replace(
        self,
        context: RootDecisionContext | None,
        owner: PermissionContext | None,
        *,
        reason: str | None = None,
    ) -> None:
        bind_root_decision_context(context)
        _FAILURE.set(reason)
        TOOL_PERMISSION_CHANNEL_ID.set(context.channel_id if context else "")
        TOOL_PERMISSION_REQUEST_ID.set(context.request_id if context else "")
        TOOL_PERMISSION_CONTEXT.set(owner if context else None)
        bind_root_permission_request(
            root_session_id=context.session_id if context else "",
            request_id=context.request_id if context else "",
            enabled=context is not None,
            queue=self._root_permission_queue if context else None,
        )

    @staticmethod
    def _pop_ask_resume(ctx: AgentCallbackContext) -> RootAskUserResume | None:
        extra = _callback_extra(ctx)
        value = extra.pop(ASK_USER_RESUME_DTO_KEY, None) if isinstance(extra, dict) else None
        return value if isinstance(value, RootAskUserResume) else None

    @staticmethod
    def _discard_ask_resume(ctx: AgentCallbackContext) -> None:
        extra = _callback_extra(ctx)
        if isinstance(extra, dict):
            extra.pop(ASK_USER_RESUME_DTO_KEY, None)


def put_permission_owner_in_inputs(
    inputs: Mapping[str, Any], owner: PermissionContext | None
) -> dict[str, Any]:
    result = dict(inputs)
    run = dict(result.get("run")) if isinstance(result.get("run"), Mapping) else {}
    context = dict(run.get("context")) if isinstance(run.get("context"), Mapping) else {}
    extra = dict(context.get("extra")) if isinstance(context.get("extra"), Mapping) else {}
    extra.pop(ROOT_PERMISSION_OWNER_KEY, None)
    if owner is not None:
        extra[ROOT_PERMISSION_OWNER_KEY] = {
            "principal_user_id": owner.principal_user_id,
            "triggering_user_id": owner.triggering_user_id,
            "group_digital_avatar": owner.group_digital_avatar,
            "enable_memory": owner.enable_memory,
            "avatar_principal_name": owner.avatar_principal_name,
            "avatar_mode": owner.avatar_mode,
        }
    context["extra"] = extra
    run["context"] = context
    result["run"] = run
    return result


def root_context_failure_from_context(ctx: Any) -> str | None:
    reason = getattr(ctx, ROOT_CONTEXT_FAILURE_ATTRIBUTE, None)
    return reason if reason in _FAILURE_REASONS else None


def _owner_from_extra(extra: Any, channel_id: str) -> PermissionContext | None:
    raw = extra.get(ROOT_PERMISSION_OWNER_KEY) if isinstance(extra, Mapping) else None
    if raw is None:
        return None
    expected = {
        "principal_user_id",
        "triggering_user_id",
        "group_digital_avatar",
        "enable_memory",
        "avatar_principal_name",
        "avatar_mode",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("invalid permission owner")
    boolean_fields = ("group_digital_avatar", "enable_memory", "avatar_mode")
    if any(not isinstance(raw[name], bool) for name in boolean_fields):
        raise ValueError("invalid permission owner")
    text_fields = ("principal_user_id", "triggering_user_id", "avatar_principal_name")
    if any(not isinstance(raw[name], str) for name in text_fields):
        raise ValueError("invalid permission owner")
    return PermissionContext(channel_id=channel_id, **dict(raw))


def _owner_from_callback(ctx: AgentCallbackContext) -> PermissionContext | None:
    extra = _callback_extra(ctx)
    context = current_root_decision_context()
    return _owner_from_extra(extra, context.channel_id if context else "")


def _callback_extra(ctx: AgentCallbackContext) -> Any:
    run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
    if run_context is None and isinstance(getattr(ctx, "extra", None), Mapping):
        run_context = ctx.extra.get("run_context")
    return (
        run_context.get("extra")
        if isinstance(run_context, Mapping)
        else getattr(run_context, "extra", None)
    )


def _session_id(ctx: AgentCallbackContext) -> str:
    session = getattr(ctx, "session", None)
    getter = getattr(session, "get_session_id", None)
    value = getter() if callable(getter) else getattr(session, "session_id", "")
    return str(value or "").strip()


def _tool_name(inputs: ToolCallInputs) -> str:
    call = inputs.tool_call
    value = call.get("name") if isinstance(call, Mapping) else getattr(call, "name", "")
    return str(value or inputs.tool_name or "").strip()


def _tool_call_id(inputs: ToolCallInputs) -> str:
    call = inputs.tool_call
    value = call.get("id") if isinstance(call, Mapping) else getattr(call, "id", "")
    return str(value or "").strip()


def _skip_tool(ctx: AgentCallbackContext, reason: str) -> None:
    call = ctx.inputs.tool_call
    call_id = call.get("id") if isinstance(call, Mapping) else getattr(call, "id", "")
    message = f"[PERMISSION_BLOCKED] {reason}"
    ctx.inputs.tool_result = {
        "success": False,
        "status": "blocked",
        "is_error": True,
        "error": message,
        "permission_status": "blocked",
        "blocked_reason": reason,
    }
    ctx.inputs.tool_msg = ToolMessage(content=message, tool_call_id=str(call_id or ""))
    ctx.extra = dict(ctx.extra)
    ctx.extra["_skip_tool"] = True
    setattr(ctx, ROOT_CONTEXT_FAILURE_ATTRIBUTE, reason)


__all__ = [
    "ROOT_PERMISSION_OWNER_KEY",
    "RootContextRail",
    "put_permission_owner_in_inputs",
    "root_context_failure_from_context",
]
