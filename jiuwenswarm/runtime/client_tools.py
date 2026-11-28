# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The wire of tools a connected client executes.

agent-core runs a client tool (``ToolCard(execution="client")``) by pausing the
call with a ``client_tool`` interrupt. The stream carries one
``chat.client_tool_request`` event per paused call; the client runs it and
answers with an ordinary ``chat.send``::

    {"source": "client_tool_interrupt",
     "answers": [{"interrupt_id": ..., "request_id": <tool call id>,
                  "result": {"status": ..., "content": [...], "is_error": ...}}, ...]}

One resume may answer any subset of the paused calls; agent-core keeps the
others paused, still waiting for their client. A client that binds the
conversation again (a reloaded task pane) gets, with its bind, the requests the
paused agent still waits on (``AgentRuntime.pending_interactions``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.tool import ClientToolResult
from openjiuwen.core.single_agent.interrupt import InterruptKind
from openjiuwen.core.single_agent.rail.base import AgentRail, AgentCallbackEvent
from pydantic import ValidationError

from agent_gateway.common.interrupt_resume import CLIENT_TOOL_INTERRUPT_SOURCE
from jiuwenswarm.common.schema.message import EventType, ReqMethod


def client_tool_request_event(interaction: Any) -> dict[str, Any] | None:
    """The ``chat.client_tool_request`` event for a paused client tool call, else None."""
    if isinstance(interaction, Mapping):
        request_id, value = str(interaction.get("id") or ""), interaction.get("value")
    else:
        request_id, value = str(getattr(interaction, "id", "") or ""), getattr(interaction, "value", None)
    read = value.get if isinstance(value, Mapping) else lambda name: getattr(value, name, None)
    if not request_id or read("interrupt_kind") != InterruptKind.CLIENT_TOOL:
        return None
    return {
        "event_type": EventType.CHAT_CLIENT_TOOL_REQUEST.value,
        "request_id": request_id,
        "interrupt_id": str(read("interrupt_id") or ""),
        "source": CLIENT_TOOL_INTERRUPT_SOURCE,
        "tool_name": str(read("tool_name") or ""),
        "arguments": dict(read("arguments") or {}),
        "client_tool": dict(read("client_request") or {}),
    }


class ClientToolAnswerMismatch(ValueError):
    """A client tool resume no paused call waits on: the client drops its results."""

    code = "CLIENT_TOOL_ANSWER_MISMATCH"


@dataclass(frozen=True)
class ClientToolAnswer:
    """One call's result from a ``client_tool_interrupt`` resume, for the interrupt it names."""

    request_id: str
    interrupt_id: str
    result: ClientToolResult


def client_tool_answers(answers: Iterable[Any]) -> list[ClientToolAnswer]:
    """The results a ``client_tool_interrupt`` resume carries.

    An entry answers its call only with the call's ``request_id``, the
    ``interrupt_id`` of the request it answers and a ``result`` that
    ``ClientToolResult.coerce`` accepts; any other entry answers nothing.
    """
    parsed: list[ClientToolAnswer] = []
    for answer in answers:
        if not isinstance(answer, Mapping) or "result" not in answer:
            continue
        request_id, interrupt_id = answer.get("request_id"), answer.get("interrupt_id")
        if not (isinstance(request_id, str) and request_id and isinstance(interrupt_id, str) and interrupt_id):
            continue
        try:
            result = ClientToolResult.coerce(answer["result"])
        except ValidationError:
            continue
        parsed.append(ClientToolAnswer(request_id=request_id, interrupt_id=interrupt_id, result=result))
    return parsed


__all__ = [
    "ClientToolAnswer",
    "ClientToolAnswerMismatch",
    "client_tool_answers",
    "client_tool_request_event",
]


# Only opaque references enter core inputs/checkpoints. Invocation data stays
# process-local and is released by the existing request owner's finally block.
APPLICATION_RUN_REF = "jiuwenswarm.application_run_ref"
_APPLICATION_RUNS: dict[str, dict[str, Any]] = {}


def validate_application_request(request: Any, metadata: dict[str, Any]) -> None:
    """Reject unsupported root execution before mode routing changes the session."""
    application = request.channel_id == "ag-ui"
    stored_channel = metadata.get("channel_id")
    if stored_channel and application != (stored_channel == "ag-ui"):
        raise ValueError("AG-UI execution must use its original session channel")
    if not application:
        return
    if request.req_method == ReqMethod.COMMAND_GOAL:
        raise ValueError("AG-UI does not support Goal execution")
    if request.req_method != ReqMethod.CHAT_SEND:
        return
    params = request.params or {}
    if (params.get("mode", "agent") not in {"agent", "agent.work.normal"}
            or metadata.get("mode", "agent") not in {"agent", "agent.work.normal"}
            or params.get("work_mode", "work") != "work"
            or metadata.get("work_mode", "work") != "work"
            or any(params.get(key) for key in (
                "team", "is_swarm", "cron", "run", "activate_response", "goal", "goal_action",
                "attach_goal", "auto_harness", "eternal_conversation",
            ))
            or params.get("source", "") not in {"", "client_tool_interrupt"}):
        raise ValueError("AG-UI requires an ordinary Work chat.send")


def parse_client_tools(raw: object) -> list[Any]:
    """Validate application declarations without changing registered abilities."""
    import copy
    import json
    from openjiuwen.core.foundation.tool import ClientTool, ToolCard, ToolExecution

    if not isinstance(raw, list):
        raise ValueError("client_tools must be an array")
    json.dumps(raw, allow_nan=False)
    tools, names = [], set()
    for item in raw:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not item["name"].strip() or item["name"] in names
                or not isinstance(item.get("description", ""), str)
                or not isinstance(item.get("parameters"), dict)
                or item["parameters"].get("type") != "object"):
            raise ValueError("invalid or duplicate client tool declaration")
        names.add(item["name"])
        if item["name"] in ApplicationInteractionRail.unsupported_tools:
            raise ValueError(f"client tool conflicts with an interaction tool: {item['name']}")
        tools.append(ClientTool(ToolCard(
            name=item["name"], description=item.get("description", ""),
            input_params=copy.deepcopy(item["parameters"]), execution=ToolExecution.CLIENT,
        )))
    return tools


