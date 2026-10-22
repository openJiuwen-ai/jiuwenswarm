"""Carry child code approvals through the parent's existing interrupt flow.

Only the live delegation and its unsent cell are retained here. Permission
evaluation, approval parsing and persistence belong to the normal host rail.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import json
from typing import Any

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult, InterruptResult
from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserPayload, AskUserRequest

from jiuwenswarm.common.permission_mode import is_auto_permission_enabled


CODE_APPROVAL_LEAD = "**cua_repl 将执行 Python / cua_repl Python execution**"
_PENDING_STATE = "jiuwenswarm.cua_code_approval.v1"
_CALL_CONTEXT = ContextVar("cua_parent_code_approval", default=None)
_CHILD_APPROVAL = ContextVar("cua_child_code_approval", default=None)


async def request_code_permission(
    code: str, title: str, *, context: AgentCallbackContext | None = None,
) -> None:
    """Wait for the owning root to authorize this exact, still-unsent cell."""
    delegation = _CHILD_APPROVAL.get()
    if delegation is not None:
        await delegation.request(code, title)
        return
    if context is None:
        raise PermissionError("No host approval channel is bound. No code was sent; variables are unchanged.")
    # Non-interactive/hosted channels use the same policy. ALLOW still works;
    # an unhandled ASK must not silently grant execution or delegate again.
    cell = _Cell(code, title)
    while True:
        decision = await cell.decide(context, None)
        if isinstance(decision, ApproveResult):
            cell.name_index += 1
            if cell.name_index >= len(cell.names):
                return
        elif isinstance(decision, InterruptResult):
            raise PermissionError(
                "Code requires approval, but this host has no interactive CUA continuation. No code was sent."
            )
        else:
            raise PermissionError(str(decision.tool_result) + " No code was sent; variables are unchanged.")


@dataclass
class _Cell:
    code: str
    title: str
    answer: asyncio.Future | None = None
    names: tuple[str, ...] = ()
    name_index: int = 0
    question: str = ""

    def confirmation_input(self, user_input, call_id):
        """Project the standard question answer through the host's own parser."""
        if isinstance(user_input, AskUserPayload):
            user_input = user_input.model_dump()
        if not isinstance(user_input, dict) or "answers" not in user_input:
            return user_input
        answers = user_input["answers"]
        if not isinstance(answers, dict) or set(answers) != {self.question}:
            return None
        selected = answers[self.question]
        if not isinstance(selected, str) or not selected.strip():
            return None
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

        # Reuse the same option parsing as web, CLI and TUI confirmations.
        # pylint: disable-next=protected-access
        reply = JiuWenSwarm._build_interactive_input_from_answers(
            call_id, [{"selected_options": [selected]}], source="confirm_interrupt",
        )
        return reply.user_inputs.get(call_id)

    async def decide(self, ctx, user_input):
        # Import lazily: the standard host builder imports the delegation rail.
        from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
        from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import capture_permission_layers

        global_permissions, _, _, permissions = capture_permission_layers(ctx.session.get_session_id())
        if is_auto_permission_enabled(permissions):
            raise PermissionError(
                "Automatic code policy requires verified sandbox execution. LOCAL cua_repl "
                "cannot satisfy that provider contract. No code was sent."
            )
        policy = build_permission_rail(
            {"permissions": global_permissions}, session_id=ctx.session.get_session_id(),
        )
        if not self.names:
            self.names = ("code",)
        if "cua_repl" in permissions.get("tools", {}) and "cua_repl" not in self.names:
            self.names += ("cua_repl",)
        if policy is None:
            if permissions.get("enabled") is True:
                raise PermissionError("The host permission rail is unavailable. No code was sent.")
            return ApproveResult()
        call = ToolCall(
            id=ctx.inputs.tool_call.id, type="function", name=self.names[self.name_index],
            arguments=json.dumps({"code": self.code, "language": "python"}),
        )
        remembered = ctx.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) or {}
        # Recheck current policy before consuming an old answer. In particular,
        # a newly installed DENY must not be overridden by a waiting approval.
        fresh = await policy.resolve_interrupt(ctx, call, None, remembered)
        if not isinstance(fresh, InterruptResult) or user_input is None:
            return fresh
        return await policy.resolve_interrupt(ctx, call, self.confirmation_input(user_input, call.id), remembered)

