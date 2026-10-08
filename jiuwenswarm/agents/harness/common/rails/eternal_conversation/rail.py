"""Pluggable, inert-by-default eternal-conversation Rail."""

from __future__ import annotations

import uuid
from typing import Any

from openjiuwen.core.foundation.tool.base import ToolCard
from openjiuwen.core.foundation.tool.function.function import LocalFunction
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import get_agent_sessions_dir

from .coordinator import SessionCoordinator
from .evidence import jsonable, read_json
from .prompts import render_memory_context
from .registry import get_session_coordinator


class EternalConversationRail(DeepAgentRail):
    """Record foreground evidence and schedule two isolated background Agents.

    One instance belongs to one session-scoped Adapter. It is always mounted so
    enabling the feature does not rebuild the Agent, but does no work and does
    not expose its tool until the request runtime flag is true.
    """

    priority = 80
    SECTION_NAME = "eternal_conversation"
    SECTION_PRIORITY = 90
    TOOL_NAME = "search_long_term_memory"

    def __init__(self) -> None:
        super().__init__()
        self._agent: Any = None
        self._builder: Any = None
        self._enabled = False
        self._session_id: str | None = None
        self._request_id: str | None = None
        self._mode = ""
        self._channel = ""
        self._project_dir: str | None = None
        self._model: Any = None
        self._coordinator: SessionCoordinator | None = None
        self._tool_card: ToolCard | None = None
        self._tool: LocalFunction | None = None
        self._tool_registered = False
        self._task_id: str | None = None
        self._interaction_resume = False
        self._pending_projection: dict[str, Any] | None = None

    def init(self, agent: Any) -> None:
        self._agent = agent
        self._builder = getattr(agent, "system_prompt_builder", None)
        self._tool_card = ToolCard(
            id="eternal_conversation_search_long_term_memory",
            name=self.TOOL_NAME,
            description=(
                "Search all published long-term memory. Results uniformly include both "
                "Pending and Built retrieval units. Use exact project names, aliases, "
                "constraints, version boundaries, or short key phrases."
            ),
            input_params={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or key phrase"}
                },
                "required": ["query"],
            },
            parallel_safe=True,
            stateless=False,
            idempotent=True,
        )

        async def search_long_term_memory(query: str) -> dict[str, Any]:
            if not self._enabled or self._coordinator is None:
                return {"error": "eternal_conversation_disabled", "matches": []}
            result = await self._coordinator.memory.search(query)
            await self._coordinator.evidence.append_audit(
                "foreground-memory-searches",
                {"task_id": self._task_id, "query": query, "result": result},
            )
            return result

        self._tool = LocalFunction(card=self._tool_card, func=search_long_term_memory)
        self._set_tool_enabled(False)

    def uninit(self, agent: Any) -> None:
        if self._builder is not None:
            self._builder.remove_section(self.SECTION_NAME)
        self._set_tool_enabled(False)
        self._agent = None
        self._builder = None

    def configure_runtime(
        self,
        *,
        enabled: bool,
        session_id: str | None,
        request_id: str | None,
        mode: str,
        channel: str,
        project_dir: str | None,
        model: Any,
        interaction_resume: bool = False,
    ) -> None:
        """Bind request values without sharing mutable state across Sessions."""
        normalized_session = str(session_id or "").strip() or None
        if enabled and normalized_session is None:
            raise ValueError("eternal conversation requires a session_id")
        if (
            self._session_id is not None
            and normalized_session is not None
            and self._session_id != normalized_session
        ):
            raise RuntimeError("one EternalConversationRail cannot serve multiple Sessions")
        self._enabled = bool(enabled)
        self._session_id = normalized_session or self._session_id
        self._request_id = request_id
        self._mode = str(mode or "")
        self._channel = str(channel or "")
        self._project_dir = project_dir
        self._model = model
        self._interaction_resume = bool(interaction_resume)
        if self._enabled and self._coordinator is None:
            root = get_agent_sessions_dir() / self._session_id / "eternal-conversation"
            self._coordinator = get_session_coordinator(
                root, self._session_id, lambda: self._model
            )
        self._set_tool_enabled(self._enabled)
        if not self._enabled and self._builder is not None:
            self._builder.remove_section(self.SECTION_NAME)

    def _set_tool_enabled(self, enabled: bool) -> None:
        manager = getattr(self._agent, "ability_manager", None)
        if manager is None or self._tool_card is None or self._tool is None:
            return
        if enabled and not self._tool_registered:
            result = manager.add_ability(self._tool_card, self._tool)
            self._tool_registered = bool(getattr(result, "added", True))
        elif not enabled and self._tool_registered:
            remove_ability = getattr(manager, "remove_ability", None)
            if callable(remove_ability):
                remove_ability(self.TOOL_NAME)
            else:
                manager.remove(self.TOOL_NAME)
            self._tool_registered = False

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if not self._enabled or self._coordinator is None:
            return
        await self._coordinator.resume_background()
        if self._interaction_resume:
            if self._task_id is None:
                raise RuntimeError("eternal conversation resume has no active natural task")
            await self._coordinator.evidence.append(
                "task-resumed",
                {"interaction_request_id": self._request_id},
                task_id=self._task_id,
            )
            return
        self._task_id = self._request_id or f"task-{uuid.uuid4().hex}"
        # In the real ReactAgent lifecycle BEFORE_INVOKE fires before the
        # ModelContext is initialized. Prepare the revision here, then apply it
        # in ON_USER_MESSAGE, which runs after context initialization but before
        # the new user input is admitted. This creates an actual safe boundary
        # without changing the framework's Rail mechanism.
        self._pending_projection = await self._coordinator.projection_for_boundary()
        await self._coordinator.evidence.append(
            "task-started",
            {
                "query": getattr(ctx.inputs, "query", None),
                "mode": self._mode,
                "channel": self._channel,
                "project_dir": self._project_dir,
            },
            task_id=self._task_id,
        )

    async def on_user_message(self, ctx: AgentCallbackContext) -> None:
        if not self._enabled or self._coordinator is None:
            return
        source = getattr(ctx.inputs, "source", None)
        if not self._interaction_resume and source == "query":
            if self._pending_projection is None:
                self._pending_projection = (
                    await self._coordinator.projection_for_boundary()
                )
            await self._apply_pending_projection(ctx)
        await self._coordinator.evidence.append(
            "user-message",
            {"parts": getattr(ctx.inputs, "parts", []), "source": source},
            task_id=self._task_id,
        )

    async def _apply_pending_projection(self, ctx: AgentCallbackContext) -> None:
        projection = self._pending_projection
        context = getattr(ctx, "context", None)
        if projection is None or context is None or self._coordinator is None:
            return
        previous = context.get_messages()
        context.set_messages([])
        await self._coordinator.evidence.append(
            "context-replaced",
            {
                "snapshot_revision": projection.get("snapshot_revision"),
                "memory_revision": projection.get("memory_revision"),
                "covered_through": projection.get("covered_through"),
                "replaced_messages": previous,
                "safe_boundary": "before-user-message-admission",
            },
            task_id=self._task_id,
        )
        await self._coordinator.mark_projection_applied(
            int(projection.get("snapshot_revision") or 0)
        )
        self._pending_projection = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if not self._enabled or self._coordinator is None or self._builder is None:
            return
        projection = read_json(self._coordinator.projection_path, {}) or {}
        content = render_memory_context(self._coordinator.root, projection)
        language = getattr(self._builder, "language", "cn") or "cn"
        self._builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={"cn": content, "en": content, language: content},
                priority=self.SECTION_PRIORITY,
            )
        )

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        await self._record_model_envelope(ctx, status="succeeded")

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        await self._record_model_envelope(ctx, status="failed")

    async def _record_model_envelope(self, ctx: AgentCallbackContext, *, status: str) -> None:
        if not self._enabled or self._coordinator is None:
            return
        response = getattr(ctx.inputs, "response", None)
        await self._coordinator.evidence.append(
            "model-visible-envelope",
            {
                "status": status,
                "messages": getattr(ctx.inputs, "messages", []),
                "tools": getattr(ctx.inputs, "tools", None),
                "response": response,
                "usage": getattr(response, "usage_metadata", None),
                "exception": str(ctx.exception) if ctx.exception else None,
            },
            task_id=self._task_id,
        )

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not self._enabled or self._coordinator is None:
            return
        await self._coordinator.evidence.append(
            "tool-call",
            {
                "tool_name": getattr(ctx.inputs, "tool_name", ""),
                "tool_args": getattr(ctx.inputs, "tool_args", None),
                "tool_call": getattr(ctx.inputs, "tool_call", None),
            },
            task_id=self._task_id,
        )

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        await self._record_tool_result(ctx, status="succeeded")

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        await self._record_tool_result(ctx, status="failed")

    async def _record_tool_result(self, ctx: AgentCallbackContext, *, status: str) -> None:
        if not self._enabled or self._coordinator is None:
            return
        await self._coordinator.evidence.append(
            "tool-result",
            {
                "status": status,
                "tool_name": getattr(ctx.inputs, "tool_name", ""),
                "tool_args": getattr(ctx.inputs, "tool_args", None),
                "tool_result": getattr(ctx.inputs, "tool_result", None),
                "tool_message": getattr(ctx.inputs, "tool_msg", None),
                "exception": str(ctx.exception) if ctx.exception else None,
            },
            task_id=self._task_id,
        )

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        if not self._enabled or self._coordinator is None:
            return
        result = jsonable(getattr(ctx.inputs, "result", None))
        if isinstance(result, dict) and result.get("result_type") == "interrupt":
            await self._coordinator.evidence.append(
                "task-suspended",
                {"result": result},
                task_id=self._task_id,
            )
            return
        event = await self._coordinator.evidence.append(
            "task-finished",
            {"result": result},
            task_id=self._task_id,
        )
        await self._coordinator.request_extract(int(event["cursor"]))

    async def close(self) -> None:
        # Coordinator lifetime follows the durable Session. Web/TUI routinely
        # retire short-lived Adapters before Extractor/Builder has converged.
        self._agent = None
        self._builder = None
        self._pending_projection = None

    async def wait_idle(self) -> None:
        if self._coordinator is not None:
            await self._coordinator.wait_idle()


__all__ = ["EternalConversationRail"]