def validate_complete_client_answers(answers: Any, pending: list[dict[str, Any]]) -> None:
    """Require the entire native batch for AG-UI; Office retains partial resumes."""
    if not isinstance(answers, list):
        raise ClientToolAnswerMismatch("answers must be an array")
    parsed = client_tool_answers(answers)
    actual = [(answer.request_id, answer.interrupt_id) for answer in parsed]
    expected = {(entry["request_id"], entry["interrupt_id"]) for entry in pending}
    if (not expected or len(parsed) != len(answers) or len(set(actual)) != len(actual)
            or set(actual) != expected):
        raise ClientToolAnswerMismatch("answers must match the complete pending client-tool batch")


def application_run_data(ctx: Any) -> dict[str, Any] | None:
    """Resolve the current core invocation, never a prior session's input."""
    context = getattr(getattr(ctx, "inputs", None), "run_context", None) or getattr(ctx, "extra", {}).get("run_context")
    extra = context.get("extra", {}) if isinstance(context, dict) else getattr(context, "extra", {})
    return _APPLICATION_RUNS.get(extra.get(APPLICATION_RUN_REF)) if isinstance(extra, dict) else None


def supports_host_approval(ctx: Any = None, rail: Any = None) -> bool:
    """Read the bound host restriction, including synchronous child calls."""
    from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT

    if ctx is not None and application_run_data(ctx) is not None:
        return False
    context = CURRENT_RUN_CONTEXT.get()
    return (getattr(rail, "supports_permission_approval", True) is not False
            and (context is None or context.supports_permission_approval))


def replace_application_tools(agent: Any, tools: list[Any]) -> None:
    """Replace only this integration's cards, retaining backend/plugin ownership."""
    manager = agent.ability_manager
    owned = getattr(agent, "_application_client_cards", {})
    for tool in tools:
        existing = manager.get(tool.card.name)
        if existing is not None and existing is not owned.get(tool.card.name):
            raise ValueError(f"client tool conflicts with an existing tool: {tool.card.name}")
    for name, card in owned.items():
        if manager.get(name) is not card:
            raise ValueError(f"client tool ownership changed: {name}")
    for name in owned:
        manager.remove_ability(name)
    for tool in tools:
        manager.add_ability(tool.card, tool)
    agent._application_client_cards = {tool.card.name: tool.card for tool in tools}


