# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashTodoRail —— 统一 todo 工具的挂载 rail（原与 UnifiedTodoTool 同文件，拆分迁入）。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.prompts.sections.todo import build_todo_section
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.schema.task import TodoStatus
from openjiuwen.harness.tools.todo_store import (
    bind_todo_batch,
    prepare_todo_batch,
    reset_todo_binding,
    snapshot_from_result,
)

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    _todo_close_nudge_message,
    is_todo_close_nudge,
)
from jiuwenswarm.agents.harness.common.rails.todo_close_gate import TodoCloseGate
from jiuwenswarm.agents.harness.flash.tools.flash_todo import (
    _TODO_NAME_REPLACEMENTS,
    _UNIFIED_TODO_NAME,
    UnifiedTodoTool,
)
from jiuwenswarm.common.utils import get_agent_sessions_dir

logger = logging.getLogger(__name__)


def bind_todo_lifecycle(tool):
    """Bind the host's session record to the SDK transaction boundary."""
    from jiuwenswarm.server.runtime.session.session_metadata import (
        check_todo_initialization,
    )

    async def guard(session_id, snapshot, *, committing=False):
        root = get_agent_sessions_dir()
        await asyncio.to_thread(check_todo_initialization, session_id, file_exists=snapshot.exists,
                                committing=committing, sessions_root=root)

    tool.snapshot_guard = guard


class FlashTodoRail(DeepAgentRail):
    """Standalone unified-todo rail (replaces ConcurrentSafeTaskPlanningRail).

    - init: build 4 Todo engines (shared TodoLockManager, never individually
      registered) and register one UnifiedTodoTool.
    - before_model_call: inject the stock todo section with tool names
      rewritten to the unified action form.
    - No outer-task-loop hooks (no after_task_iteration): TaskPlan sync /
      advance reminders are intentionally dropped.
    - Close nudge (``TodoCloseGate``): once this invoke has created or modified
      todos, a text-only ending while an item is still in_progress is swapped
      for one internal ``todo`` call asking the model to close it — otherwise
      the frontend task list stays "in progress" after delivery.

    Concurrency safety: registration goes through ``add_ability`` alone, which
    rebinds the resource-manager entry with ``refresh=True`` atomically (later
    owner wins) — no racy get-then-add against the process-global registry.
    """

    priority = 90
    inherit_to_subagents = False

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._tools: list = []
        self.engines: dict = {}
        self._agent: Any = None
        self._close_gate = TodoCloseGate(_UNIFIED_TODO_NAME)
        self._open_todos: list[tuple[str, dict[str, Any]]] = []
        self._todo_revision = -1
        self._warned_unknown_iteration = False

    def init(self, agent) -> None:
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

        self._agent = agent
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
        for engine in engines.values():
            bind_todo_lifecycle(engine)
        unified_tool = UnifiedTodoTool(engines, language, agent_id)

        try:
            # 统一卡是 stateful（card.stateless 默认 False），add_ability 内部即
            # 以 refresh=True 在 resource_mgr 原子重绑（后注册者胜）——不在这里
            # 先 get_tool/add_tool：那对全局注册表是非原子的检查后写入，并发
            # 建同名 agent 时会竞出 "already exist"。
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

    async def before_invoke(self, ctx) -> None:
        reset_todo_binding()
        if "create" in self.engines and ctx.session is not None:
            self.engines["create"].reset_invoke_marker(ctx.session.get_session_id())
        self._close_gate.reset()
        self._open_todos = []
        self._todo_revision = -1

    async def before_model_call(self, ctx) -> None:
        self._inject_todo_section()
        await self._close_gate.arm(ctx.session, can_nudge=self._can_nudge(ctx))

    async def after_model_call(self, ctx) -> None:
        prepare_todo_batch(ctx)
        dropped = False
        try:
            dropped = self._close_gate.attach(
                getattr(ctx.inputs, "response", None), can_nudge=self._can_nudge(ctx)
            )
            if dropped:
                logger.info(
                    "[FlashTodoRail] todo close nudge: open=%s",
                    [todo_id for todo_id, _ in self._open_todos],
                )
        finally:
            await self._close_gate.finish(ctx.session, dropped=dropped)

    async def before_tool_call(self, ctx) -> None:
        bind_todo_batch(ctx, self.engines.get("modify"))
        if not isinstance(ctx.inputs, ToolCallInputs) or not is_todo_close_nudge(ctx.inputs.tool_call):
            return
        msg = _todo_close_nudge_message(self._open_todos)
        for old, new in _TODO_NAME_REPLACEMENTS:
            msg = msg.replace(old, new)
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = msg
        ctx.inputs.tool_msg = ToolMessage(
            content=msg, tool_call_id=str(getattr(ctx.inputs.tool_call, "id", "") or "")
        )

    async def after_tool_call(self, ctx) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs) or ctx.inputs.tool_name != _UNIFIED_TODO_NAME:
            return
        if is_todo_close_nudge(ctx.inputs.tool_call):
            return
        if _todo_action(ctx.inputs.tool_args) not in ("create", "modify"):
            return
        snapshot = snapshot_from_result(ctx.inputs.tool_result)
        if snapshot is not None:
            if snapshot.revision < self._todo_revision:
                return
            self._todo_revision = snapshot.revision
            self._open_todos = [(row["id"], {"content": row.get("content", "")})
                                for row in snapshot.todos if row.get("status") == "in_progress"]
        else:
            self._open_todos = await self._load_open_todos(ctx.session)

    def _can_nudge(self, ctx) -> bool:
        return ctx.session is not None and bool(self._open_todos) and self._has_rounds_to_close_and_reply(ctx)

    def _has_rounds_to_close_and_reply(self, ctx) -> bool:
        iteration = int(getattr(ctx.inputs, "react_iteration", 0) or 0)
        react = getattr(self._agent, "_react_agent", None)
        max_iterations = getattr(getattr(react, "_config", None), "max_iterations", None)
        if iteration <= 0 or not isinstance(max_iterations, int) or max_iterations <= 0:
            if not self._warned_unknown_iteration:
                self._warned_unknown_iteration = True
                logger.warning(
                    "[FlashTodoRail] react iteration unknown (iteration=%s max=%r); todo close nudge disabled",
                    iteration,
                    max_iterations,
                )
            return False
        return iteration <= max_iterations - 2

    async def _load_open_todos(self, session) -> list[tuple[str, dict[str, Any]]]:
        list_engine = self.engines.get("list")
        if session is None or list_engine is None:
            return []
        try:
            items = await list_engine.load_todos(session.get_session_id())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FlashTodoRail] load todos for close nudge failed: %s", exc)
            return []
        return [
            (item.id, {"content": item.content})
            for item in items
            if item.status == TodoStatus.IN_PROGRESS
        ]

    def _inject_todo_section(self) -> None:
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


def _todo_action(tool_args: Any) -> str:
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except (TypeError, ValueError):
            return ""
    if not isinstance(tool_args, dict):
        return ""
    return str(tool_args.get("action") or "").strip().lower()


__all__ = ["FlashTodoRail"]
