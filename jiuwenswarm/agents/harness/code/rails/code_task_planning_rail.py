# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code-mode TaskPlanningRail with CC-aligned todo tools and reminders."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace
from weakref import WeakValueDictionary

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PROMPT_ATTACHMENT_HISTORY_METADATA_KEY,
    PromptAttachment,
    PromptAttachmentKind,
    hash_prompt_attachment,
)
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
_TASK_REMINDER_HISTORY_STATE_KEY = "state"
_TASK_REMINDER_HISTORY_SESSION_KEY = "session_id"
_TASK_REMINDER_BLOCK_SEPARATOR = "\n\n---\n\n"
_TASK_REMINDER_LEGACY_PREFIX = "The task tools haven't been used recently."
_TASK_REMINDER_CURRENT_PREFIX = "Use the task tools when the current work would benefit"
_TASK_REMINDER_COUNTED_ITERATIONS_KEY = (
    "_jiuwenswarm_task_reminder_counted_react_iterations"
)


@dataclass
class _TaskReminderState:
    content: str = ""
    item_count: int = 0
    delivery_sequence: int = 0
    turns_since_task_management: int = 0
    turns_since_task_reminder: int = 0


class CodeTaskPlanningRail(TaskPlanningRail):
    """Register code-mode todo tools.

    - Uses CodeTodo* tool classes (shorter descriptions, coarse-milestone guidance).
    - Skips the openjiuwen static todo system section.
    - Appends one task_reminder prompt attachment after each configured period
      without todo management.
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
        self._task_reminder_states: OrderedDict[str, _TaskReminderState] = OrderedDict()
        self._task_reminder_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )

    async def on_user_message(self, ctx: AgentCallbackContext) -> None:
        """Restore the active reminder before attachment history is synchronized."""

        await self._restore_task_reminder_attachment(ctx)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Append a task_reminder attachment when its cadence is due.

        Code mode intentionally avoids the parent rail's static todo prompt
        section. It still preserves the parent's model-selection behavior: 
        when the model has gone many turns without using todo tools, add one
        prompt attachment that reminds it of the task tools. Between reminder
        turns the attachment is left unchanged, keeping history append-only and
        avoiding a follow-up removal delta.
        """

        await self._switch_model_if_needed(ctx)

        session_id = self._session_id(ctx)
        manager = getattr(getattr(ctx, "agent", None), "prompt_attachment_manager", None)
        if not session_id or manager is None or self._find_todo_tool() is None:
            return

        # ON_USER_MESSAGE normally restores the attachment before admission.
        # Keep restoration and cadence mutation under the same session lock for
        # resumed workflows and overlapping requests on one adapter instance.
        async with self._task_reminder_lock(session_id):
            await self._restore_task_reminder_attachment_locked(
                ctx, manager, session_id
            )
            if not self._mark_task_reminder_iteration_counted(ctx):
                return
            state = self._get_task_reminder_state(ctx, session_id)
            state.turns_since_task_management += 1
            state.turns_since_task_reminder += 1
            self._save_task_reminder_state(session_id, state)

            should_remind = (
                state.turns_since_task_management
                >= self.task_reminder_turns_since_management
                and state.turns_since_task_reminder
                >= self.task_reminder_turns_between_reminders
            )
            if not should_remind:
                return

            delivery_sequence = state.delivery_sequence + 1
            content = (
                self._build_task_reminder_content()
                + f"\n\n<!-- task-reminder-delivery:{delivery_sequence} -->"
            )
            await manager.add_section(
                session_id=session_id,
                section=_TASK_REMINDER_SECTION,
                kind=_TASK_REMINDER_KIND,
                content=content,
                priority=60,
                source=_TASK_REMINDER_SOURCE,
                metadata={
                    "item_count": 0,
                    "delivery_sequence": delivery_sequence,
                },
                content_kind="text/markdown",
            )
            state.content = content
            state.item_count = 0
            state.delivery_sequence = delivery_sequence
            state.turns_since_task_reminder = 0
            self._save_task_reminder_state(session_id, state)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        await super().after_tool_call(ctx)

        if not self._is_todo_tool_call(ctx):
            return

        session_id = self._session_id(ctx)
        if not session_id:
            return

        async with self._task_reminder_lock(session_id):
            state = self._get_task_reminder_state(ctx, session_id)
            state.turns_since_task_management = 0
            self._save_task_reminder_state(session_id, state)

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

    async def _restore_task_reminder_attachment(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        session_id = self._session_id(ctx)
        manager = getattr(getattr(ctx, "agent", None), "prompt_attachment_manager", None)
        if not session_id or manager is None:
            return

        async with self._task_reminder_lock(session_id):
            await self._restore_task_reminder_attachment_locked(
                ctx, manager, session_id
            )

    async def _restore_task_reminder_attachment_locked(
        self,
        ctx: AgentCallbackContext,
        manager,
        session_id: str,
    ) -> None:
        """Reconcile the live manager with the canonical session state."""

        reminders = await manager.list_by_filter(
            session_id=session_id,
            section=_TASK_REMINDER_SECTION,
            source=_TASK_REMINDER_SOURCE,
        )
        if reminders:
            state = self._get_task_reminder_state(ctx, session_id)
            reminder = reminders[0]
            metadata = reminder.metadata or {}
            manager_sequence = max(
                self._non_negative_int(metadata.get("delivery_sequence")),
                self._delivery_sequence_from_content(reminder.content),
            )
            if not state.content or manager_sequence >= state.delivery_sequence:
                changed = (
                    state.content != (reminder.content or "")
                    or state.delivery_sequence != manager_sequence
                    or state.item_count
                    != self._non_negative_int(metadata.get("item_count"))
                )
                state.content = reminder.content or ""
                state.item_count = self._non_negative_int(metadata.get("item_count"))
                state.delivery_sequence = manager_sequence
                if changed:
                    self._save_task_reminder_state(session_id, state)
                return

            await self._add_task_reminder_attachment(manager, session_id, state)
            return

        state = self._get_task_reminder_state(ctx, session_id)
        if not state.content:
            recovered_content = await self._recover_task_reminder_from_history(
                ctx, session_id
            )
            if not recovered_content:
                return
            state.content = recovered_content
            state.delivery_sequence = self._delivery_sequence_from_content(
                recovered_content
            )
            state.item_count = 0
            self._save_task_reminder_state(session_id, state)

        await self._add_task_reminder_attachment(manager, session_id, state)

    @staticmethod
    async def _add_task_reminder_attachment(
        manager,
        session_id: str,
        state: _TaskReminderState,
    ) -> None:
        await manager.add_section(
            session_id=session_id,
            section=_TASK_REMINDER_SECTION,
            kind=_TASK_REMINDER_KIND,
            content=state.content,
            priority=60,
            source=_TASK_REMINDER_SOURCE,
            metadata={
                "item_count": state.item_count,
                "delivery_sequence": state.delivery_sequence,
            },
            content_kind="text/markdown",
        )

    async def _recover_task_reminder_from_history(
        self,
        ctx: AgentCallbackContext,
        session_id: str,
    ) -> str:
        """Recover an active pre-state reminder from attachment history."""

        messages = self._history_messages(ctx)
        target_hash, _ = self._latest_task_reminder_history_state(
            messages, session_id
        )
        if not target_hash:
            return ""

        for message in reversed(messages):
            if not self._is_attachment_history_message(message, session_id):
                continue
            rendered = getattr(message, "content", "")
            if not isinstance(rendered, str):
                continue
            for content in self._task_reminder_content_candidates(rendered):
                if self._task_reminder_hash(content, session_id) == target_hash:
                    return content

        # Older reminders embedded the whole todo list and may have exceeded
        # PromptAttachmentManager's per-section rendering limit. The history
        # then contains only a truncated prefix while its metadata retains the
        # hash of the complete content. Rebuild that legacy payload from the
        # authoritative todo store so the active section can still be restored
        # without appending a spurious removal delta during an upgrade.
        todos = await self._load_fresh_todos(session_id)
        legacy_content = self._build_legacy_task_reminder_content(todos)
        if self._task_reminder_hash(legacy_content, session_id) == target_hash:
            return legacy_content
        return ""

    @staticmethod
    def _task_reminder_hash(content: str, session_id: str) -> str:
        candidate = PromptAttachment(
            id="recovered-task-reminder",
            section=_TASK_REMINDER_SECTION,
            kind=_TASK_REMINDER_KIND,
            content=content,
            source=_TASK_REMINDER_SOURCE,
            session_id=session_id,
            content_kind="text/markdown",
        )
        return hash_prompt_attachment(candidate)

    @staticmethod
    def _task_reminder_content_candidates(rendered: str) -> list[str]:
        candidates: list[str] = []
        for prefix in (
            _TASK_REMINDER_CURRENT_PREFIX,
            _TASK_REMINDER_LEGACY_PREFIX,
        ):
            start = rendered.find(prefix)
            if start < 0:
                continue
            endpoints = {len(rendered)}
            for boundary in (
                _TASK_REMINDER_BLOCK_SEPARATOR,
                "\n\nThe following previously supplied dynamic context",
                "\n\n以下先前提供的动态上下文已不再生效",
                "\n</system-reminder>",
            ):
                search_from = start
                while True:
                    end = rendered.find(boundary, search_from)
                    if end < 0:
                        break
                    endpoints.add(end)
                    search_from = end + len(boundary)
            for end in sorted(endpoints):
                if end > start:
                    candidates.append(rendered[start:end].rstrip())
        return candidates

    @staticmethod
    def _delivery_sequence_from_content(content: str | None) -> int:
        marker = "<!-- task-reminder-delivery:"
        if not content or marker not in content:
            return 0
        value = content.rsplit(marker, 1)[-1].split("-->", 1)[0].strip()
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _task_reminder_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._task_reminder_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._task_reminder_locks[session_id] = lock
        return lock

    @staticmethod
    def _mark_task_reminder_iteration_counted(ctx: AgentCallbackContext) -> bool:
        """Count a recovered model retry only once for its ReAct iteration."""

        iteration = getattr(
            getattr(ctx, "inputs", None),
            "react_iteration",
            ctx.extra.get("_react_iteration", 0),
        )
        counted = ctx.extra.setdefault(_TASK_REMINDER_COUNTED_ITERATIONS_KEY, set())
        if iteration in counted:
            return False
        counted.add(iteration)
        return True

    def _get_task_reminder_state(
        self,
        ctx: AgentCallbackContext,
        session_id: str,
    ) -> _TaskReminderState:
        cached = self._task_reminder_states.get(session_id)
        if cached is not None:
            return replace(cached)

        state = self._task_reminder_state_from_history(ctx, session_id)
        self._save_task_reminder_state(session_id, state)
        return state

    def _task_reminder_state_from_history(
        self,
        ctx: AgentCallbackContext,
        session_id: str,
    ) -> _TaskReminderState:
        messages = self._history_messages(ctx)
        assistant_indexes = [
            index
            for index, message in enumerate(messages)
            if isinstance(message, AssistantMessage)
        ]
        last_todo_index = self._last_todo_result_index(messages)
        turns_since_management = sum(
            index > last_todo_index for index in assistant_indexes
        )

        target_hash, latest_attachment_index = (
            self._latest_task_reminder_history_state(messages, session_id)
        )
        if target_hash:
            reminder_index = self._task_reminder_payload_index(
                messages, session_id, target_hash
            )
            turns_since_reminder = max(
                0,
                sum(index > reminder_index for index in assistant_indexes) - 1,
            )
        elif latest_attachment_index >= 0:
            turns_since_reminder = sum(
                index > latest_attachment_index for index in assistant_indexes
            )
        else:
            turns_since_reminder = len(assistant_indexes)

        return _TaskReminderState(
            turns_since_task_management=turns_since_management,
            turns_since_task_reminder=turns_since_reminder,
        )

    @staticmethod
    def _history_messages(ctx: AgentCallbackContext) -> list:
        context = getattr(ctx, "context", None)
        get_messages = getattr(context, "get_messages", None)
        if not callable(get_messages):
            return []
        return list(get_messages(with_history=True))

    @staticmethod
    def _is_attachment_history_message(message, session_id: str) -> bool:
        metadata = getattr(message, "metadata", {}) or {}
        if not metadata.get(PROMPT_ATTACHMENT_HISTORY_METADATA_KEY):
            return False
        history_session_id = metadata.get(_TASK_REMINDER_HISTORY_SESSION_KEY)
        return history_session_id is None or str(history_session_id) == session_id

    def _latest_task_reminder_history_state(
        self,
        messages: list,
        session_id: str,
    ) -> tuple[str, int]:
        current_hash = ""
        last_change_index = -1
        state_seen = False
        for index, message in enumerate(messages):
            if not self._is_attachment_history_message(message, session_id):
                continue
            history_state = (getattr(message, "metadata", {}) or {}).get(
                _TASK_REMINDER_HISTORY_STATE_KEY
            )
            if isinstance(history_state, dict):
                next_hash = str(history_state.get(_TASK_REMINDER_SECTION, ""))
                if next_hash != current_hash and (state_seen or next_hash):
                    last_change_index = index
                current_hash = next_hash
                state_seen = True
        return current_hash, last_change_index

    def _task_reminder_payload_index(
        self,
        messages: list,
        session_id: str,
        target_hash: str,
    ) -> int:
        truncated_index = -1
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not self._is_attachment_history_message(message, session_id):
                continue
            rendered = getattr(message, "content", "")
            if not isinstance(rendered, str):
                continue
            if truncated_index < 0 and (
                _TASK_REMINDER_CURRENT_PREFIX in rendered
                or _TASK_REMINDER_LEGACY_PREFIX in rendered
            ):
                truncated_index = index
            for content in self._task_reminder_content_candidates(rendered):
                if self._task_reminder_hash(content, session_id) == target_hash:
                    return index
        return truncated_index

    @staticmethod
    def _last_todo_result_index(messages: list) -> int:
        tool_names_by_id: dict[str, str] = {}
        for message in messages:
            if not isinstance(message, AssistantMessage):
                continue
            for tool_call in message.tool_calls or []:
                tool_call_id = str(getattr(tool_call, "id", "") or "")
                if tool_call_id:
                    tool_names_by_id[tool_call_id] = str(
                        getattr(tool_call, "name", "") or ""
                    )

        last_todo_index = -1
        for index, message in enumerate(messages):
            if not isinstance(message, ToolMessage):
                continue
            tool_name = tool_names_by_id.get(str(message.tool_call_id), "")
            if tool_name.startswith("todo_"):
                last_todo_index = index
        return last_todo_index

    def _load_task_reminder_state(self, session_id: str) -> _TaskReminderState:
        cached = self._task_reminder_states.get(session_id)
        if cached is not None:
            return replace(cached)
        return _TaskReminderState()

    def _save_task_reminder_state(
        self,
        session_id: str,
        state: _TaskReminderState,
    ) -> None:
        self._task_reminder_states[session_id] = replace(state)
        self._task_reminder_states.move_to_end(session_id)
        while len(self._task_reminder_states) > self.max_tracked_task_reminder_sessions:
            self._task_reminder_states.popitem(last=False)

    @staticmethod
    def _non_negative_int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

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
    def _build_task_reminder_content() -> str:
        return (
            "Use the task tools when the current work would benefit from tracking "
            "progress. Use todo_list to read the latest task state, todo_create "
            "to add new tasks, and todo_modify to update task status (set to "
            "in_progress when starting and completed when done). Consider "
            "cleaning up the task list if it has become stale. Ignore this "
            "periodic reminder when task tracking is not relevant, and never "
            "mention the reminder to the user."
        )

    @staticmethod
    def _build_legacy_task_reminder_content(todos: list[TodoItem]) -> str:
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
            status = (
                todo.status.value
                if hasattr(todo.status, "value")
                else str(todo.status)
            )
            task_items.append(f"#{todo.id}. [{status}] {todo.content}")
        return f"{message}\n\nHere are the existing tasks:\n\n" + "\n".join(task_items)
