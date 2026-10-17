"""Migrated Auto Permission runtime bridge slice."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, MutableMapping
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    PermissionInterruptRequest,
    ToolInvocation,
    logger,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_metadata import (
    _reviewer_ui_metadata,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.runtime_result import (
    _filter_base_call_kwargs,
    _is_openjiuwen_runtime_context,
    _permission_interrupt_extra_fields,
    _permission_interrupt_message,
    _permission_terminal_decision_source,
    _permission_terminal_outcome,
    _permission_tool_result_payload,
    _permission_tool_result_text,
    _resolve_base_call_args,
    _result_from_base_side_effect,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.audit import (
    emit_permission_audit,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import ASK_LEVEL
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    build_manual_approval_required_response,
    classify_permission_result,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context_rail import (
    root_context_failure_from_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_stream_metadata import (
    record_reviewer_tool_result_metadata,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_PERMISSION_QUEUE_USER_INPUT_KEY,
    mark_permission_interrupt_request,
    tool_invocation_key_from_context,
)


class AutoPermissionRuntimeBridgeMixin:
    """Phase 8c runtime bridge behavior."""

    def _emit_audit(self, facts: ToolDecisionFacts, **kwargs: Any) -> Any | None:
        return emit_permission_audit(
            facts,
            persistent_writer=self.persistent_audit_writer,
            **kwargs,
        )

    def _emit_permission_result_observation(
        self,
        facts: ToolDecisionFacts,
        *,
        result: Any,
        session_id: str | None,
        tool_call_id: str | None,
        decision_source: str,
        context: Any | None = None,
    ) -> None:
        """Observe a pending manual wait or final rail outcome."""
        outcome = _permission_terminal_outcome(result)
        source = _permission_terminal_decision_source(decision_source)
        try:
            if outcome is None:
                self._emit_audit(
                    facts,
                    decision=ASK_LEVEL,
                    reason="permission_manual_wait",
                    degraded=False,
                    extra={
                        "record_kind": "permission_pending",
                        "decision_source": source,
                        "authorization_stage": "manual_approval",
                        "stage_outcome": "manual_wait",
                    },
                )
                return
            extra = {
                "record_kind": "permission_terminal",
                "authorization_outcome": outcome,
                "decision_source": source,
            }
            host_context_failure = root_context_failure_from_context(context)
            if host_context_failure is not None:
                extra["host_context_failure"] = host_context_failure
            self._emit_audit(
                facts,
                decision=outcome,
                reason="permission_terminal",
                degraded=False,
                extra=extra,
            )
        except Exception:  # noqa: BLE001 - observation must not affect authorization
            logger.exception(
                "permission_terminal observation failed tool_name=%s",
                facts.tool_name,
            )

    def _runtime_result(
        self,
        invocation: ToolInvocation,
        result: Any,
    ) -> Any:
        result_class = classify_permission_result(result)
        is_runtime_context = _is_openjiuwen_runtime_context(invocation.ctx)
        if (
            is_runtime_context
            and result_class == "interrupt"
        ):
            result = self._prepare_manual_pending_invocation(invocation, result)
            result_class = classify_permission_result(result)
        if not is_runtime_context:
            return result
        if result_class == "allow":
            return result
        if result_class in {"denied", "user_rejection"}:
            self._skip_runtime_tool(invocation, result)
            return result
        if result_class == "interrupt":
            self._raise_runtime_interrupt(invocation, result)
        return result

    @staticmethod
    def _prepare_manual_pending_invocation(
        invocation: ToolInvocation, result: Any
    ) -> Any:
        key = tool_invocation_key_from_context(invocation.ctx)
        if key is None:
            return result
        if not isinstance(result, Mapping):
            return result
        wire_key = key.to_wire()
        merged = dict(result)
        value = merged.get("value")
        merged["value"] = {
            **(dict(value) if isinstance(value, Mapping) else {}),
            "tool_invocation_key": wire_key,
        }
        result_metadata = merged.get("metadata")
        merged["metadata"] = {
            **(dict(result_metadata) if isinstance(result_metadata, Mapping) else {}),
            "tool_invocation_key": wire_key,
        }
        return merged

    @staticmethod
    def _normalize_runtime_permission_result(
        invocation: ToolInvocation,
        result: Any,
        *,
        facts: ToolDecisionFacts,
        decision_source: str,
    ) -> Any:
        """Apply runtime-only terminal conversions before observation."""
        if (
            classify_permission_result(result) == "user_rejection"
            and decision_source == "manual_approval"
        ):
            reviewer_metadata = _reviewer_ui_metadata(
                facts,
                reason="user_rejected",
                metadata={
                    "decision_source": "manual_approval",
                    "final_reviewer_status": "denied",
                    "reviewer_status": "denied",
                },
            )
            return {
                "feedback": _permission_tool_result_text(result),
                "reason": "user_rejected",
                "status": "denied",
                "permission_decision": "deny",
                "permission_status": "denied",
                "reviewer_metadata": reviewer_metadata,
            }
        if (
            classify_permission_result(result) != "interrupt"
            or not _is_openjiuwen_runtime_context(invocation.ctx)
        ):
            return result
        return result

    def _skip_runtime_tool(self, invocation: ToolInvocation, result: Any) -> None:
        ctx = invocation.ctx
        tool_result_text = _permission_tool_result_text(result)
        tool_result = _permission_tool_result_payload(result, tool_result_text)
        tool_call_id = str(getattr(invocation.tool_call, "id", "") or "")
        if isinstance(tool_result, Mapping):
            reviewer_metadata = tool_result.get("reviewer_metadata")
            if not isinstance(reviewer_metadata, Mapping):
                reviewer_metadata = tool_result.get("reviewer")
            extra = getattr(ctx, "extra", None)
            if isinstance(reviewer_metadata, Mapping) and isinstance(
                extra, MutableMapping
            ):
                record_reviewer_tool_result_metadata(
                    extra,
                    tool_call_id=tool_call_id,
                    metadata=reviewer_metadata,
                )
        tool_message = ToolMessage(content=tool_result_text, tool_call_id=tool_call_id)
        skip_tool = getattr(self.base_rail, "_skip_tool", None)
        if callable(skip_tool):
            skip_tool(ctx, invocation.tool_call, tool_result, tool_message)
            return

        inputs = getattr(ctx, "inputs")
        ctx.extra["_skip_tool"] = True
        inputs.tool_result = tool_result
        inputs.tool_msg = tool_message

    def _raise_runtime_interrupt(self, invocation: ToolInvocation, result: Any) -> None:
        request = self._build_runtime_interrupt_request(invocation, result)
        mark_permission_interrupt_request(invocation.ctx, request)
        raise_interrupt = getattr(self.base_rail, "_raise_interrupt", None)
        if callable(raise_interrupt):
            raise_interrupt(invocation.tool_name, invocation.tool_call, request)
            return
        raise AbortError(
            reason=f"Tool execution interrupted: {invocation.tool_name}",
            cause=ToolInterruptException(
                request=request,
                tool_call=invocation.tool_call,
            ),
        )

    def _build_runtime_interrupt_request(
        self, invocation: ToolInvocation, result: Any
    ) -> InterruptRequest:
        extra_fields = _permission_interrupt_extra_fields(result)
        auto_manual = extra_fields.pop("auto_permission_manual", False) is True
        invocation_key = extra_fields.get("tool_invocation_key")
        request_metadata = (
            {
                "resume_user_input_key": ROOT_PERMISSION_QUEUE_USER_INPUT_KEY,
                "tool_invocation_key": dict(invocation_key),
            }
            if isinstance(invocation_key, Mapping)
            else {}
        )
        if auto_manual:
            request_metadata["auto_permission_manual"] = True
        return PermissionInterruptRequest(
            message=_permission_interrupt_message(result),
            payload_schema=ConfirmPayload.to_schema(),
            auto_confirm_key=(
                "" if auto_manual else self._runtime_auto_confirm_key(invocation)
            ),
            metadata=request_metadata,
            **extra_fields,
        )

    def _runtime_auto_confirm_key(self, invocation: ToolInvocation) -> str:
        if invocation.tool_call is None:
            return ""
        get_auto_confirm_key = getattr(self.base_rail, "_get_auto_confirm_key", None)
        if not callable(get_auto_confirm_key):
            return ""
        try:
            key = get_auto_confirm_key(invocation.tool_call)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return ""
        return str(key or "")

    async def _call_base_rail(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], invocation: ToolInvocation
    ) -> Any:
        before_tool_call = getattr(self.base_rail, "before_tool_call", None)
        if not callable(before_tool_call):
            return build_manual_approval_required_response(
                "base_permission_rail_unavailable"
            )

        call_args = _resolve_base_call_args(before_tool_call, args, kwargs, invocation)
        call_kwargs = _filter_base_call_kwargs(before_tool_call, kwargs) if args else {}
        result = before_tool_call(*call_args, **call_kwargs)
        if inspect.isawaitable(result):
            result = await result
        return _result_from_base_side_effect(invocation.ctx, result)

    async def _call_base_after_rail(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], invocation: ToolInvocation
    ) -> Any:
        after_tool_call = getattr(self.base_rail, "after_tool_call", None)
        if not callable(after_tool_call):
            return None
        call_args = _resolve_base_call_args(after_tool_call, args, kwargs, invocation)
        call_kwargs = _filter_base_call_kwargs(after_tool_call, kwargs)
        result = after_tool_call(*call_args, **call_kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
