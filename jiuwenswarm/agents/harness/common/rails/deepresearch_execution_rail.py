# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native HITL bridge for the high-level DeepResearch execution tool."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserRequest

from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
    EXECUTION_SCHEMA,
    bind_deepresearch_execution_context,
    reset_deepresearch_execution_context,
)
from jiuwenswarm.common.schema.ask_user import ask_user_response_schema
from jiuwenswarm.perf.context import (
    extract_session_id_from_callback,
    get_request_context,
)

DEEPRESEARCH_EXECUTION_STATE_KEY = "deepresearch_execution_states"
DEEPRESEARCH_EXECUTION_ALIAS_KEY = "deepresearch_execution_aliases"
_TOKENS_KEY = "__deepresearch_execution_context_tokens__"
_TOOL_NAME = "deepresearch_execute"


def _tool_name(ctx: AgentCallbackContext) -> str:
    if not isinstance(ctx.inputs, ToolCallInputs):
        return ""
    return str(
        ctx.inputs.tool_name
        or getattr(ctx.inputs.tool_call, "name", "")
        or ""
    ).strip()


def _tool_call_id(ctx: AgentCallbackContext) -> str:
    if not isinstance(ctx.inputs, ToolCallInputs):
        return ""
    return str(getattr(ctx.inputs.tool_call, "id", "") or "").strip()


def _load_states(session: Any) -> dict[str, dict[str, Any]]:
    if session is None:
        return {}
    value = session.get_state(DEEPRESEARCH_EXECUTION_STATE_KEY)
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): dict(state)
        for key, state in value.items()
        if isinstance(state, Mapping)
    }


def _save_states(session: Any, states: dict[str, dict[str, Any]]) -> None:
    if session is not None:
        session.update_state({DEEPRESEARCH_EXECUTION_STATE_KEY: states})


def _load_aliases(session: Any) -> dict[str, str]:
    if session is None:
        return {}
    value = session.get_state(DEEPRESEARCH_EXECUTION_ALIAS_KEY)
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(workflow_id)
        for key, workflow_id in value.items()
        if str(key).strip() and str(workflow_id).strip()
    }


def _save_aliases(session: Any, aliases: dict[str, str]) -> None:
    if session is not None:
        session.update_state({DEEPRESEARCH_EXECUTION_ALIAS_KEY: aliases})


def _workflow_id(session: Any, tool_call_id: str) -> str:
    return _load_aliases(session).get(tool_call_id, tool_call_id)


def _interaction_tool_call(tool_call: Any, interaction_id: str) -> Any:
    model_copy = getattr(tool_call, "model_copy", None)
    cloned = model_copy(deep=True) if callable(model_copy) else copy.deepcopy(tool_call)
    cloned.id = interaction_id
    return cloned


def _resume_input(ctx: AgentCallbackContext, tool_call_id: str) -> Any:
    value = getattr(ctx, "extra", {}).get(RESUME_USER_INPUT_KEY)
    if isinstance(value, InteractiveInput):
        return value.user_inputs.get(tool_call_id)
    if isinstance(value, Mapping) and tool_call_id in value:
        return value[tool_call_id]
    return value


def _result_payload(value: Any) -> dict[str, Any] | None:
    detailed_output = getattr(value, "detailed_output", None)
    if isinstance(detailed_output, Mapping):
        value = detailed_output
    return dict(value) if isinstance(value, Mapping) else None


class DeepResearchExecutionRail(DeepAgentRail):
    """Persist the workflow state and turn tool envelopes into native HITL."""

    # Higher priorities run first. Convert the envelope before
    # JiuSwarmStreamEventRail(priority=80), so the stream rail sees the native
    # interrupt and does not leak the internal envelope as a tool result.
    priority = 81

    def __init__(self, *, model_provider):
        super().__init__()
        self._model_provider = model_provider
        self._agent_id = "jiuwenswarm"

    def init(self, agent: Any) -> None:
        card = getattr(agent, "card", None)
        self._agent_id = str(
            getattr(card, "id", "")
            or getattr(card, "name", "")
            or "jiuwenswarm"
        ).strip()

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if _tool_name(ctx) != _TOOL_NAME:
            return
        tool_call_id = _tool_call_id(ctx)
        workflow_id = _workflow_id(ctx.session, tool_call_id)
        states = _load_states(ctx.session)

        def save_state(state: dict[str, Any]) -> None:
            current = _load_states(ctx.session)
            current[workflow_id] = dict(state)
            _save_states(ctx.session, current)

        model = self._model_provider() if callable(self._model_provider) else None
        request_context = get_request_context(
            session_id=extract_session_id_from_callback(ctx)
        )
        request_id = str((request_context or {}).get("request_id") or "").strip()
        requested_report_type = (request_context or {}).get("requested_report_type")
        token = bind_deepresearch_execution_context(
            tool_call_id=workflow_id,
            state=states.get(workflow_id),
            user_input=_resume_input(ctx, tool_call_id),
            model=model,
            save_state=save_state,
            agent_id=self._agent_id,
            request_id=request_id,
            requested_report_type=requested_report_type,
        )
        tokens = ctx.extra.setdefault(_TOKENS_KEY, {})
        tokens[tool_call_id] = token

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if _tool_name(ctx) != _TOOL_NAME:
            return
        try:
            payload = _result_payload(ctx.inputs.tool_result)
            if payload is None or payload.get("schema_version") != EXECUTION_SCHEMA:
                return
            tool_call_id = _tool_call_id(ctx)
            workflow_id = _workflow_id(ctx.session, tool_call_id)
            state = payload.get("state")
            states = _load_states(ctx.session)
            if isinstance(state, Mapping):
                states[workflow_id] = dict(state)
                _save_states(ctx.session, states)

            if payload.get("kind") == "interaction":
                interaction = payload.get("interaction")
                if not isinstance(interaction, Mapping):
                    return
                questions = interaction.get("questions")
                if not isinstance(questions, list):
                    return
                request = AskUserRequest(
                    message=str(interaction.get("query") or ""),
                    payload_schema=ask_user_response_schema(),
                    questions=questions,
                )
                revision = int(state.get("revision") or 0) if isinstance(state, Mapping) else 0
                interaction_id = f"{workflow_id}_interaction_{revision}"
                aliases = _load_aliases(ctx.session)
                aliases[interaction_id] = workflow_id
                _save_aliases(ctx.session, aliases)
                ctx.inputs.tool_result = ToolInterruptException(
                    request=request,
                    tool_call=_interaction_tool_call(
                        ctx.inputs.tool_call,
                        interaction_id,
                    ),
                )
                ctx.inputs.tool_msg = None
                return

            states.pop(workflow_id, None)
            _save_states(ctx.session, states)
            aliases = {
                alias: target
                for alias, target in _load_aliases(ctx.session).items()
                if target != workflow_id
            }
            _save_aliases(ctx.session, aliases)
            content = payload.get("content")
            if isinstance(content, str) and content.strip():
                ctx.request_force_finish(
                    {"output": content.strip(), "result_type": "answer"}
                )
        finally:
            tool_call_id = _tool_call_id(ctx)
            tokens = ctx.extra.get(_TOKENS_KEY, {})
            token = tokens.pop(tool_call_id, None) if isinstance(tokens, dict) else None
            if not tokens:
                ctx.extra.pop(_TOKENS_KEY, None)
            reset_deepresearch_execution_context(token)


__all__ = [
    "DEEPRESEARCH_EXECUTION_ALIAS_KEY",
    "DEEPRESEARCH_EXECUTION_STATE_KEY",
    "DeepResearchExecutionRail",
]
