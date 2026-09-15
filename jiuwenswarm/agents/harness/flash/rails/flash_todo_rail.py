# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashTodoRail —— 统一 todo 工具的挂载 rail（原与 UnifiedTodoTool 同文件，拆分迁入）。"""

from __future__ import annotations

import logging

from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.prompts.sections.todo import build_todo_section
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.flash.tools.flash_todo import (
    UnifiedTodoTool,
    _TODO_NAME_REPLACEMENTS,
)

logger = logging.getLogger(__name__)


class FlashTodoRail(DeepAgentRail):
    """Standalone unified-todo rail (replaces ConcurrentSafeTaskPlanningRail).

    - init: build 4 Todo engines (shared TodoLockManager, never individually
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
            TodoGetTool,
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
            "get": TodoGetTool(
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
                "(engines: create/list/get/modify, 4-in-1)"
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
                        except Exception as exc:
                            logger.debug(
                                "[FlashTodoRail] remove_ability %s failed: %s", name, exc
                            )
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


__all__ = ["FlashTodoRail"]
