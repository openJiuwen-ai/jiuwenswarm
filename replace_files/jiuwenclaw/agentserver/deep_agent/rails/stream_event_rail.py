# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuClawStreamEventRail — Stream event emission, pause checks, context fix.

Migrated from JiuClawReActAgent:
  - _emit_tool_call / _emit_tool_result / _emit_todo_updated / _emit_context_compression
  - fix_incomplete_tool_context
  - Pause checkpoint logic
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Optional

import tiktoken
from openjiuwen.core.context_engine.schema.messages import OffloadMixin
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ToolMessage,
)
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent import BaseAgent
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.tools.todo import TodoStatus, TodoListTool
from openjiuwen.harness.workspace.workspace import WorkspaceNode

from jiuwenclaw.config import get_config
from jiuwenclaw.utils import fix_json_arguments, logger

# Import subagent context functions for fork_agent
from jiuwenclaw.agentserver.tools.subagent_executor import (
    set_subagent_parent_session,
    set_current_agent_context,
)

_TODO_TOOL_NAMES = frozenset(["todo_create", "todo_list", "todo_modify"])
_DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS = 128000


def _resolve_context_window_limit_tokens() -> int:
    """Read react.context_window_limit_tokens from config with safe coercion.

    Falls back to 128000 on missing / non-positive / non-numeric values.
    """
    try:
        react_cfg = (get_config() or {}).get("react") or {}
    except Exception:
        return _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS

    raw = react_cfg.get("context_window_limit_tokens")
    if isinstance(raw, bool):
        return _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS
    try:
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS
    if value <= 0:
        return _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS
    return value


def _structured_tool_result_payload(result: Any) -> Any | None:
    if isinstance(result, (dict, list)):
        return result
    return None


