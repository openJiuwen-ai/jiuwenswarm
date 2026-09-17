# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Compatibility shims for OpenJiuWen todo tools."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.harness.schema.task import TodoItem, TodoStatus
from openjiuwen.harness.tools.todo import TodoModifyTool as _OpenJiuWenTodoModifyTool


class CompatibleTodoModifyTool(_OpenJiuWenTodoModifyTool):
    """Accept deleted/canceled status updates from clients as todo mutations.

    Some clients send task deletion as an update payload with
    ``{"status": "deleted"}`` rather than the canonical
    ``{"action": "delete", "ids": [...]}``. The upstream tool rejects that
    status, leaving the task pending. This shim preserves the canonical behavior
    while treating those update statuses as delete/cancel operations.
    """

    async def _update_todos(
        self,
        session_id: str,
        todos_data: list[dict[str, Any]],
        current_todos: list[TodoItem],
    ) -> str:
        if not isinstance(todos_data, list):
            raise build_error(
                StatusCode.TOOL_TODOS_VALIDATION_INVALID,
                reason="Batch update failed: 'todos' must be a list",
            )

        todo_map = {todo.id: todo for todo in current_todos}
        deleted_ids: set[str] = set()
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
            if status_value in ("deleted", "delete"):
                deleted_ids.add(todo_id)
                continue

            current_todo = todo_map[todo_id]
            if "content" in todo_data:
                current_todo.content = todo_data["content"]
            if "activeForm" in todo_data:
                current_todo.activeForm = todo_data["activeForm"]
            if "description" in todo_data:
                current_todo.description = todo_data["description"]
            if "status" in todo_data:
                if status_value == "canceled":
                    status_value = "cancelled"
                current_todo.status = TodoStatus(status_value)
            if "selected_model_id" in todo_data:
                current_todo.selected_model_id = todo_data["selected_model_id"]
            updated_count += 1

        updated_todos = [todo for todo in current_todos if todo.id not in deleted_ids]
        self._validate_single_in_progress(updated_todos)
        await self.save_todos(session_id, updated_todos)

        parts: list[str] = []
        if updated_count:
            parts.append(f"Successfully updated {updated_count} task(s)")
        if deleted_ids:
            parts.append(
                f"Successfully deleted {len(deleted_ids)} task(s) (IDs: {', '.join(sorted(deleted_ids))})"
            )
        return "; ".join(parts) or "No task changes applied"


def install_todo_modify_compat_patch() -> None:
    """Patch OpenJiuWen exports so TaskPlanningRail uses the compatible tool."""
    import openjiuwen.harness.tools as tools_module
    import openjiuwen.harness.tools.todo as todo_module

    tools_module.TodoModifyTool = CompatibleTodoModifyTool
    todo_module.TodoModifyTool = CompatibleTodoModifyTool
