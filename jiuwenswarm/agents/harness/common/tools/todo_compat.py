# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Compatibility shims for OpenJiuWen todo tools."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Optional

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.harness.schema.task import TodoItem, TodoStatus
from openjiuwen.harness.tools.todo import TodoModifyTool as _OpenJiuWenTodoModifyTool

from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    clear_persistent_todo,
    is_backlog_todo_query,
    is_persistent_todo,
    mark_persistent_todo,
    read_turn_user_query,
    user_query_drops_all_reminders,
    user_query_drops_reminder,
)

logger = logging.getLogger(__name__)

# 跟这次 modify 调用走，不挂在工具实例上，避免并发请求互相覆盖。
_TURN_QUERY: ContextVar[str] = ContextVar("todo_reminder_drop_query", default="")
_KEPT_REMINDER_NOTE = (
    "Kept {count} reminder(s). They stay listed until the user asks to delete that reminder"
)


def _query_names_todo(query: str, todo: TodoItem) -> bool:
    """True when the user quoted this reminder's id or full content."""
    text = query or ""
    todo_id = todo.id or ""
    if len(todo_id) >= 4 and todo_id in text:
        return True
    content = (todo.content or "").strip()
    return len(content) >= 4 and content in text


def _keep_persistent_reminder(todo: TodoItem) -> bool:
    """Keep a reminder unless this turn names it, or asks to clear them all."""
    if not is_persistent_todo(todo):
        return False
    query = _TURN_QUERY.get()
    if not user_query_drops_reminder(query):
        return True
    if _query_names_todo(query, todo):
        return False
    return not user_query_drops_all_reminders(query)


class CompatibleTodoModifyTool(_OpenJiuWenTodoModifyTool):
    """Accept deleted/canceled status updates from clients as todo mutations.

    Some clients send task deletion as an update payload with
    ``{"status": "deleted"}`` rather than the canonical
    ``{"action": "delete", "ids": [...]}`. The upstream tool rejects that
    status, leaving the task pending. This shim preserves the canonical behavior
    while treating those update statuses as delete/cancel operations.
    """

    async def invoke(self, inputs: Any, **kwargs: Any) -> Any:
        session = kwargs.get("session")
        query = read_turn_user_query(session) if session is not None else ""
        token = _TURN_QUERY.set(query)
        try:
            return await super().invoke(inputs, **kwargs)
        finally:
            _TURN_QUERY.reset(token)

    async def _delete_todos(
        self,
        session_id: str,
        ids: list[str],
        current_todos: list[TodoItem],
    ) -> str:
        delete_ids = set(ids)
        remaining: list[TodoItem] = []
        deleted: list[str] = []
        kept: list[str] = []
        for todo in current_todos:
            if todo.id not in delete_ids:
                remaining.append(todo)
                continue
            if _keep_persistent_reminder(todo):
                remaining.append(todo)
                kept.append(todo.id)
                continue
            deleted.append(todo.id)
        if deleted:
            await self.save_todos(session_id, remaining)
        return _drop_result("deleted", ids, deleted, kept)

    async def _cancel_todos(
        self,
        session_id: str,
        ids: list[str],
        current_todos: list[TodoItem],
    ) -> str:
        cancel_ids = set(ids)
        cancelled: list[str] = []
        kept: list[str] = []
        for todo in current_todos:
            if todo.id not in cancel_ids:
                continue
            if _keep_persistent_reminder(todo):
                kept.append(todo.id)
                continue
            todo.status = TodoStatus.CANCELLED
            cancelled.append(todo.id)
        if cancelled:
            await self.save_todos(session_id, current_todos)
        return _drop_result("cancelled", ids, cancelled, kept)

    async def _update_todos(
        self,
        session_id: str,
        todos_data: list[dict[str, Any]],
        current_todos: list[TodoItem],
        *,
        generation_token: Optional[str] = None,
    ) -> str:
        if not isinstance(todos_data, list):
            raise build_error(
                StatusCode.TOOL_TODOS_VALIDATION_INVALID,
                reason="Batch update failed: 'todos' must be a list",
            )

        todo_map = {todo.id: todo for todo in current_todos}
        deleted_ids: set[str] = set()
        kept_ids: list[str] = []
        updated_count = 0

        for todo_data in todos_data:
            todo_id = todo_data.get("id")
            if not todo_id:
                raise build_error(
                    StatusCode.TOOL_TODOS_VALIDATION_INVALID,
                    reason="Batch update failed: Missing required field: 'id'",
                )
            if todo_id not in todo_map:
                raise build_error(
                    StatusCode.TOOL_TODOS_VALIDATION_INVALID,
                    reason=f"Batch update failed: Task with ID '{todo_id}' not found",
                )

            status_value = todo_data.get("status")
            if status_value == "canceled":
                status_value = "cancelled"
            current_todo = todo_map[todo_id]
            dropping = status_value in ("deleted", "delete", "cancelled")
            if dropping and _keep_persistent_reminder(current_todo):
                kept_ids.append(todo_id)
                continue
            if status_value in ("deleted", "delete"):
                deleted_ids.add(todo_id)
                continue
            if "content" in todo_data:
                current_todo.content = todo_data["content"]
            if "activeForm" in todo_data:
                current_todo.activeForm = todo_data["activeForm"]
            if "description" in todo_data:
                current_todo.description = todo_data["description"]
            if "status" in todo_data:
                current_todo.status = TodoStatus(status_value)
                if status_value == "in_progress":
                    clear_persistent_todo(current_todo)
            if "selected_model_id" in todo_data:
                current_todo.selected_model_id = todo_data["selected_model_id"]
            # 本轮触碰即归本轮：与 agent-core TodoModifyTool._update_todos 对齐，
            # update 重打 generation_token（token 为空时保留原标记）。
            if generation_token:
                current_todo.generation_token = generation_token
            updated_count += 1

        updated_todos = [todo for todo in current_todos if todo.id not in deleted_ids]
        self._validate_single_in_progress(updated_todos, generation_token)
        await self.save_todos(session_id, updated_todos)

        parts: list[str] = []
        if updated_count:
            parts.append(f"Successfully updated {updated_count} task(s)")
        if deleted_ids:
            parts.append(
                f"Successfully deleted {len(deleted_ids)} task(s) (IDs: {', '.join(sorted(deleted_ids))})"
            )
        if kept_ids:
            parts.append(_KEPT_REMINDER_NOTE.format(count=len(kept_ids)))
        return "; ".join(parts) or "No task changes applied"