class ApplicationInteractionRail(AgentRail):
    """Channel-specific presentation limits on the normal Agent execution chain."""

    priority = -100

    def init(self, agent: Any) -> None:
        self._agent = agent

    def fork_for_agent(self) -> ApplicationInteractionRail:
        return ApplicationInteractionRail()

    unsupported_tools = frozenset({
        "ask_user", "enter_plan_mode", "exit_plan_mode", "switch_mode",
        "create_goal", "update_goal", "sessions_spawn", "subagent_spawn",
        "subagent_send_input", "subagent_resume",
    })

    def callback_priority(self, event: Any) -> int:
        # Reject unsupported entry points before any interactive rail runs.
        return 10000 if event in {AgentCallbackEvent.BEFORE_TOOL_CALL, AgentCallbackEvent.BEFORE_INVOKE} else -100

    async def before_invoke(self, ctx: Any) -> None:
        from dataclasses import replace
        from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
        from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT, RunContext

        # Some child factories install the SDK permission rail rather than the
        # host subclass. Preserve its engine and bind only the final ASK hook.
        for rail in self._agent.find_rails_by_type((PermissionInterruptRail,)):
            if type(rail) is PermissionInterruptRail:
                rail._host = replace(rail._host, request_permission_confirmation=self._deny_confirmation)
        current = CURRENT_RUN_CONTEXT.get() or RunContext()
        ctx.extra["_application_context_token"] = CURRENT_RUN_CONTEXT.set(
            replace(current, supports_permission_approval=False),
        )

    @staticmethod
    async def _deny_confirmation(request: Any) -> Any:
        from openjiuwen.harness.security.permission_engine.models import PermissionConfirmResponse

        return PermissionConfirmResponse(approved=False)

    async def before_model_call(self, ctx: Any) -> None:
        data = application_run_data(ctx)
        if data is None:
            if not supports_host_approval(ctx):
                ctx.inputs.tools = [tool for tool in (ctx.inputs.tools or []) if tool.name not in self.unsupported_tools]
            return
        previous = set(getattr(data["agent"], "_application_client_cards", {}))
        replace_application_tools(data["agent"], data["tools"])
        # Preserve filtering performed by existing rails; add only App cards.
        own = set(data["agent"]._application_client_cards)
        if ctx.extra.get("_finalizing_max_iterations"):
            return
        ctx.inputs.tools = [
            tool for tool in (ctx.inputs.tools or [])
            if tool.name not in self.unsupported_tools and tool.name not in own
            and tool.name not in data["previous_names"] and tool.name not in previous
        ] + [tool.card.tool_info() for tool in data["tools"]]

    async def before_tool_call(self, ctx: Any) -> None:
        from dataclasses import replace
        from openjiuwen.core.foundation.llm import ToolMessage

        if supports_host_approval(ctx):
            return
        from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT, RunContext
        call_id = getattr(ctx.inputs.tool_call, "id", "")
        tokens = ctx.extra.setdefault("_application_tool_tokens", {})
        current = CURRENT_RUN_CONTEXT.get() or RunContext()
        tokens[call_id] = CURRENT_RUN_CONTEXT.set(replace(current, supports_permission_approval=False))
        name = ctx.inputs.tool_name or getattr(ctx.inputs.tool_call, "name", "")
        if name in self.unsupported_tools:
            text = "[INTERACTION_UNAVAILABLE] This channel supports text and application tools only. Ask questions in text. Background dispatch and Plan/Goal interaction are unavailable."
            ctx.extra.setdefault("_skip_tool_calls", {})[call_id] = True
            ctx.inputs.tool_result = text
            ctx.inputs.tool_msg = ToolMessage(content=text, tool_call_id=ctx.inputs.tool_call.id)

    async def after_tool_call(self, ctx: Any) -> None:
        from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT
        tokens = ctx.extra.get("_application_tool_tokens", {})
        token = tokens.pop(getattr(ctx.inputs.tool_call, "id", ""), None)
        if token is not None:
            CURRENT_RUN_CONTEXT.reset(token)

    async def after_invoke(self, ctx: Any) -> None:
        from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT

        data = application_run_data(ctx)
        if data is not None:
            data["round_result"] = getattr(ctx.inputs, "result", None)
        token = ctx.extra.pop("_application_context_token", None)
        if token is not None:
            CURRENT_RUN_CONTEXT.reset(token)
