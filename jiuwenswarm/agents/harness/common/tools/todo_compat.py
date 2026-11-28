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

    It also keeps the "only one task may be ``in_progress``" invariant
    enforceable by the model. ``update`` is a partial patch over a list the
    model does not re-read, so a payload cannot demote a stale ``in_progress``
    task it does not know about, and the upstream rejection names neither the
    offending ids nor a remedy. The model then resends the same call until the
    runner's identical-tool-args bailout aborts the run. The overrides below
    break that loop.
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

            if todo_data.get("status") in ("deleted", "delete"):
                deleted_ids.add(todo_id)
                continue
            self._apply_update(todo_map[todo_id], todo_data)
            updated_count += 1

        updated_todos = [todo for todo in current_todos if todo.id not in deleted_ids]
        demotion_note = self._demote_stale_in_progress(updated_todos, todos_data)
        self._validate_single_in_progress(updated_todos)
        await self.save_todos(session_id, updated_todos)

        parts: list[str] = []
        if updated_count:
            parts.append(f"Successfully updated {updated_count} task(s)")
        if deleted_ids:
            parts.append(
                f"Successfully deleted {len(deleted_ids)} task(s) (IDs: {', '.join(sorted(deleted_ids))})"
            )
        if demotion_note:
            parts.append(demotion_note)
        return "; ".join(parts) or "No task changes applied"

    @staticmethod
    def _apply_update(todo: TodoItem, todo_data: dict[str, Any]) -> None:
        for field in ("content", "activeForm", "description", "selected_model_id"):
            if field in todo_data:
                setattr(todo, field, todo_data[field])
        if "status" in todo_data:
            status_value = todo_data["status"]
            todo.status = TodoStatus("cancelled" if status_value == "canceled" else status_value)

    @staticmethod
    def _demote_stale_in_progress(
        todos: list[TodoItem],
        todos_data: list[dict[str, Any]],
    ) -> str | None:
        """Move tasks left ``in_progress`` by an earlier turn back to ``pending``.

        Acts only when the payload leaves exactly one task ``in_progress``. A
        task counts when the payload sets its status and its merged status is
        ``in_progress``, so a later entry that deletes or moves it cancels the
        promotion. Under a single-active-task invariant that task is the one
        being worked on now, and every other ``in_progress`` task, which the
        payload does not mention, is a leftover from a transition the model
        never sent. A payload that leaves two tasks ``in_progress`` states a
        contradiction and is left for validation to reject.

        The target is ``pending`` because whether a stale task finished is
        unknown here. Returns the note that tells the model about the repair,
        or ``None`` when nothing moved.
        """
        by_id = {todo.id: todo for todo in todos}
        promoted_ids: list[str] = []
        for data in todos_data:
            todo = by_id.get(data["id"]) if "status" in data else None
            if todo is not None and todo.status == TodoStatus.IN_PROGRESS and todo.id not in promoted_ids:
                promoted_ids.append(todo.id)
        if len(promoted_ids) != 1:
            return None
        demoted_ids: list[str] = []
        for todo in todos:
            if todo.status == TodoStatus.IN_PROGRESS and todo.id != promoted_ids[0]:
                todo.status = TodoStatus.PENDING
                demoted_ids.append(todo.id)
        if not demoted_ids:
            return None
        return (
            f"Also moved {len(demoted_ids)} stale in_progress task(s) back to 'pending' "
            f"(IDs: {', '.join(demoted_ids)}) because '{promoted_ids[0]}' is now the "
            "in-progress task and only one is allowed; update them to 'completed' or "
            "'cancelled' if that is not what they should be"
        )

    def _validate_single_in_progress(self, todos_data: list[TodoItem]) -> None:
        """Reject multiple ``in_progress`` tasks with an actionable message.

        Upstream raises "More than one task is marked as 'in_progress' (only
        one allowed)", which names neither the tasks in conflict nor a way out.
        The model cannot act on it and repeats the rejected call verbatim. This
        override keeps the invariant and adds the two facts needed to fix it:
        which ids collide, and what a valid follow-up call looks like.
        """
        in_progress_ids = [
            todo.id for todo in todos_data if todo.status == TodoStatus.IN_PROGRESS
        ]
        if len(in_progress_ids) > 1:
            raise build_error(
                StatusCode.TOOL_TODOS_VALIDATION_INVALID,
                reason=(
                    "More than one task is marked as 'in_progress' (only one allowed). "
                    f"Tasks in conflict: {', '.join(in_progress_ids)}. "
                    "Retrying the same call will fail again. Keep exactly one of these ids "
                    "'in_progress' and set every other one to 'completed', 'cancelled' or "
                    "'pending': with an 'update' call for a task that already exists, and "
                    "in this call for a task that this call adds."
                ),
            )


def install_todo_modify_compat_patch() -> None:
    """Patch OpenJiuWen exports so TaskPlanningRail uses the compatible tool."""
    import openjiuwen.harness.tools as tools_module
    import openjiuwen.harness.tools.todo as todo_module

    tools_module.TodoModifyTool = CompatibleTodoModifyTool
    todo_module.TodoModifyTool = CompatibleTodoModifyTool