_REMINDER_CREATE_SUFFIX = (
    "\n\nStatus is pending. This reminder stays listed until the user "
    "deletes it or asks to start it."
)


def _session_id_of(session: Any) -> str | None:
    getter = getattr(session, "get_session_id", None)
    if not callable(getter):
        return None
    value = getter()
    if isinstance(value, str) and value:
        return value
    return None


def _strip_execute_now(message: str) -> str:
    kept: list[str] = []
    for line in message.splitlines():
        if line.startswith("Next step:"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


async def persist_backlog_create(tool: Any, session: Any, result: Any) -> Any:
    """Mark a create-only reminder pending and stop the execute-now hint."""
    if not isinstance(result, dict):
        return result
    if not is_backlog_todo_query(read_turn_user_query(session)):
        return result
    session_id = _session_id_of(session)
    if session_id is None:
        return result
    # todo_create already replaced the file with this call's tasks.
    todos = await tool.load_todos(session_id)
    changed = False
    for todo in todos:
        if is_persistent_todo(todo):
            continue
        mark_persistent_todo(todo)
        todo.status = TodoStatus.PENDING
        changed = True
    if not changed:
        return result
    await tool.save_todos(session_id, todos)
    message = result.get("message")
    if not isinstance(message, str):
        return result
    updated = dict(result)
    body = _strip_execute_now(message)
    updated["message"] = f"{body}{_REMINDER_CREATE_SUFFIX}"
    return updated


def _todo_status_text(todo: TodoItem) -> str:
    status = todo.status
    if isinstance(status, TodoStatus):
        return status.value
    return str(status)


def _is_active_reminder(todo: TodoItem) -> bool:
    if not is_persistent_todo(todo):
        return False
    return _todo_status_text(todo) not in ("completed", "cancelled")


def _is_missing_todo_file(exc: Exception) -> bool:
    return "not found" in str(exc).lower()


async def _load_todos_or_empty(tool: Any, session_id: str) -> list[TodoItem]:
    """Read the current file. A missing file means there is nothing to keep."""
    try:
        loaded = await tool.load_todos(session_id)
    except Exception as exc:
        if not _is_missing_todo_file(exc):
            logger.warning("todo_create could not read existing reminders: %s", exc)
        return []
    if isinstance(loaded, list):
        return loaded
    return []


def _drop_result(verb: str, ids: list[str], changed: list[str], kept: list[str]) -> str:
    if not changed and not kept:
        joined = ", ".join(ids)
        return f"No tasks {verb}: None of the provided IDs ({joined}) were found"
    parts: list[str] = []
    if changed:
        parts.append(
            f"Successfully {verb} {len(changed)} task(s) (IDs: {', '.join(changed)})"
        )
    if kept:
        parts.append(_KEPT_REMINDER_NOTE.format(count=len(kept)))
    return "; ".join(parts)


async def restore_persistent_reminders(
    tool: Any,
    session_id: str,
    before: list[TodoItem],
) -> None:
    """Put still-active reminders back after todo_create replaces the file."""
    preserved = [todo for todo in before if _is_active_reminder(todo)]
    if not preserved:
        return
    current = await tool.load_todos(session_id)
    seen = {todo.id for todo in current if todo.id}
    extras = [todo for todo in preserved if todo.id and todo.id not in seen]
    if not extras:
        return
    await tool.save_todos(session_id, [*current, *extras])


async def merge_persistent_list_items(tool: Any, session: Any, result: Any) -> Any:
    """Put reminders back into todo_list after the generation filter drops them."""
    if not isinstance(result, dict):
        return result
    session_id = _session_id_of(session)
    if session_id is None:
        return result
    tasks = result.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    seen: set[str] = set()
    for item in tasks:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            seen.add(item["id"])
    extras: list[dict[str, Any]] = []
    todos = await tool.load_todos(session_id)
    for todo in todos:
        entry = _persistent_list_entry(todo)
        if entry is None or entry["id"] in seen:
            continue
        extras.append(entry)
        seen.add(entry["id"])
    if not extras:
        return result
    merged = dict(result)
    merged["tasks"] = [*tasks, *extras]
    return merged


def _persistent_list_entry(todo: TodoItem) -> dict[str, Any] | None:
    if not is_persistent_todo(todo):
        return None
    status = todo.status.value if isinstance(todo.status, TodoStatus) else str(todo.status)
    if status in ("completed", "cancelled"):
        return None
    if not todo.id:
        return None
    depends = todo.depends_on if isinstance(todo.depends_on, list) else []
    return {
        "id": todo.id,
        "content": todo.content,
        "status": status,
        "depends_on": depends,
    }


def install_todo_modify_compat_patch() -> None:
    """Patch OpenJiuWen exports so TaskPlanningRail uses the compatible tool."""
    import openjiuwen.harness.tools as tools_module
    import openjiuwen.harness.tools.todo as todo_module
    from openjiuwen.harness.tools.todo import TodoCreateTool, TodoListTool

    tools_module.TodoModifyTool = CompatibleTodoModifyTool
    todo_module.TodoModifyTool = CompatibleTodoModifyTool
    _install_backlog_create_patch(TodoCreateTool)
    _install_backlog_list_patch(TodoListTool)


def _install_backlog_create_patch(tool_cls: type) -> None:
    if getattr(tool_cls.invoke, "backlog_create_patched", False):
        return
    original = tool_cls.invoke

    async def invoke(self: Any, inputs: Any, **kwargs: Any) -> Any:
        session = kwargs.get("session")
        session_id = _session_id_of(session)
        before: list[TodoItem] = []
        if session_id is not None:
            before = await _load_todos_or_empty(self, session_id)
        result = await original(self, inputs, **kwargs)
        if session_id is not None:
            await restore_persistent_reminders(self, session_id, before)
        return await persist_backlog_create(self, session, result)

    invoke.backlog_create_patched = True  # type: ignore[attr-defined]
    tool_cls.invoke = invoke


def _install_backlog_list_patch(tool_cls: type) -> None:
    if getattr(tool_cls.invoke, "backlog_list_patched", False):
        return
    original = tool_cls.invoke

    async def invoke(self: Any, inputs: Any, **kwargs: Any) -> Any:
        result = await original(self, inputs, **kwargs)
        return await merge_persistent_list_items(self, kwargs.get("session"), result)

    invoke.backlog_list_patched = True  # type: ignore[attr-defined]
    tool_cls.invoke = invoke
