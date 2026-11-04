# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep the agent from repeating client tool calls that are running or failed.

- ``before_tool_call``: a client tool call identical (tool and arguments) to one
  still running, or to one that timed out, was cancelled or lost its client, is
  refused before any other rail sees it, so it never reaches the client again.
- ``on_user_message``: input the user wrote (not a delivered result) lets the
  agent call failed tools again: the user may have asked for it.
- ``before_model_call``: a short note lists the running and failed calls so the
  model neither waits on nor invents their results.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs, UserMessageInputs
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.prompt.priority_registry import SystemPromptPriority
from jiuwenswarm.runtime.client_tool_adapter import ClientToolAdapter, is_client_tool_name, is_result_envelope

_SECTION = "client_tools"

# Above the permission queue (100): a refused repeat must not ask the user for approval first.
CLIENT_TOOL_GUARD_PRIORITY = 110


def _arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value) if value.strip() else {}
        except ValueError:
            return {}
    return value if isinstance(value, Mapping) else {}


class ClientToolGuardRail(DeepAgentRail):
    """Refuse repeated client tool calls and remind the model of the ones in flight."""

    priority = CLIENT_TOOL_GUARD_PRIORITY

    def __init__(self) -> None:
        super().__init__()
        self._adapter: Callable[[], ClientToolAdapter | None] = lambda: None
        self._session_id = ""
        self._prompt_builder = None

    def bind(self, session_id: str, adapter: Callable[[], ClientToolAdapter | None]) -> None:
        """Guard the client tool calls of ``session_id``."""
        self._session_id = str(session_id or "").strip()
        self._adapter = adapter

    def init(self, agent) -> None:
        self._prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        if self._prompt_builder is not None:
            self._prompt_builder.remove_section(_SECTION)
        self._prompt_builder = None

    def _bound(self) -> ClientToolAdapter | None:
        return self._adapter() if self._session_id else None

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs) or not is_client_tool_name(inputs.tool_name):
            return
        adapter = self._bound()
        if adapter is None:
            return
        refusal = adapter.check_invocation(self._session_id, inputs.tool_name, _arguments(inputs.tool_args))
        if refusal is not None:
            self._skip(ctx, refusal)

    async def on_user_message(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, UserMessageInputs) or inputs.source == "resume":
            return
        adapter = self._bound()
        if adapter is not None and any(str(part).strip() and not is_result_envelope(str(part)) for part in inputs.parts):
            await adapter.note_user_input(self._session_id)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self._prompt_builder is None:
            return
        self._prompt_builder.remove_section(_SECTION)
        adapter = self._bound()
        content = adapter.reminder(self._session_id) if adapter is not None else None
        if content:
            self._prompt_builder.add_section(PromptSection(
                name=_SECTION,
                content={"cn": content, "en": content},
                priority=SystemPromptPriority.CLIENT_TOOLS,
            ))

    @staticmethod
    def _skip(ctx: AgentCallbackContext, refusal: str) -> None:
        tool_call = ctx.inputs.tool_call
        call_id = str(
            (tool_call.get("id") if isinstance(tool_call, Mapping) else getattr(tool_call, "id", "")) or ""
        )
        if not call_id and tool_call is not None and not isinstance(tool_call, Mapping):
            # AbilityManager finds the skip marker by call id; bind one rather than skip nothing.
            call_id = f"client-tool-guard-{uuid.uuid4().hex}"
            tool_call.id = call_id
        payload = {"success": False, "reason": "client_tool_not_repeated", "retryable": False, "detail": refusal}
        ctx.inputs.tool_result = payload
        ctx.inputs.tool_msg = ToolMessage(content=refusal, tool_call_id=call_id)
        if call_id:
            ctx.extra.setdefault("_skip_tool_calls", {})[call_id] = True


__all__ = ["CLIENT_TOOL_GUARD_PRIORITY", "ClientToolGuardRail"]
