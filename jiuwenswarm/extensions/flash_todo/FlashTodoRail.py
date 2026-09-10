# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashTodoRail — lightweight unified-todo rail (replaces TaskPlanningRail).

New standalone rail (does NOT inherit TaskPlanningRail / ConcurrentSafeTaskPlanningRail)
registering a single unified `todo` tool (action dispatch: create / list / modify).
The 3 stock Todo engines (TodoCreateTool / TodoListTool / CompatibleTodoModifyTool)
are instantiated but never individually registered — the unified tool dispatches
to them and threads ``session`` through (todo.json is keyed by session).

Why standalone (not a TaskPlanningRail subclass): the stock base class hard-codes
``tool_name.startswith("todo_")`` / ``== "todo_create"`` in its outer-task-loop
hooks; the unified tool name ``todo`` silently breaks those checks. Ditching the
inheritance removes the coupling entirely. What we drop (TaskPlan sync / advance
reminders / model selection) is not used by this deployment; the frontend todo
refresh chain lives in stream_event_rail / task_execution_rail, which match by
tool-name set (runtime-patched by the extension to include "todo").
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.prompts.sections.todo import build_todo_section
from openjiuwen.harness.rails.base import DeepAgentRail

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

_UNIFIED_TODO_ACTIONS = ("create", "list", "modify")
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
            "- create: create the task list (REPLACES the current list). `tasks` is a JSON array; "
            "each item requires content / activeForm / description and a short unique `id` "
            "(the id is the sole key for cross-turn status updates; do not use random strings). "
            "The first task is auto-set to in_progress; only one task can be in_progress at a time.\n"
            "- list: list all active (non-completed, non-cancelled) tasks with id/content/status.\n"
            "- modify: mutate tasks; pass `modify_action` to select the operation:\n"
            "    update (batch field updates — send ONLY changed fields, usually id + status),\n"
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
        "- create：创建任务列表（覆盖当前列表）。tasks 为 JSON 数组，每项必含 content / "
        "activeForm / description，并提供简短唯一的 id（id 是跨轮更新状态的唯一依据，"
        "禁止随机字符串或 UUID）。首个任务自动设为 in_progress，同一时间仅一个 in_progress。\n"
        "- list：列出全部未完成（非 completed/cancelled）任务的 id/content/status 概要。\n"
        "- modify：修改任务，用 modify_action 指定操作：\n"
        "    update（批量更新字段——只传变化字段，通常仅 id + status）、\n"
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
            "tasks": {
                "type": "array",
                "items": {"type": "object"},
                "description": d(
                    "create 必填：任务数组 [{id, content, activeForm, description, "
                    "selected_model_id?}]；覆盖当前列表",
                    "create: task array [{id, content, activeForm, description, "
                    "selected_model_id?}]; replaces the current list",
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
                "items": {"type": "object"},
                "description": d(
                    "modify_action=update/append 时：任务数据数组（update 只传变化字段）",
                    "for modify_action=update/append: task data array (update: changed fields only)",
                ),
            },
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": d(
                    "modify_action=delete/cancel 时：任务 ID 列表",
                    "for modify_action=delete/cancel: list of task ids",
                ),
            },
            "todo_data": {
                "type": "object",
                "description": d(
                    "modify_action=insert_before/insert_after 时：{target_id, items: [...]}",
                    "for modify_action=insert_before/insert_after: {target_id, items: [...]}",
                ),
            },
        },
    }


