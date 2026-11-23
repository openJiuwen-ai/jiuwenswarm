# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Operation arguments shared by Flash Todo execution and permission projection."""

from typing import Any

TODO_ACTIONS = ("create", "list", "get", "modify")
MODIFY_ACTIONS = ("update", "delete", "cancel", "append", "insert_after", "insert_before")

_UPDATE_EXAMPLE = (
    'For a status update, use {"action":"modify","modify_action":"update",'
    '"todos":[{"id":"<task_id>","status":"<new_status>"}]} with the actual task ID and status.'
)


class TodoArgumentError(ValueError):
    """Invalid model input for a Flash Todo operation."""

    def __init__(self, message: str):
        super().__init__(
            f"[INVALID_ARGUMENT] {message} This call did not modify the todo list."
        )


def normalize_todo_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """把 create/modify 的列表键别名归一到规范键。

    create 的规范键是 ``tasks``、modify 的规范键是 ``todos``。模型混用时按当前
    action 复制一次，不删除原键也不改动调用方传入的 dict；未命中别名时原样返回，
    避免无谓拷贝。
    """
    action = str(args.get("action") or "").strip().lower()
    canonical, alias = ("tasks", "todos") if action == "create" else ("todos", "tasks")
    if isinstance(args.get(canonical), list):
        return args
    alias_value = args.get(alias)
    if not isinstance(alias_value, list) or not alias_value:
        return args
    normalized = dict(args)
    normalized[canonical] = alias_value
    return normalized


def parse_todo_operation(args: dict[str, Any]) -> tuple[str, str | None]:
    """Resolve operation names without changing the input or touching task state."""
    args = normalize_todo_arguments(args)
    action = str(args.get("action") or "").strip().lower()
    if action not in TODO_ACTIONS:
        raise TodoArgumentError(f"unsupported todo action. Use {', '.join(TODO_ACTIONS)}.")
    if action != "modify":
        return action, None

    modify_action = str(args.get("modify_action") or "").strip().lower()
    if modify_action not in MODIFY_ACTIONS:
        message = (
            "Unsupported 'modify_action'." if modify_action
            else "'modify_action' is required for action=modify."
        )
        raise TodoArgumentError(
            f"{message} Use {', '.join(MODIFY_ACTIONS)}. {_UPDATE_EXAMPLE}"
        )
    if modify_action in ("update", "append") and not isinstance(args.get("todos"), list):
        raise TodoArgumentError(
            "'todos' must be an array, including for a single task. "
            'Update example: {"action":"modify","modify_action":"update",'
            '"todos":[{"id":"env","status":"completed"}]}; '
            "for append, include id/content/status in each item. "
            "Top-level id/status does not update a task."
        )
    return action, modify_action