class JiuClawStreamEventRail(DeepAgentRail):
    """Emit frontend stream events and enforce pause/abort checkpoints.

    Pause/abort state is owned by this Rail (not DeepAgent) so that
    interface.py can call rail.pause() / rail.resume() / rail.abort()
    without requiring changes to DeepAgent.
    """

    priority = 80

    def __init__(self) -> None:
        super().__init__()
        self._deep_agent: Optional[Any] = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._abort_requested = False
        self._conversation_id: str = ""
        self._stream_tasks: set[asyncio.Task] = set()

    def init(self, agent: Any) -> None:
        self._deep_agent = agent

    # -- pause / resume / abort API for interface.py --

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._abort_requested = False
        self._pause_event.set()

    def abort(self) -> None:
        self._abort_requested = True
        self._pause_event.set()

    def reset_abort(self) -> None:
        self._abort_requested = False

    @property
    def abort_requested(self) -> bool:
        return self._abort_requested

    # ------------------------------------------------------------------
    # before_invoke (Outer event on DeepAgent): capture conversation_id
    # ------------------------------------------------------------------

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if isinstance(ctx.inputs, InvokeInputs):
            self._conversation_id = ctx.inputs.conversation_id or ""

    # ------------------------------------------------------------------
    # before_model_call: pause check + context fix + compression info
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        await self._pause_event.wait()
        if self._abort_requested:
            raise asyncio.CancelledError("Agent abort requested")

        # Always repair tool-call context before a model round.
        # The conversation may become incomplete after any prior tool-call
        # iteration, so a one-time guard is unsafe for multi-iteration ReAct loops.
        if ctx.context is not None:
            await self.fix_incomplete_tool_context(ctx.context)

        await self._emit_context_compression(ctx)
        await self._emit_context_usage(ctx)

    # ------------------------------------------------------------------
    # before_tool_call: pause check + emit tool_call event + set context for fork_agent
    # ------------------------------------------------------------------

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        await self._pause_event.wait()
        if self._abort_requested:
            raise asyncio.CancelledError("Agent abort requested")

        # Set current agent context for fork_agent to get correct messages
        # This ensures fork_agent gets messages from the running context
        # (not from storage which may be empty for subagent scenarios)
        if ctx.context is not None:
            set_current_agent_context(ctx.context)

        # Set parent session for subagent tools
        # Use parent session if session is a proxy (SubagentSessionProxy)
        # This ensures tools get the actual parent session, not the proxy wrapper
        session = ctx.session
        if session is not None:
            actual_session = getattr(session, '_parent', session) if session else None
            set_subagent_parent_session(actual_session)

        if session is not None and isinstance(ctx.inputs, ToolCallInputs):
            tc = ctx.inputs.tool_call
            await self._emit_tool_call(session, tc)
            await self._emit_tool_update(session, tc, status="in_progress")

    # ------------------------------------------------------------------
    # after_tool_call: emit tool_result + todo.updated + clear context
    # ------------------------------------------------------------------

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        # Clear context after tool execution
        set_current_agent_context(None)
        set_subagent_parent_session(None)

        session = ctx.session
        if session is None or not isinstance(ctx.inputs, ToolCallInputs):
            return

        await self._emit_tool_result(session, ctx.inputs.tool_call, ctx.inputs.tool_result)

        tool_name = ctx.inputs.tool_name
        if tool_name in _TODO_TOOL_NAMES and self._conversation_id:
            await self._emit_todo_updated(ctx.agent, session, self._conversation_id)

    # ------------------------------------------------------------------
    # on_model_exception: attempt context repair + clear context
    # ------------------------------------------------------------------

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        # Clear context on exception
        set_current_agent_context(None)
        set_subagent_parent_session(None)

        if ctx.context is not None:
            logger.info("[StreamEventRail] Attempting context repair after model exception")
            await self.fix_incomplete_tool_context(ctx.context)

    # ------------------------------------------------------------------
    # Private helpers (migrated from JiuClawReActAgent)
    # ------------------------------------------------------------------

    @staticmethod
    async def _emit_tool_call(session: Session, tool_call: Any) -> None:
        try:
            await session.write_stream(
                OutputSchema(
                    type="tool_call",
                    index=0,
                    payload={
                        "tool_call": {
                            "name": getattr(tool_call, "name", ""),
                            "arguments": getattr(tool_call, "arguments", {}),
                            "tool_call_id": getattr(tool_call, "id", ""),
                        }
                    },
                )
            )
        except Exception:
            logger.debug("tool_call emit failed", exc_info=True)

    @staticmethod
    async def _emit_tool_result(session: Session, tool_call: Any, result: Any) -> None:
        try:
            raw_output = _structured_tool_result_payload(result)
            tool_result_payload = {
                "tool_name": getattr(tool_call, "name", "") if tool_call else "",
                "tool_call_id": getattr(tool_call, "id", "") if tool_call else "",
                "result": str(result)[:1000] if result is not None else "",
            }
            if raw_output is not None:
                tool_result_payload["raw_output"] = raw_output
            await session.write_stream(
                OutputSchema(
                    type="tool_result",
                    index=0,
                    payload={
                        "tool_result": tool_result_payload
                    },
                )
            )
        except Exception:
            logger.debug("tool_result emit failed", exc_info=True)

    @staticmethod
    async def _emit_tool_update(session: Session, tool_call: Any, *, status: str) -> None:
        try:
            await session.write_stream(
                OutputSchema(
                    type="tool_update",
                    index=0,
                    payload={
                        "tool_update": {
                            "tool_name": getattr(tool_call, "name", "") if tool_call else "",
                            "tool_call_id": getattr(tool_call, "id", "") if tool_call else "",
                            "arguments": getattr(tool_call, "arguments", {}) if tool_call else {},
                            "status": str(status or "").strip() or "in_progress",
                        }
                    },
                )
            )
        except Exception:
            logger.debug("tool_update emit failed", exc_info=True)

    async def _emit_todo_updated(
        self, agent: BaseAgent, session: Session, session_id: str
    ) -> None:
        """Emit todo list update event to frontend.

        Loads current todos using TodoListTool, maps internal status to
        frontend-compatible values, and emits a 'todo.updated' stream event.

        Args:
            agent: The agent instance to access ability_manager for tool lookup.
            session: Session object for writing stream events.
            session_id: Session ID used to locate the todo JSON file.
        """
        todo_tool = self._get_todo_tool(agent)
        if todo_tool is None:
            logger.debug("[StreamEventRail] TodoListTool not available")
            return

        try:
            todo_tool.set_file(session_id)
            todos_data = await todo_tool.load_todos()
        except Exception as exc:
            logger.debug(
                "[StreamEventRail] Failed to load todos: %s", exc
            )
            return

        if not todos_data:
            return

        todos = self._format_todos_for_frontend(todos_data)

        try:
            await session.write_stream(
                OutputSchema(
                    type="todo.updated",
                    index=0,
                    payload={"todos": todos},
                )
            )
        except Exception:
            logger.debug("todo.updated emit failed", exc_info=True)

    def _get_todo_tool(self, agent: BaseAgent) -> TodoListTool | None:
        """Get TodoListTool from agent's ability_manager or create new instance.

        First attempts to retrieve the registered tool from the agent's
        ability_manager and Runner's resource_mgr. If not found, falls back
        to creating a new TodoListTool instance with rail's workspace config.

        Args:
            agent: The agent instance to access ability_manager.

        Returns:
            TodoListTool instance or None if unavailable.
        """
        # Try to get registered tool from agent's ability_manager
        try:
            tool_card = agent.ability_manager.get("todo_list")
            registered_tool = Runner.resource_mgr.get_tool(tool_card.id)
            if isinstance(registered_tool, TodoListTool):
                return registered_tool
        except Exception:
            pass

        # Fallback: create new tool instance
        try:
            language = getattr(
                getattr(self._deep_agent, "system_prompt_builder", None),
                "language", "cn",
            ) or "cn"
            agent_id = self._deep_agent.card.id if self._deep_agent else None 
            return TodoListTool(
                operation=self.sys_operation,
                workspace=str(self.workspace.get_node_path(WorkspaceNode.TODO)),
                language=language,
                agent_id=agent_id
            )
        except Exception as exc:
            logger.debug(
                "[StreamEventRail] Failed to create TodoListTool: %s", exc
            )
            return None

    @staticmethod
    def _format_todos_for_frontend(
        todos_data: List[Any],
    ) -> List[dict[str, Any]]:
        """Format todo items for frontend compatibility.

        Maps internal TodoStatus values to frontend-compatible status strings.

        Args:
            todos_data: List of TodoItem objects from TodoListTool.

        Returns:
            List of formatted todo dictionaries.
        """
        status_mapping = {
            TodoStatus.PENDING: "pending",
            TodoStatus.IN_PROGRESS: "in_progress",
            TodoStatus.COMPLETED: "completed",
            TodoStatus.CANCELLED: "skip",
        }

        return [
            {
                "id": item.id,
                "content": item.content,
                "activeForm": item.activeForm,
                "status": status_mapping.get(item.status, item.status.value),
                "createdAt": item.createdAt,
                "updatedAt": item.updatedAt,
            }
            for item in todos_data
        ]

    @staticmethod
    async def _emit_context_compression(ctx: AgentCallbackContext) -> None:
        """Emit context compression stats based on raw_total_tokens and current context tokens."""
        session = ctx.session
        if session is None:
            return

        context = ctx.context
        if context is None:
            return

        try:
            stat = context.statistic()
            raw_total_tokens = stat.raw_total_tokens
            current_context_tokens = stat.single_messages_token

            if raw_total_tokens > 0:
                rate = (raw_total_tokens - current_context_tokens) / raw_total_tokens * 100
            else:
                rate = 0

            await session.write_stream(
                OutputSchema(
                    type="context.compressed",
                    index=0,
                    payload={
                        "rate": rate,
                        "before_compressed": raw_total_tokens,
                        "after_compressed": current_context_tokens,
                    },
                )
            )
        except Exception:
            logger.debug("context_compression emit failed", exc_info=True)

    @staticmethod
    async def _emit_context_usage(ctx: AgentCallbackContext) -> None:
        """Emit context window usage stats for the frontend status panel.

        Numerator: current effective context tokens (post-compression).
        Denominator: react.context_window_limit_tokens from config.
        """
        session = ctx.session
        if session is None:
            return

        context = ctx.context
        if context is None:
            return

        try:
            stat = context.statistic()
            used_tokens_raw = getattr(stat, "single_messages_token", None)
            used_tokens = (
                max(int(used_tokens_raw), 0)
                if isinstance(used_tokens_raw, (int, float))
                else None
            )

            limit_tokens = _resolve_context_window_limit_tokens()
            usage_percent = (
                round(used_tokens / limit_tokens * 100, 1)
                if used_tokens is not None and limit_tokens > 0
                else None
            )

            await session.write_stream(
                OutputSchema(
                    type="context.usage",
                    index=0,
                    payload={
                        "used_tokens": used_tokens,
                        "limit_tokens": limit_tokens,
                        "usage_percent": usage_percent,
                    },
                )
            )
        except Exception:
            logger.debug("context_usage emit failed", exc_info=True)

    @staticmethod
    def _ensure_json_arguments(arguments: Any) -> str:
        """Ensure tool call arguments are valid JSON string.

        If arguments is a dict, convert to JSON string. If arguments is a string,
        validate it can be parsed as JSON. If parsing fails, return empty JSON object.

        Args:
            arguments: The arguments value from tool_call.

        Returns:
            Valid JSON string (e.g., '{"key": "value"}').
        """
        if isinstance(arguments, dict):
            import json
            return json.dumps(arguments)
        if isinstance(arguments, str):
            repaired = fix_json_arguments(arguments)
            if isinstance(repaired, dict):
                import json
                return json.dumps(repaired, ensure_ascii=False)
            logger.warning("Illegal Tool call arguments after repair: %s", arguments)
            return "{}"
        return "{}"

    async def fix_incomplete_tool_context(self, context: Any) -> None:
        """Fix incomplete context: ensure assistant messages with tool_calls have matching tool messages."""
        try:
            messages = context.get_messages()
            len_messages = len(messages)
            if len_messages == 0:
                return

            messages = context.pop_messages(size=len_messages)
            tool_message_cache: dict = {}
            tool_id_cache: list = []

            for i in range(len_messages):
                if isinstance(messages[i], AssistantMessage):
                    if not tool_id_cache:
                        tool_calls = getattr(messages[i], "tool_calls", None)
                        if tool_calls:
                            for tc in tool_calls:
                                arguments = getattr(tc, "arguments", '{}')
                                arguments = self._ensure_json_arguments(arguments)
                                if hasattr(tc, "arguments"):
                                    tc.arguments = arguments
                                tool_id_cache.append({
                                        "tool_call_id": getattr(tc, "id", ""),
                                        "tool_name": getattr(tc, "name", ""),
                                })
                        await context.add_messages(messages[i])
                    else:
                        logger.info("Fixed incomplete tool context with placeholder messages")
                        for tc_info in tool_id_cache:
                            tool_name = tc_info["tool_name"]
                            tool_call_id = tc_info["tool_call_id"]
                            if tool_call_id in tool_message_cache:
                                await context.add_messages(tool_message_cache[tool_call_id])
                            else:
                                await context.add_messages(ToolMessage(
                                        content=f"[工具执行被中断] 工具 {tool_name} 执行过程中被用户打断，没有执行结果。",
                                        tool_call_id=tool_call_id,
                                ))
                        tool_id_cache = []
                        tool_calls = getattr(messages[i], "tool_calls", None)
                        if tool_calls:
                            for tc in tool_calls:
                                arguments = getattr(tc, "arguments", {})
                                arguments = self._ensure_json_arguments(arguments)
                                if hasattr(tc, "arguments"):
                                    tc.arguments = arguments
                                tool_id_cache.append({
                                        "tool_call_id": getattr(tc, "id", ""),
                                        "tool_name": getattr(tc, "name", ""),
                                })
                        await context.add_messages(messages[i])
                elif isinstance(messages[i], ToolMessage):
                    if not tool_id_cache:
                        tool_message_cache[messages[i].tool_call_id] = messages[i]
                        continue
                    if messages[i].tool_call_id == tool_id_cache[0]["tool_call_id"]:
                        await context.add_messages(messages[i])
                        tool_id_cache.pop(0)
                    else:
                        tool_message_cache[messages[i].tool_call_id] = messages[i]
                        continue
                else:
                    logger.info("Fixed incomplete tool context with placeholder messages")
                    for tc_info in tool_id_cache:
                        tool_name = tc_info["tool_name"]
                        tool_call_id = tc_info["tool_call_id"]
                        if tool_call_id in tool_message_cache:
                            await context.add_messages(tool_message_cache[tool_call_id])
                        else:
                            await context.add_messages(ToolMessage(
                                    content=f"[工具执行被中断] 工具 {tool_name} 执行过程中被用户打断，没有执行结果。",
                                    tool_call_id=tool_call_id,
                            ))
                    tool_id_cache = []
                    await context.add_messages(messages[i])

            if tool_id_cache:
                for tc_info in tool_id_cache:
                    tool_name = tc_info["tool_name"]
                    tool_call_id = tc_info["tool_call_id"]
                    if tool_call_id in tool_message_cache:
                        await context.add_messages(tool_message_cache[tool_call_id])
                    else:
                        await context.add_messages(ToolMessage(
                                content=f"[工具执行被中断] 工具 {tool_name} 执行过程中被用户打断，没有执行结果。",
                                tool_call_id=tool_call_id,
                        ))
        except Exception as e:
            logger.warning("Failed to fix incomplete tool context: %s", e)