class UnifiedTodoTool(Tool):
    """Unified todo tool (3-in-1): action dispatch to create/list/modify engines.

    Must be a Tool subclass (not LocalFunction): LocalFunction.invoke calls
    ``func(**inputs)`` and drops kwargs, so ``session`` (the key todo.json is
    stored under) would be lost. A Tool subclass's ``invoke(inputs, **kwargs)``
    is called by ability_manager directly with session threaded through.
    """

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
        data = dict(inputs or {})
        action = str(data.get("action") or "").strip().lower()

        if action == "create":
            tasks = data.get("tasks")
            if not tasks or not isinstance(tasks, list):
                raise ValueError(
                    "'tasks' is required for action=create and must be a JSON array"
                )
            return await self._engines["create"].invoke({"tasks": tasks}, **kwargs)

        if action == "list":
            return await self._engines["list"].invoke({}, **kwargs)

        if action == "modify":
            modify_action = str(data.get("modify_action") or "").strip().lower()
            if not modify_action:
                raise ValueError("'modify_action' is required for action=modify")
            inner = {"action": modify_action}
            for key in ("todos", "ids", "todo_data"):
                if data.get(key) is not None:
                    inner[key] = data[key]
            return await self._engines["modify"].invoke(inner, **kwargs)

        raise ValueError(f"unsupported todo action: {action!r}")

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class FlashTodoRail(DeepAgentRail):
    """Standalone unified-todo rail (replaces ConcurrentSafeTaskPlanningRail).

    - init: build 3 Todo engines (shared TodoLockManager, never individually
      registered) and register one UnifiedTodoTool.
    - before_model_call: inject the stock todo section with tool names
      rewritten to the unified action form.
    - No outer-task-loop hooks (no after_tool_call / after_task_iteration):
      TaskPlan sync / advance reminders are intentionally dropped.

    Concurrency safety (kept from ConcurrentSafeTaskPlanningRail): check
    ``Runner.resource_mgr.get_tool`` before adding, so parallel agents sharing
    one resource id don't spam "resource already exist" errors.
    """

    priority = 90

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._tools: list = []
        self.engines: dict = {}

    def init(self, agent) -> None:
        from openjiuwen.core.runner import Runner
        from openjiuwen.harness.tools import (
            TodoCreateTool,
            TodoListTool,
            TodoModifyTool,
        )
        from openjiuwen.harness.tools.todo import TodoLockManager
        from openjiuwen.harness.workspace.workspace import WorkspaceNode

        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            logger.warning("[FlashTodoRail] agent has no ability_manager; skip")
            return

        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

        if self.workspace is None:
            deep_cfg = getattr(agent, "deep_config", None) or getattr(
                agent, "_deep_config", None
            )
            workspace = getattr(deep_cfg, "workspace", None) if deep_cfg else None
            if workspace is not None:
                self.set_workspace(workspace)
        if self.workspace is None or self.sys_operation is None:
            logger.warning("[FlashTodoRail] init skip: no workspace/sys_operation")
            return

        workspace_dir = str(self.workspace.get_node_path(WorkspaceNode.TODO))
        agent_id = getattr(getattr(agent, "card", None), "id", None)
        language = (
            self.system_prompt_builder.language
            if self.system_prompt_builder
            else "cn"
        )

        shared_lock = TodoLockManager()
        engines = {
            "create": TodoCreateTool(
                self.sys_operation, workspace_dir, language, agent_id, shared_lock
            ),
            "list": TodoListTool(
                self.sys_operation, workspace_dir, language, agent_id, shared_lock
            ),
            # CompatibleTodoModifyTool (installed via install_todo_modify_compat_patch
            # at import time in interface_deep) tolerates deleted/canceled status
            # variants from clients.
            "modify": TodoModifyTool(
                self.sys_operation, workspace_dir, language, agent_id, shared_lock
            ),
        }
        self.engines = engines
        unified_tool = UnifiedTodoTool(engines, language, agent_id)

        try:
            if Runner.resource_mgr.get_tool(unified_tool.card.id) is None:
                Runner.resource_mgr.add_tool(unified_tool)
            ability_manager.add_ability(unified_tool.card, unified_tool)
            self._tools.append(unified_tool)
            logger.info(
                "[FlashTodoRail] unified todo tool registered "
                "(engines: create/list/modify, 3-in-1)"
            )
        except Exception as exc:
            logger.warning("[FlashTodoRail] register unified todo failed: %s", exc)

    def uninit(self, agent) -> None:
        try:
            if self.system_prompt_builder:
                self.system_prompt_builder.remove_section(SectionName.TODO)
            ability_manager = getattr(agent, "ability_manager", None)
            if ability_manager and self._tools:
                for tool in self._tools:
                    name = getattr(tool.card, "name", None)
                    if name:
                        try:
                            ability_manager.remove_ability(name)
                        except Exception:
                            pass
        except Exception as exc:
            logger.warning("[FlashTodoRail] uninit failed: %s", exc)

    async def before_model_call(self, ctx) -> None:
        """Inject the todo anchor section with unified tool names."""
        if self.system_prompt_builder is None:
            return
        section = build_todo_section(
            language=self.system_prompt_builder.language,
        )
        if section is not None:
            adapted = section
            content = getattr(section, "content", None)
            if isinstance(content, dict):
                new_content = {}
                for lang_key, text in content.items():
                    if isinstance(text, str):
                        for old, new in _TODO_NAME_REPLACEMENTS:
                            text = text.replace(old, new)
                    new_content[lang_key] = text
                try:
                    adapted = type(section)(
                        name=section.name,
                        content=new_content,
                        priority=section.priority,
                    )
                except Exception:
                    adapted = section
            self.system_prompt_builder.add_section(adapted)
        else:
            self.system_prompt_builder.remove_section(SectionName.TODO)


__all__ = ["FlashTodoRail", "UnifiedTodoTool"]
