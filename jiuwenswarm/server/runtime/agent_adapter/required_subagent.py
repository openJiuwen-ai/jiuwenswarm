# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run one requested subagent through the ordinary ReAct tool path.

A request may name a mounted subagent and a task under
``params["agent_subagent_required"]``. The turn then answers from that child
alone: the rail replaces every model tool choice with the three SDK subagent
tools, and the adapter reports the child's verified result or an error.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness.subagent_runtime.config import WAIT_TIMEOUT_MS_DEFAULT

RUN_CONTEXT_KEY = "jiuwenswarm.required_subagent"
_STATE_KEY = "_jiuwenswarm_required_subagent"
#: Longest single ``subagent_wait``; a longer timeout costs more iterations.
_WAIT_MS = 60_000
#: The three SDK tools a required delegation drives.
_SUBAGENT_TOOLS = frozenset({"subagent_spawn", "subagent_wait", "subagent_close"})


@dataclass
class RequiredSubagentTurn:
    """What one required delegation asked for, and where it has got to."""

    name: str
    task: str
    timeout_ms: int
    elapsed_ms: int = 0
    subagent_id: str = ""
    result: str = ""
    error: str = ""
    completed: bool = False
    close_attempted: bool = False
    closed: bool = False

    def __deepcopy__(self, memo: dict[int, Any]) -> RequiredSubagentTurn:
        """Keep completion state shared when the SDK copies a queued request."""
        return self

    @property
    def cleanup_pending(self) -> bool:
        """A failed delegation that still owns a child to close."""
        return bool(self.error and self.subagent_id and not self.close_attempted)


def mounted_subagent_names(config: Any) -> list[str]:
    """The subagent names mounted on one agent, in SDK resolution order."""
    names: list[str] = []
    for spec in getattr(config, "subagents", None) or ():
        card = getattr(spec, "agent_card", None) or getattr(spec, "card", None)
        name = str(getattr(card, "name", "") or "").strip()
        # The SDK uses the first spec with a given name.
        if name and name not in names:
            names.append(name)
    return names


def required_subagent_from_params(params: dict[str, Any]) -> RequiredSubagentTurn | None:
    """Read ``agent_subagent_required`` from one request's parameters."""
    raw = params.get("agent_subagent_required")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("agent_subagent_required must contain a name and task")
    name, task = raw.get("name"), raw.get("task")
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError("agent_subagent_required.name must be a subagent name")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("agent_subagent_required.task must be non-empty text")
    return RequiredSubagentTurn(name=name, task=task, timeout_ms=WAIT_TIMEOUT_MS_DEFAULT)


def validate_required_subagent(agent: Any, state: RequiredSubagentTurn) -> None:
    """Refuse a delegation this agent cannot run, and say why."""
    config = getattr(getattr(agent, "react_agent", None), "config", None)
    max_iterations = getattr(config, "max_iterations", None)
    deep_config = getattr(agent, "deep_config", None)
    if max_iterations is None:
        max_iterations = getattr(deep_config, "max_iterations", None)
    # One spawn, one wait per timeout slice, one close on failure.
    required_iterations = 2 + math.ceil(state.timeout_ms / _WAIT_MS)
    if max_iterations is not None and max_iterations < required_iterations:
        raise ValueError(
            "Required subagent delegation needs at least "
            f"{required_iterations} ReAct iterations to wait and close on failure"
        )
    manager = getattr(agent, "ability_manager", None)
    cards = manager.list() if manager is not None else ()
    mounted_tools = {getattr(card, "name", None) for card in cards or ()}
    if not _SUBAGENT_TOOLS.issubset(mounted_tools):
        raise ValueError("Subagent delegation is not enabled")
    roster = mounted_subagent_names(deep_config)
    if state.name not in roster:
        raise ValueError(
            f"Subagent '{state.name}' is not mounted. "
            f"Mounted subagents: {', '.join(roster) or 'none'}"
        )


def with_required_subagent_run_context(
    inputs: dict[str, Any], state: RequiredSubagentTurn
) -> dict[str, Any]:
    """Copy *inputs* with the command in the SDK run context the rail reads."""
    updated = dict(inputs)
    raw_run = updated.get("run")
    run = dict(raw_run) if isinstance(raw_run, Mapping) else {}
    raw_context = run.get("context")
    context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
    raw_extra = context.get("extra")
    extra = dict(raw_extra) if isinstance(raw_extra, Mapping) else {}
    extra[RUN_CONTEXT_KEY] = state
    context["extra"] = extra
    run["context"] = context
    run.setdefault("kind", "normal")
    updated["run"] = run
    return updated


def required_subagent_state(inputs: dict[str, Any]) -> RequiredSubagentTurn | None:
    """The delegation inside *inputs*, or ``None`` for an ordinary turn."""
    run = inputs.get("run")
    context = run.get("context") if isinstance(run, Mapping) else None
    extra = context.get("extra") if isinstance(context, Mapping) else None
    state = extra.get(RUN_CONTEXT_KEY) if isinstance(extra, Mapping) else None
    return state if isinstance(state, RequiredSubagentTurn) else None


