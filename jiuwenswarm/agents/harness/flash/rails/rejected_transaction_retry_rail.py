# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash-only continuation guard for atomically rejected apply_ops calls."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from openjiuwen.core.foundation.llm import ToolCall, ToolMessage
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.todo_close_gate import TodoCloseGate

logger = logging.getLogger(__name__)

REJECTED_TRANSACTION_RETRY_TOOL_NAME = "flash_rejected_transaction_retry"
REJECTED_TRANSACTION_RETRY_ID_PREFIX = "flash-rejected-retry-"

_INVOKE_TOOL_NAME = "invoke_tool"
_APPLY_OPS_TOOL_NAME = "apply_ops"


class RejectedTransactionRetryRail(DeepAgentRail):
    """Prevent a rejected transaction from masquerading as a final answer.

    Flash drops the shared TaskExecutionRail.  When an ``invoke_tool ->
    apply_ops`` transaction is atomically rejected, a weak model turn may say
    "I will fix and resend" without emitting the promised tool call.  The ReAct
    loop treats text-only output as a final answer, ending the request before
    the document transaction is retried.

    This rail is deliberately narrow: it only reacts to an explicit
    ``applied=false`` plus ``status=rejected`` business result, and only in
    Flash.  One internal retry nudge is injected; a second text-only ending or
    the final ReAct iteration force-finishes as an incomplete transaction.
    """

    priority = 91
    inherit_to_subagents = False

    def __init__(self) -> None:
        super().__init__()
        self._rejected_pending = False
        self._retry_nudge_used = False
        self._text_hold = TodoCloseGate(REJECTED_TRANSACTION_RETRY_TOOL_NAME)

    async def before_invoke(self, ctx: Any) -> None:
        """Reset request-local state so one request cannot poison the next."""
        _ = ctx
        self._rejected_pending = False
        self._retry_nudge_used = False
        self._text_hold.reset()

    async def before_model_call(self, ctx: Any) -> None:
        """Hold outer streamed text until this rail decides its fate."""
        if not self._rejected_pending:
            return
        # TodoCloseGate tracks delivery of hold/drop/release controls. Its
        # ``used`` flag only blocks a second nudge; this rail keeps that policy
        # in ``_retry_nudge_used`` so the second text turn can still be held
        # and dropped before an incomplete_transaction force-finish.
        self._text_hold.used = False
        await self._text_hold.arm(ctx.session, can_nudge=True)

    async def after_tool_call(self, ctx: Any) -> None:
        """Track the business outcome of the proxied apply_ops transaction."""
        inputs = getattr(ctx, "inputs", None)
        if not isinstance(inputs, ToolCallInputs):
            return
        if str(inputs.tool_name or "") != _INVOKE_TOOL_NAME:
            return

        inner_name, inner_result = self._unwrap_invoke_tool(
            inputs.tool_args, inputs.tool_result
        )
        if inner_name != _APPLY_OPS_TOOL_NAME:
            return

        if self._is_rejected_result(inner_result):
            self._rejected_pending = True
            logger.warning(
                "[RejectedTransactionRetryRail] apply_ops rejected; "
                "text-only next turn will be guarded"
            )
        elif self._is_applied_result(inner_result):
            self._rejected_pending = False

    async def after_react_iteration(self, ctx: Any) -> None:
        """Preserve the incomplete-transaction result at the final iteration.

        The generic final-iteration rail reports ``max_iterations`` after the
        last tool batch.  If that last batch leaves ``apply_ops`` rejected, the
        more precise business outcome must win: the document transaction is
        incomplete and can be resumed after fixing the rejected operations.
        """
        if not self._rejected_pending or not self._is_last_react_iteration(ctx):
            return
        output = self._incomplete_transaction_output()
        self._force_finish_incomplete(ctx, output=output)

    async def after_model_call(self, ctx: Any) -> None:
        """Convert one process-only text turn into a real retry request."""
        if not self._rejected_pending:
            return
        dropped = False
        try:
            inputs = getattr(ctx, "inputs", None)
            response = getattr(inputs, "response", None)
            if response is None or getattr(response, "tool_calls", None):
                return
            if str(getattr(response, "finish_reason", None) or "") == "length":
                return
            if not str(getattr(response, "content", None) or "").strip():
                return

            if self._retry_nudge_used or self._is_last_react_iteration(ctx):
                output = self._incomplete_transaction_output()
                response.content = output
                response.tool_calls = []
                self._force_finish_incomplete(ctx, output=output)
                dropped = True
                return

            response.content = ""
            response.tool_calls = [
                ToolCall(
                    id=(
                        f"{REJECTED_TRANSACTION_RETRY_ID_PREFIX}"
                        f"{uuid.uuid4().hex[:12]}"
                    ),
                    type="function",
                    name=REJECTED_TRANSACTION_RETRY_TOOL_NAME,
                    arguments="{}",
                )
            ]
            self._retry_nudge_used = True
            dropped = True
            logger.info(
                "[RejectedTransactionRetryRail] injected one apply_ops retry nudge"
            )
        finally:
            await self._text_hold.finish(ctx.session, dropped=dropped)

    async def before_tool_call(self, ctx: Any) -> None:
        """Skip the internal nudge and return an instruction to the model."""
        inputs = getattr(ctx, "inputs", None)
        if not isinstance(inputs, ToolCallInputs):
            return
        tool_call = inputs.tool_call
        if (
            str(inputs.tool_name or "")
            != REJECTED_TRANSACTION_RETRY_TOOL_NAME
            and str(getattr(tool_call, "name", "") or "")
            != REJECTED_TRANSACTION_RETRY_TOOL_NAME
        ):
            return

        message = (
            "[APPLY_OPS_RETRY_REQUIRED] 上一条 invoke_tool -> apply_ops 返回 "
            "applied=false且status=rejected，事务未生效。必须根据 failures "
            "修正对应 op，然后重新提交整个 apply_ops 事务；不要只输出说明文字，"
            "也不要跳过未完成的页面。"
        )
        ctx.extra["_skip_tool"] = True
        inputs.tool_result = message
        inputs.tool_msg = ToolMessage(
            content=message,
            tool_call_id=str(getattr(tool_call, "id", "") or ""),
        )

    @staticmethod
    def _parse_tool_args_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _unwrap_invoke_tool(cls, tool_args: Any, tool_result: Any) -> tuple[str | None, Any]:
        args = cls._parse_tool_args_payload(tool_args)
        inner_name = args.get("tool_name")
        if not isinstance(inner_name, str):
            inner_name = None

        if isinstance(tool_result, dict):
            if inner_name is None and isinstance(tool_result.get("tool_name"), str):
                inner_name = str(tool_result["tool_name"])
            return inner_name, tool_result.get("result")

        data = getattr(tool_result, "data", None)
        if isinstance(data, dict):
            if inner_name is None and isinstance(data.get("tool_name"), str):
                inner_name = str(data["tool_name"])
            return inner_name, data.get("result")
        return inner_name, tool_result

    @classmethod
    def _payload_dicts(cls, value: Any, depth: int = 0) -> list[dict[str, Any]]:
        """Conservatively parse known JSON/nested payload shapes."""
        if depth > 4:
            return []
        if isinstance(value, dict):
            found = [value]
            for child in value.values():
                found.extend(cls._payload_dicts(child, depth + 1))
            return found
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return []
            return cls._payload_dicts(parsed, depth + 1)

        data = getattr(value, "data", None)
        if data is not None:
            return cls._payload_dicts(data, depth + 1)
        return []

    @classmethod
    def _is_rejected_result(cls, result: Any) -> bool:
        for payload in cls._payload_dicts(result):
            if cls._is_rejected_payload(payload):
                return True
        return False

    @staticmethod
    def _is_rejected_payload(payload: dict[str, Any]) -> bool:
        return (
            payload.get("applied") is False
            and str(payload.get("status", "")).lower() == "rejected"
        )

    @classmethod
    def _is_applied_result(cls, result: Any) -> bool:
        for payload in cls._payload_dicts(result):
            if payload.get("applied") is True:
                return True
        return False

    @staticmethod
    def _is_last_react_iteration(ctx: Any) -> bool:
        inputs = getattr(ctx, "inputs", None)
        try:
            iteration = int(getattr(inputs, "react_iteration", 0) or 0)
        except (TypeError, ValueError):
            return False
        if iteration <= 0:
            return False
        agent = getattr(ctx, "agent", None)
        react_agent = getattr(agent, "_react_agent", None)
        config = getattr(react_agent, "_config", None)
        max_iterations = getattr(config, "max_iterations", None)
        if not isinstance(max_iterations, int) or max_iterations <= 0:
            return False
        return iteration >= max_iterations

    @staticmethod
    def _incomplete_transaction_output() -> str:
        return (
            "PPT 事务未完成：apply_ops 原子拒绝后没有成功重发。\n"
            "已完成的进度和产物已保留；失败原因见上一条 apply_ops 结果。\n"
            "可继续执行：请修复报错 op 后重新提交完整事务。"
        )

    @classmethod
    def _force_finish_incomplete(cls, ctx: Any, *, output: str) -> None:
        request_force_finish = getattr(ctx, "request_force_finish", None)
        if not callable(request_force_finish):
            logger.error(
                "[RejectedTransactionRetryRail] request_force_finish unavailable"
            )
            return
        request_force_finish(
            {
                "output": output,
                "result_type": "error",
                "finish_reason": "incomplete_transaction",
            }
        )


__all__ = [
    "REJECTED_TRANSACTION_RETRY_ID_PREFIX",
    "REJECTED_TRANSACTION_RETRY_TOOL_NAME",
    "RejectedTransactionRetryRail",
]
