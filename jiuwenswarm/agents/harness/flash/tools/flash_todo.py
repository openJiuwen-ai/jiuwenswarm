# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""UnifiedTodoTool — flash 统一 todo 工具（todo_create/get/list/modify 4 合 1）。

单卡 action 分发到 4 个 stock Todo 引擎（引擎不单独注册），session 透传
（todo.json 按 session 键控）。挂载与提示注入见 flash_todo_rail.FlashTodoRail
（独立 rail，不继承 TaskPlanningRail：其外层 task-loop 钩子硬编码
``todo_`` 前缀，统一名 ``todo`` 会静默绕过这些检查；前端 todo 刷新链路在
stream_event_rail / task_execution_rail，其工具名常量已含 ``todo``）。
"""

from __future__ import annotations

import logging

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput

logger = logging.getLogger(__name__)

_UNIFIED_TODO_NAME = "todo"

# Stock section text references the old tool names; rewrite them to the unified
# action form so the guidance stays accurate without touching the venv text.
_TODO_NAME_REPLACEMENTS = (
    ("todo_create", "todo(action=create)"),
    ("todo_modify", "todo(action=modify)"),
    ("todo_list", "todo(action=list)"),
    ("todo_get", "todo(action=get)"),
)

_UNIFIED_TODO_ACTIONS = ("create", "list", "get", "modify")
_MODIFY_ACTIONS = (
    "update",
    "delete",
    "cancel",
    "append",
    "insert_after",
    "insert_before",
)


def _unified_todo_description(language: str) -> str:
    if str(language).lower().startswith("en"):
        return (
            "Unified todo management tool. Select the operation via `action`:\n"
            "- create: create a task list; active plans require explicit plan_action=new/rebuild. "
            "`tasks` is a JSON array; "
            "each item requires content / activeForm / description and a short unique `id` "
            "(the id is the sole key for cross-turn status updates; do not use random strings). "
            "The first task is auto-set to in_progress; only one task can be in_progress at a time.\n"
            "- list: list all active (non-completed, non-cancelled) tasks with id/content/status.\n"
            "- get: fetch one task's full record (content/description/status) by `id`.\n"
            "- modify: mutate tasks; pass `modify_action` to select the operation:\n"
            "    update (requires `todos`: [{id, ...changed fields}], e.g. "
            "{action: modify, modify_action: update, todos: [{id: env, status: completed}]}), "
            "not top-level id/status,\n"
            "    delete / cancel (pass `ids`: list of task ids),\n"
            "    append (add `todos` to the end),\n"
            "    insert_before / insert_after (pass `todo_data`: {target_id, items}).\n"
            "\n"
            "Rules:\n"
            "- Update status as soon as it changes; do not batch accumulated changes. A single "
            "update normally touches only the current task (→ completed) and the next (→ in_progress).\n"
            "- Before marking completed: verify the work is truly done (e.g. tests pass). Never "
            "mark completed when partially implemented or errors remain.\n"
            "- Granularity: one todo = one independently advanceable/verifiable execution stage; "
            "merge logically-continuous steps into one item; do not mechanically mirror tool calls "
            "or deliverable structure."
        )
    return (
        "待办任务统一管理工具，通过 action 指定操作：\n"
        "- create：创建任务列表；活动计划须明确传 plan_action=new/rebuild 才能替换。tasks 为 JSON 数组，每项必含 content / "
        "activeForm / description，并提供简短唯一的 id（id 是跨轮更新状态的唯一依据，"
        "禁止随机字符串或 UUID）。首个任务自动设为 in_progress，同一时间仅一个 in_progress。\n"
        "- list：列出全部未完成（非 completed/cancelled）任务的 id/content/status 概要。\n"
        "- get：按 id 查询单个任务的完整记录（content/description/status）。\n"
        "- modify：修改任务，用 modify_action 指定操作：\n"
        "    update（必传 todos 数组，每项为 id + 变化字段，例如 "
        "{action: modify, modify_action: update, todos: [{id: env, status: completed}]}；"
        "不能把 id/status 放在顶层）、\n"
        "    delete / cancel（传 ids：任务 ID 列表）、\n"
        "    append（传 todos：追加到末尾）、\n"
        "    insert_before / insert_after（传 todo_data：{target_id, items}，插到目标任务前/后）。\n"
        "\n"
        "规则：\n"
        "- 状态一旦变化立即更新，不要积攒多次变化后一起更新；单次更新通常只动当前任务"
        "（→ completed）和下一任务（→ in_progress）。\n"
        "- 标记 completed 前确认工作真正完成（如测试通过）；部分实现、有报错时不得标记 completed。\n"
        "- 任务粒度：一条 todo = 一个可独立推进/验证的执行阶段；逻辑连续的步骤合并为一条；"
        "不要把单次工具调用或最终交付物的结构机械转成 todo。"
    )


def _unified_todo_input_params(language: str) -> dict:
    en = str(language).lower().startswith("en")

    def d(cn: str, en_: str) -> str:
        return en_ if en else cn

    return {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_UNIFIED_TODO_ACTIONS),
                "description": d("要执行的操作", "Operation to execute"),
            },
            "plan_action": {"type": "string", "enum": ["new", "rebuild"]},
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "object", "required": ["id", "content"], "properties": {
                    "id": {"type": "string"}, "content": {"type": "string", "minLength": 1},
                    "activeForm": {"type": "string"}, "description": {"type": "string"},
                    "selected_model_id": {"type": "string"},
                }},
                "description": d(
                    "create 必填：任务数组 [{id, content, activeForm, description, "
                    "selected_model_id?}]；活动计划默认不覆盖",
                    "create: task array [{id, content, activeForm, description, "
                    "selected_model_id?}]; preserves an active plan by default",
                ),
            },
            "id": {
                "type": "string",
                "description": d(
                    "get 必填：要查询的任务 ID",
                    "get: the task id to fetch",
                ),
            },
            "modify_action": {
                "type": "string",
                "enum": list(_MODIFY_ACTIONS),
                "description": d(
                    "modify 必填：具体修改操作",
                    "modify: the mutation to apply",
                ),
            },
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string"},
                        "status": {"type": "string"},
                        "content": {"type": "string"},
                        "activeForm": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
                "description": d(
                    "modify_action=update/append 必填，单项更新也必须使用数组；"
                    "update 示例 [{id: env, status: completed}, {id: build, status: in_progress}]",
                    "required for modify_action=update/append, including a single update; "
                    "example [{id: env, status: completed}, {id: build, status: in_progress}]",
                ),
            },
            "ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
                "description": d(
                    "modify_action=delete/cancel 时：任务 ID 列表",
                    "for modify_action=delete/cancel: list of task ids",
                ),
            },
            "todo_data": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "minLength": 1},
                    "items": {"type": "array", "minItems": 1, "items": {
                        "type": "object", "required": ["id", "content", "status"],
                        "properties": {"id": {"type": "string"}, "content": {"type": "string"},
                                       "status": {"type": "string"}, "activeForm": {"type": "string"},
                                       "description": {"type": "string"},
                                       "selected_model_id": {"type": "string"}},
                    }},
                },
                "required": ["target_id", "items"],
                "description": d(
                    "modify_action=insert_before/insert_after 时：{target_id, items: [...]}",
                    "for modify_action=insert_before/insert_after: {target_id, items: [...]}",
                ),
            },
        },
    }


class UnifiedTodoTool(Tool):
    """Unified todo tool (4-in-1): action dispatch to create/list/get/modify engines.

    Must be a Tool subclass (not LocalFunction): LocalFunction.invoke calls
    ``func(**inputs)`` and drops kwargs, so ``session`` (the key todo.json is
    stored under) would be lost. A Tool subclass's ``invoke(inputs, **kwargs)``
    is called by ability_manager directly with session threaded through.
    """

    _log_execution_status = True

    def __init__(self, engines: dict, language: str, agent_id=None):
        card = ToolCard(
            id=f"UnifiedTodoTool_{agent_id}" if agent_id else "UnifiedTodoTool",
            name=_UNIFIED_TODO_NAME,
            description=_unified_todo_description(language),
            input_params=_unified_todo_input_params(language),
        )
        super().__init__(card)
        self._engines = engines

    async def invoke(self, inputs, **kwargs):
        # 参数错误用结构化 ToolOutput 返回（与 FlashMemoryTool 同风格），
        # 不 raise——交给模型读到错误后自行纠正参数。
        if not isinstance(inputs, dict):
            return ToolOutput(success=False, error="todo inputs must be a JSON object")
        data = dict(inputs)
        action = str(data.get("action") or "").strip().lower()

        if action == "create":
            tasks = data.get("tasks")
            if not tasks or not isinstance(tasks, list):
                return ToolOutput(
                    success=False,
                    error="'tasks' is required for action=create and must be a JSON array",
                )
            return await self._engines["create"].invoke(
                {"tasks": tasks, "plan_action": data.get("plan_action")}, **kwargs
            )

        if action == "list":
            return await self._engines["list"].invoke({}, **kwargs)

        if action == "get":
            task_id = str(data.get("id") or "").strip()
            if not task_id:
                return ToolOutput(success=False, error="'id' is required for action=get")
            return await self._engines["get"].invoke({"id": task_id}, **kwargs)

        if action == "modify":
            modify_action = str(data.get("modify_action") or "").strip().lower()
            if not modify_action:
                return ToolOutput(
                    success=False, error="'modify_action' is required for action=modify"
                )
            if modify_action in ("update", "append") and not isinstance(data.get("todos"), list):
                return ToolOutput(
                    success=False,
                    error="'todos' must be an array, including for a single task. "
                    'Update example: {"action":"modify","modify_action":"update",'
                    '"todos":[{"id":"env","status":"completed"}]}; '
                    "for append, include id/content/status in each item. "
                    "Top-level id/status does not update a task.",
                )
            inner = {"action": modify_action}
            for key in ("todos", "ids", "todo_data"):
                if data.get(key) is not None:
                    inner[key] = data[key]
            try:
                return await self._engines["modify"].invoke(inner, **kwargs)
            except BaseError as exc:
                if getattr(exc, "tool_commit_state", None) == "unknown":
                    raise
                if "More than one task is marked as 'in_progress'" not in str(exc):
                    raise
                hint = ""
                try:
                    listing = await self._engines["list"].invoke({}, **kwargs)
                    active = [row["id"] for row in listing["tasks"] if row.get("status") == "in_progress"]
                    hint = f" Current in_progress task IDs: {active}."
                except Exception:
                    logger.debug("Could not read active tasks for Todo validation feedback", exc_info=True)
                return ToolOutput(
                    success=False,
                    error=f"{exc}.{hint} The update was not committed. Complete the current task "
                    "only if its work is done, then start the next task in the same todos array; "
                    "do not assume a failed update changed task status.",
                )

        return ToolOutput(success=False, error=f"unsupported todo action: {action!r}")

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


__all__ = ["UnifiedTodoTool"]