def _finish_if_settled(ctx: Any, state: RequiredSubagentTurn) -> None:
    """End the turn once the delegation has an answer or an error to report."""
    if state.error and not state.cleanup_pending:
        ctx.request_force_finish({"output": state.error, "result_type": "error"})
    elif state.completed:
        ctx.request_force_finish({"output": state.result, "result_type": "answer"})


def _tool_call(name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=f"required_subagent_{uuid4().hex}",
        type="function",
        name=name,
        arguments=json.dumps(arguments, ensure_ascii=False),
    )


class RequiredSubagentRail(AgentRail):
    """Replace model tool choices for one explicitly requested delegation."""

    async def before_model_call(self, ctx: Any) -> None:
        run_context = ctx.extra.get("run_context")
        extra = getattr(run_context, "extra", None)
        state = extra.get(RUN_CONTEXT_KEY) if isinstance(extra, Mapping) else None
        if isinstance(state, RequiredSubagentTurn):
            ctx.extra[_STATE_KEY] = state

    async def after_model_call(self, ctx: Any) -> None:
        state = ctx.extra.get(_STATE_KEY)
        response = getattr(ctx.inputs, "response", None)
        if not isinstance(state, RequiredSubagentTurn) or not isinstance(response, AssistantMessage):
            return
        response.content = ""
        response.reasoning_content = None
        if state.cleanup_pending:
            response.tool_calls = [_tool_call("subagent_close", {
                "subagent_id": state.subagent_id,
            })]
            return
        if state.error:
            response.tool_calls = []
            ctx.request_force_finish({"output": state.error, "result_type": "error"})
            return
        if not state.subagent_id:
            response.tool_calls = [_tool_call("subagent_spawn", {
                "subagent_type": state.name,
                "task_description": state.task,
                "display_name": state.name,
                "role": state.name,
            })]
            return
        response.tool_calls = [_tool_call("subagent_wait", {
            "subagent_ids": [state.subagent_id],
            "timeout_ms": min(_WAIT_MS, state.timeout_ms - state.elapsed_ms),
        })]

    async def after_tool_call(self, ctx: Any) -> None:
        state = ctx.extra.get(_STATE_KEY)
        if not isinstance(state, RequiredSubagentTurn):
            return
        tool_name = getattr(ctx.inputs, "tool_name", "")
        result = getattr(ctx.inputs, "tool_result", None)
        if tool_name not in _SUBAGENT_TOOLS:
            state.error = "Required subagent delegation failed: an unexpected tool ran."
        elif tool_name == "subagent_close":
            state.close_attempted = True
            data = getattr(result, "data", None)
            if (
                getattr(result, "success", False)
                and isinstance(data, dict)
                and data.get("subagent_id") == state.subagent_id
            ):
                state.closed = True
                state.error += f" Child {state.subagent_id} was closed."
            else:
                state.error += f" Child {state.subagent_id} may still be active; close failed."
        elif not getattr(result, "success", False) or not isinstance(getattr(result, "data", None), dict):
            state.error = f"Required subagent delegation failed during {tool_name}."
            if state.subagent_id:
                state.error += f" Child {state.subagent_id} needs cleanup."
        elif tool_name == "subagent_spawn":
            if state.subagent_id:
                state.error = "Required subagent delegation failed: more than one child started."
            else:
                state.subagent_id = str(result.data.get("subagent_id") or "")
                if not state.subagent_id:
                    state.error = "Required subagent delegation failed: spawn returned no child ID."
        else:
            state.elapsed_ms += min(_WAIT_MS, state.timeout_ms - state.elapsed_ms)
            status = result.data.get("statuses", {}).get(state.subagent_id)
            if status == "completed" and not result.data.get("timed_out"):
                state.result = str(result.data.get("results", {}).get(state.subagent_id) or "")
                if state.result.strip():
                    state.completed = True
                else:
                    state.error = "Required subagent delegation failed: child returned no answer."
            elif status not in {"running", "pending_init"} or state.elapsed_ms >= state.timeout_ms:
                state.error = (
                    "Required subagent delegation failed: "
                    f"child {state.subagent_id} status is {status or 'unknown'}."
                )
        _finish_if_settled(ctx, state)

    async def after_react_iteration(self, ctx: Any) -> None:
        state = ctx.extra.get(_STATE_KEY)
        if not isinstance(state, RequiredSubagentTurn):
            return
        _finish_if_settled(ctx, state)


__all__ = [
    "RequiredSubagentRail",
    "RequiredSubagentTurn",
    "mounted_subagent_names",
    "required_subagent_from_params",
    "required_subagent_state",
    "validate_required_subagent",
    "with_required_subagent_run_context",
]
