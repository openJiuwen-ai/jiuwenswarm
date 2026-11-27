# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent import BaseAgent
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs, ToolCallInputs
from openjiuwen.core.runner import Runner
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.agentserver.tools.skill_step_toolkit import SkillStepToolkit
from jiuwenclaw.agentserver.tools.todo_toolkits import TaskStatus, TodoToolkit

logger = logging.getLogger(__name__)

_ACTIVE_TASK_ID: ContextVar[str | None] = ContextVar("active_task_id", default=None)


def get_current_task_id() -> str | None:
    """Return current task id for stream payload correlation."""
    return _ACTIVE_TASK_ID.get()


@dataclass
class TaskExecutionContext:
    task_id: str
    todo_id: str
    task_content: str
    task_index: int
    total_tasks: int
    parent_request_id: str
    start_time: float
    status: Literal["running", "succeeded", "failed", "skipped"] = "running"


class TaskExecutionRail(DeepAgentRail):
    """Emit task.start/task.complete around todo and skill_step execution status transitions.
    
    This rail tracks both TodoToolkit (todo_*) and the skill_step facade tool,
    emitting lifecycle events when task status changes.
    """

    priority = 85

    def __init__(self) -> None:
        super().__init__()
        self._todo_map: dict[str, dict[str, Any]] = {}
        self._todo_map_before_tool: dict[str, dict[str, Any]] = {}
        self._active_tasks: dict[str, TaskExecutionContext] = {}
        self._deep_agent: Any | None = None
        self._current_task_id: str | None = None

    def get_current_task_id(self) -> str | None:
        return self._current_task_id

    def init(self, agent: Any) -> None:
        self._deep_agent = agent
        logger.info("[TaskExecutionRail] init done: agent=%s", type(agent).__name__)

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        self._todo_map = {}
        self._active_tasks = {}
        self._current_task_id = None
        _ACTIVE_TASK_ID.set(None)
        if isinstance(ctx.inputs, InvokeInputs):
            await self._init_todo_tracking(ctx.agent, ctx.session)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        tool_name = ctx.inputs.tool_name
        todo_tools = {
            "todo_create", "todo_start", "todo_complete", "todo_insert", "todo_remove",
            "skill_step",
        }
        if tool_name in todo_tools:
            self._todo_map_before_tool = dict(self._todo_map)
            return

        self._bind_context_to_in_progress_task()

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        tool_name = ctx.inputs.tool_name
        todo_tools = {
            "todo_create", "todo_start", "todo_complete", "todo_insert", "todo_remove",
            "skill_step",
        }
        if tool_name in todo_tools:
            await self._sync_from_todo_tool_and_emit_transitions(ctx)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        self._todo_map_before_tool = {}
        self._bind_context_to_in_progress_task()

    async def _init_todo_tracking(self, agent: BaseAgent, session: Session | None) -> None:
        if session is None:
            return
        
        for tool_key in ["todo_list", "skill_step"]:
            try:
                if tool_key == "skill_step":
                    agent.ability_manager.get(tool_key)
                    registered_tool = SkillStepToolkit(session_id=session.get_session_id())
                else:
                    tool_card = agent.ability_manager.get(tool_key)
                    registered_tool = Runner.resource_mgr.get_tool(tool_card.id)
                if isinstance(registered_tool, (TodoToolkit, SkillStepToolkit)):
                    registered_tool.set_session_id(session.get_session_id())
                    todos = registered_tool.load_tasks()
                    total = len(todos)
                    for index, todo in enumerate(todos):
                        self._todo_map[str(todo.idx)] = {
                            "content": str(todo.tasks),
                            "status": self._normalize_status(todo.status),
                            "index": index,
                            "total": total,
                        }
                    if total > 0:
                        logger.info("[TaskExecutionRail] Initialized %s: %d tasks", tool_key, total)
            except Exception as exc:
                logger.debug("[TaskExecutionRail] No %s tool found: %s", tool_key, exc)

    async def _sync_from_todo_tool_and_emit_transitions(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None:
            return
        tool_name = ctx.inputs.tool_name if isinstance(ctx.inputs, ToolCallInputs) else ""
        todo_tool = self._get_todo_tool(ctx.agent, ctx.session.get_session_id(), tool_name)
        if todo_tool is None:
            logger.warning("[TaskExecutionRail] todo_tool not found for %s", tool_name)
            return
        try:
            todos = todo_tool.load_tasks()
        except Exception as exc:
            logger.warning("[TaskExecutionRail] Failed to load todos: %s", exc)
            return

        current_map = self._build_todo_map(todos)
        previous_map = dict(self._todo_map_before_tool or self._todo_map)
        parent_request_id = self._extract_request_id(ctx)

        for todo_id, current in current_map.items():
            prev = previous_map.get(todo_id)
            prev_status = str(prev.get("status", "")) if prev else ""
            curr_status = str(current.get("status", ""))

            if curr_status == "in_progress" and prev_status not in {"in_progress", "completed"}:
                logger.info("[TaskExecutionRail] task.start: idx=%s", todo_id)
                await self._emit_start_if_needed(ctx.session, todo_id, current, parent_request_id)
            elif prev_status == "in_progress" and curr_status == "completed":
                logger.info("[TaskExecutionRail] task.complete: idx=%s", todo_id)
                await self._emit_complete_if_needed(ctx.session, todo_id, status="succeeded")

        self._todo_map = current_map
        self._todo_map_before_tool = {}
        self._bind_context_to_in_progress_task()

    async def _emit_start_if_needed(
        self,
        session: Session,
        todo_id: str,
        todo: dict[str, Any],
        parent_request_id: str,
    ) -> None:
        task_id = self._build_task_id(todo_id)
        if task_id in self._active_tasks:
            _ACTIVE_TASK_ID.set(task_id)
            return
        context = TaskExecutionContext(
            task_id=task_id,
            todo_id=todo_id,
            task_content=str(todo.get("content", "")),
            task_index=int(todo.get("index", 0)),
            total_tasks=int(todo.get("total", 0)),
            parent_request_id=parent_request_id,
            start_time=time.time(),
        )
        self._active_tasks[task_id] = context
        _ACTIVE_TASK_ID.set(task_id)
        await self._emit_task_start(session, context)

    async def _emit_complete_if_needed(
        self,
        session: Session,
        todo_id: str,
        *,
        status: Literal["succeeded", "failed", "skipped"],
        error: str | None = None,
    ) -> None:
        task_id = self._build_task_id(todo_id)
        context = self._active_tasks.get(task_id)
        if context is None:
            return
        if get_current_task_id() == task_id:
            _ACTIVE_TASK_ID.set(None)
        await self._emit_task_complete(session, context, status=status, error=error)
        self._active_tasks.pop(task_id, None)

    def _bind_context_to_in_progress_task(self) -> None:
        for todo_id, todo in self._todo_map.items():
            if str(todo.get("status", "")) == "in_progress":
                task_id = self._build_task_id(todo_id)
                _ACTIVE_TASK_ID.set(task_id)
                self._current_task_id = task_id
                return
        _ACTIVE_TASK_ID.set(None)
        self._current_task_id = None

    async def _emit_task_start(self, session: Session, context: TaskExecutionContext) -> None:
        await session.write_stream(
            OutputSchema(
                type="task.start",
                index=0,
                payload={
                    "task_id": context.task_id,
                    "task_content": context.task_content,
                    "task_index": context.task_index,
                    "total_tasks": context.total_tasks,
                    "parent_request_id": context.parent_request_id,
                    "timestamp": context.start_time,
                },
            )
        )

    async def _emit_task_complete(
        self,
        session: Session,
        context: TaskExecutionContext,
        *,
        status: Literal["succeeded", "failed", "skipped"],
        error: str | None = None,
    ) -> None:
        timestamp = time.time()
        duration_ms = int((timestamp - context.start_time) * 1000)
        await session.write_stream(
            OutputSchema(
                type="task.complete",
                index=0,
                payload={
                    "task_id": context.task_id,
                    "task_content": context.task_content,
                    "status": status,
                    "duration_ms": duration_ms,
                    "error": error,
                    "timestamp": timestamp,
                },
            )
        )

    def _get_todo_tool(self, agent: BaseAgent, session_id: str, tool_name: str = "") -> Any | None:
        is_skill_step = tool_name == "skill_step"
        if is_skill_step:
            return SkillStepToolkit(session_id=session_id)

        toolkit_class = SkillStepToolkit if is_skill_step else TodoToolkit
        tool_key = "todo_list"
        
        try:
            tool_card = agent.ability_manager.get(tool_key)
            registered_tool = Runner.resource_mgr.get_tool(tool_card.id)
            if isinstance(registered_tool, toolkit_class):
                registered_tool.set_session_id(session_id)
                return registered_tool
        except Exception as exc:
            logger.debug("[TaskExecutionRail] Failed to get registered %s: %s", toolkit_class.__name__, exc)
            pass

        try:
            return toolkit_class(session_id=session_id)
        except Exception as exc:
            logger.warning("[TaskExecutionRail] Failed to create %s: %s", toolkit_class.__name__, exc)
            return None

    @staticmethod
    def _build_todo_map(todos: list[Any]) -> dict[str, dict[str, Any]]:
        mapped: dict[str, dict[str, Any]] = {}
        total = len(todos)
        for index, todo in enumerate(todos):
            mapped[str(todo.idx)] = {
                "content": str(todo.tasks),
                "status": TaskExecutionRail._normalize_status(todo.status),
                "index": index,
                "total": total,
            }
        return mapped

    @staticmethod
    def _extract_request_id(ctx: AgentCallbackContext) -> str:
        value = getattr(ctx.inputs, "request_id", "")
        return str(value) if value else ""

    @staticmethod
    def _normalize_status(status: Any) -> str:
        if isinstance(status, TaskStatus):
            status_map = {
                TaskStatus.WAITING: "waiting",
                TaskStatus.RUNNING: "in_progress",
                TaskStatus.COMPLETED: "completed",
                TaskStatus.CANCELLED: "cancelled",
            }
            return status_map.get(status, "waiting")
        if hasattr(status, "value"):
            return str(getattr(status, "value", "")).lower()
        return str(status or "").lower()

    @staticmethod
    def _build_task_id(todo_id: str) -> str:
        return f"todo:{todo_id}"