class _Delegation:
    def __init__(self, arguments):
        self.arguments = dict(arguments)
        self.cells: asyncio.Queue[_Cell] = asyncio.Queue(maxsize=1)
        self.pending: _Cell | None = None
        self.task: asyncio.Task | None = None

    async def request(self, code, title):
        answer = asyncio.get_running_loop().create_future()
        await self.cells.put(_Cell(code, title, answer))
        error = await answer
        if error:
            raise PermissionError(str(error) + " No code was sent; variables are unchanged.")

    async def run(self, original, inputs, kwargs):
        token = _CHILD_APPROVAL.set(self)
        try:
            return await original.invoke(inputs, **kwargs)
        finally:
            _CHILD_APPROVAL.reset(token)

    async def next_cell(self):
        waiting = asyncio.create_task(self.cells.get())
        try:
            done, _ = await asyncio.wait((self.task, waiting), return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                return False
            self.pending = waiting.result()
            return True
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


class CuaCodeApprovals:
    """One transport per root agent; Core still owns interrupt/resume state."""

    def __init__(self):
        self.delegations: dict[tuple[str, str], _Delegation] = {}

    @staticmethod
    def key(ctx: AgentCallbackContext) -> tuple[str, str]:
        return ctx.session.get_session_id(), ctx.inputs.tool_call.id

    def is_pending(self, ctx: AgentCallbackContext) -> bool:
        state = ctx.session.get_state(_PENDING_STATE) or {}
        return self.key(ctx) in self.delegations or ctx.inputs.tool_call.id in state

    def bind(self, ctx, user_input):
        return _CALL_CONTEXT.set((self, ctx, user_input))

    @staticmethod
    def reset(token):
        _CALL_CONTEXT.reset(token)

    async def invoke(
        self, original: Tool, inputs: dict, kwargs: dict, ctx: AgentCallbackContext, user_input: Any,
    ):
        key = self.key(ctx)
        entry = self.delegations.get(key)
        if entry is None:
            if self.is_pending(ctx):
                return ToolOutput(success=False, error=(
                    "The suspended CUA process is no longer available. No code was replayed; "
                    "observe the desktop before starting a new task."
                ))
            entry = _Delegation(inputs)
            self.delegations[key] = entry
            entry.task = asyncio.create_task(entry.run(original, inputs, kwargs))
        elif entry.arguments != inputs:
            raise RuntimeError("CUA approval continuation arguments changed; no code was sent.")
        try:
            while entry.pending is not None or await entry.next_cell():
                if entry.task.done():
                    break
                decision = await entry.pending.decide(ctx, user_input)
                user_input = None  # An answer can authorize only its pending policy check.
                if isinstance(decision, InterruptResult):
                    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
                        extract_question_from_interaction,
                    )

                    original_request = decision.request
                    fence = "```"
                    while fence in entry.pending.code:
                        fence += "`"
                    entry.pending.question = (
                        CODE_APPROVAL_LEAD + "\n\n" + original_request.message
                        + "\n\n" + fence + "python\n" + entry.pending.code + "\n" + fence
                    )
                    question = extract_question_from_interaction(dict(id=key[1], value=dict(
                        original_request.model_dump(), tool_name=entry.pending.names[entry.pending.name_index],
                    )))
                    question.update(question=entry.pending.question, header="cua_repl")
                    # Use Core's structured question contract. The host already
                    # renders and resumes it without classifying arbitrary code
                    # text as a new permission request for the outer task_tool.
                    request = AskUserRequest(
                        message=entry.pending.question,
                        questions=[question],
                        payload_schema=AskUserPayload.to_schema(),
                        auto_confirm_key="cua_repl:code",
                        metadata=original_request.metadata,
                    )
                    state = dict(ctx.session.get_state(_PENDING_STATE) or {})
                    state[key[1]] = True
                    ctx.session.update_state({_PENDING_STATE: state})
                    raise ToolInterruptException(request=request, tool_call=ctx.inputs.tool_call)
                if isinstance(decision, ApproveResult):
                    entry.pending.name_index += 1
                    if entry.pending.name_index < len(entry.pending.names):
                        continue
                    error = None
                else:
                    error = getattr(decision, "tool_result", None) or "Code execution was rejected."
                entry.pending.answer.set_result(error)
                entry.pending = None
            return entry.task.result()
        except ToolInterruptException:
            # Leave the original child, Python variables and unsent cell alive.
            raise
        except BaseException:
            await self._cancel(key)
            raise
        finally:
            if entry.task.done():
                self.delegations.pop(key, None)
                state = dict(ctx.session.get_state(_PENDING_STATE) or {})
                state.pop(key[1], None)
                ctx.session.update_state({_PENDING_STATE: state})

    async def _cancel(self, key: tuple[str, str]) -> None:
        entry = self.delegations.pop(key, None)
        if entry is not None:
            entry.task.cancel()
            await asyncio.gather(entry.task, return_exceptions=True)

    async def close(self) -> None:
        for key in tuple(self.delegations):
            await self._cancel(key)

    def cancel(self) -> None:
        for entry in self.delegations.values():
            entry.task.cancel()


class CuaApprovalTool(Tool):
    """Keep TaskTool's implementation, cleanup, tracing and usage accounting."""

    def __init__(self, original, owner):
        super().__init__(original.card.model_copy(update={"id": owner + ".cua_approval_task"}))
        self.original = original

    async def invoke(self, inputs, **kwargs):
        binding = _CALL_CONTEXT.get()
        if binding is None or inputs.get("subagent_type") != "cua_agent":
            return await self.original.invoke(inputs, **kwargs)
        approvals, ctx, user_input = binding
        return await approvals.invoke(self.original, inputs, kwargs, ctx, user_input)

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
