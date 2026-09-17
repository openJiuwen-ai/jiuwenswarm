# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code-mode TaskPlanningRail with CC-aligned todo tools and reminders."""

from __future__ import annotations

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentKind
from openjiuwen.harness.rails.task_planning_rail import TaskPlanningRail
from openjiuwen.harness.schema.task import TodoItem, TodoStatus
from openjiuwen.harness.tools.todo import TodoTool
from openjiuwen.harness.workspace.workspace import WorkspaceNode

from jiuwenswarm.agents.harness.code.tools.code_todo_tools import (
    CodeTodoCreateTool,
    CodeTodoGetTool,
    CodeTodoListTool,
    CodeTodoModifyTool,
)

_TASK_REMINDER_SECTION = "task_reminder"
_TASK_REMINDER_KIND = PromptAttachmentKind.TODO_REMINDER
_TASK_REMINDER_SOURCE = "jiuwenswarm.code_task_planning.task_reminder"
_TASK_REMINDER_TURNS_SINCE_MANAGEMENT = 10
_TASK_REMINDER_TURNS_BETWEEN_REMINDERS = 10
_MAX_TRACKED_TASK_REMINDER_SESSIONS = 1000


class CodeTaskPlanningRail(TaskPlanningRail):
    """Register code-mode todo tools.

    - Uses CodeTodo* tool classes (shorter descriptions, coarse-milestone guidance).
    - Skips the openjiuwen static todo system section.
    - Adds a task_reminder prompt attachment only when todo management
      has been absent for several model turns.
    """

    def __init__(
        self,
        *args,
        task_reminder_turns_since_management: int = _TASK_REMINDER_TURNS_SINCE_MANAGEMENT,
        task_reminder_turns_between_reminders: int = _TASK_REMINDER_TURNS_BETWEEN_REMINDERS,
        max_tracked_task_reminder_sessions: int = _MAX_TRACKED_TASK_REMINDER_SESSIONS,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.task_reminder_turns_since_management = task_reminder_turns_since_management
        self.task_reminder_turns_between_reminders = task_reminder_turns_between_reminders
        self.max_tracked_task_reminder_sessions = max(1, max_tracked_task_reminder_sessions)
        self._turns_since_task_management: dict[str, int] = {}
        self._turns_since_task_reminder: dict[str, int] = {}
        self._tracked_task_reminder_sessions: list[str] = []

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Refresh the per-call task_reminder attachment.

        Code mode intentionally avoids the parent rail's static todo prompt
        section. It still preserves the parent's model-selection behavior: 
        when the model has gone many turns without using todo tools, add one
        runtime prompt attachment that reminds it of the task tools and includes
        current todos.
        """

        await self._switch_model_if_needed(ctx)

        session_id = self._session_id(ctx)
        manager = getattr(getattr(ctx, "agent", None), "prompt_attachment_manager", None)
        if not session_id or manager is None or self._find_todo_tool() is None:
            return

        self._track_task_reminder_session(session_id)
        turns_since_management = self._turns_since_task_management.get(session_id, 0) + 1
        turns_since_reminder = self._turns_since_task_reminder.get(session_id, 0) + 1
        self._turns_since_task_management[session_id] = turns_since_management
        self._turns_since_task_reminder[session_id] = turns_since_reminder

        should_remind = (
            turns_since_management >= self.task_reminder_turns_since_management
            and turns_since_reminder >= self.task_reminder_turns_between_reminders
        )
        if not should_remind:
            await self._clear_task_reminder_attachment(manager, session_id)
            return

        todos = await self._load_fresh_todos(session_id)
        content = self._build_task_reminder_content(todos)
        await manager.add_section(
            session_id=session_id,
            section=_TASK_REMINDER_SECTION,
            kind=_TASK_REMINDER_KIND,
            content=content,
            priority=60,
            source=_TASK_REMINDER_SOURCE,
            metadata={"item_count": len(todos)},
            content_kind="text/markdown",
        )
        self._turns_since_task_reminder[session_id] = 0

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        await super().after_tool_call(ctx)

        if not self._is_todo_tool_call(ctx):
            return

        session_id = self._session_id(ctx)
        manager = getattr(getattr(ctx, "agent", None), "prompt_attachment_manager", None)
        if not session_id:
            return

        self._turns_since_task_management[session_id] = 0
        if manager is not None:
            await self._clear_task_reminder_attachment(manager, session_id)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        session_id = self._session_id(ctx)
        try:
            await super().after_invoke(ctx)
        finally:
            if session_id:
                self._clear_task_reminder_session_state(session_id)

    def init(self, agent) -> None:
        """Register CC-aligned todo tools on the agent."""
        from openjiuwen.harness.deep_agent import DeepAgent

        if not (
            isinstance(agent, DeepAgent)
            and agent.deep_config
            and hasattr(agent, "ability_manager")
        ):
            return

        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

        if not self.sys_operation:
            self.set_sys_operation(agent.deep_config.sys_operation)
        if not self.workspace:
            self.set_workspace(agent.deep_config.workspace)

        workspace_dir = str(self.workspace.get_node_path(WorkspaceNode.TODO))
        agent_id = getattr(getattr(agent, "card", None), "id", None)

        tool_configs: list[tuple[type, bool]] = [
            (CodeTodoCreateTool, False),
            (CodeTodoListTool, False),
            (CodeTodoGetTool, False),
            (CodeTodoModifyTool, False),
        ]

        existing_tools: list[TodoTool] = []
        for ability in agent.ability_manager.list():
            if isinstance(ability, ToolCard):
                tool_instance = Runner.resource_mgr.get_tool(tool_id=ability.id)
                if tool_instance:
                    for index, (tool_class, found) in enumerate(tool_configs):
                        if isinstance(tool_instance, tool_class):
                            tool_configs[index] = (tool_class, True)
                            existing_tools.append(tool_instance)
                            break

        tools = list(existing_tools)
        try:
            for tool_class, found in tool_configs:
                if not found:
                    new_tool = tool_class(
                        self.sys_operation,
                        workspace_dir,
                        "en",
                        agent_id,
                    )
                    # Unified registration (mirrors the parent TaskPlanningRail):
                    # add_ability qualifies the stateful tool id to
                    # ``{name}_{owner_id}`` and lets teardown_tools drop it at
                    # round-end, instead of leaking a bare id that refresh-warns
                    # on the next native rebuild.
                    agent.ability_manager.add_ability(new_tool.card, new_tool)
                    tools.append(new_tool)
            self.tools = tools
        except Exception as exc:
            logger.warning(
                "CodeTaskPlanningRail: failed to add tool, error: %s", exc
            )

    @staticmethod
    def _session_id(ctx: AgentCallbackContext) -> str | None:
        session = getattr(ctx, "session", None)
        if session is not None and hasattr(session, "get_session_id"):
            return session.get_session_id()
        return None

    @staticmethod
    def _is_todo_tool_call(ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        if isinstance(inputs, ToolCallInputs):
            tool_name = inputs.tool_name
        else:
            tool_name = getattr(inputs, "tool_name", "")
        return bool(tool_name and str(tool_name).startswith("todo_"))

    @staticmethod
    async def _clear_task_reminder_attachment(manager, session_id: str) -> None:
        try:
            await manager.clear_section(
                session_id=session_id,
                section=_TASK_REMINDER_SECTION,
            )
        except Exception:
            logger.debug("CodeTaskPlanningRail: failed to clear task reminder")

    async def _switch_model_if_needed(self, ctx: AgentCallbackContext) -> None:
        if not self._model_selection:
            return

        if self._default_llm is None:
            self._default_llm = getattr(ctx.agent, "_llm", None)

        selected_model_id = await self._get_fresh_in_progress_model_id(ctx)
        if selected_model_id and selected_model_id in self._model_id_to_model:
            target_model = self._model_id_to_model[selected_model_id]
        else:
            target_model = self._default_llm

        if target_model is not None:
            ctx.agent.set_llm(target_model)
            ctx.agent.config.model_name = target_model.model_config.model_name
            logger.debug(
                "CodeTaskPlanningRail: switched to model_id=%s", selected_model_id
            )

    def _track_task_reminder_session(self, session_id: str) -> None:
        if session_id in self._tracked_task_reminder_sessions:
            self._tracked_task_reminder_sessions.remove(session_id)
        self._tracked_task_reminder_sessions.append(session_id)

        while len(self._tracked_task_reminder_sessions) > self.max_tracked_task_reminder_sessions:
            evicted_session_id = self._tracked_task_reminder_sessions.pop(0)
            self._clear_task_reminder_session_state(evicted_session_id)

    def _clear_task_reminder_session_state(self, session_id: str) -> None:
        self._turns_since_task_management.pop(session_id, None)
        self._turns_since_task_reminder.pop(session_id, None)
        if session_id in self._tracked_task_reminder_sessions:
            self._tracked_task_reminder_sessions.remove(session_id)

    async def _get_fresh_in_progress_model_id(
        self,
        ctx: AgentCallbackContext,
    ) -> str | None:
        """Return selected_model_id from freshly loaded todos."""
        session_id = self._session_id(ctx)
        if session_id is None:
            return None

        todos = await self._load_fresh_todos(session_id)
        for todo in todos:
            if todo.status == TodoStatus.IN_PROGRESS:
                return todo.selected_model_id
        return None

    async def _load_fresh_todos(self, session_id: str) -> list[TodoItem]:
        tool = self._find_todo_tool()
        if tool is None:
            return []

        try:
            todos = await tool.load_todos(session_id)
        except Exception:
            logger.debug("CodeTaskPlanningRail: failed to load fresh todos")
            return []

        self._todos_cache[session_id] = todos
        return todos

    @staticmethod
    def _build_task_reminder_content(todos: list[TodoItem]) -> str:
        message = (
            "The task tools haven't been used recently. If you're working on "
            "tasks that would benefit from tracking progress, consider using "
            "todo_create to add new tasks and todo_modify to update task status "
            "(set to in_progress when starting, completed when done). Also "
            "consider cleaning up the task list if it has become stale. Only "
            "use these if relevant to the current work. This is just a gentle "
            "reminder - ignore if not applicable. Make sure that you NEVER "
            "mention this reminder to the user"
        )
        if not todos:
            return message

        task_items = []
        for todo in todos:
            status = todo.status.value if hasattr(todo.status, "value") else str(todo.status)
            task_items.append(f"#{todo.id}. [{status}] {todo.content}")
        return f"{message}\n\nHere are the existing tasks:\n\n" + "\n".join(task_items)
