# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code-mode todo tools with CC-aligned descriptions and schemas."""

from __future__ import annotations

import uuid
from typing import Optional

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.harness.tools.todo import (
    TodoCreateTool,
    TodoGetTool,
    TodoListTool,
    TodoLockManager,
    TodoModifyTool,
)

from jiuwenswarm.agents.harness.code.prompt.code_todo_tool_prompts import (
    CODE_TODO_TOOL_PROMPTS,
)
from jiuwenswarm.agents.harness.common.tools.todo_compat import core_todo_container_key


def _align_core_container(inputs, key: str):
    """按已安装 Core 的契约准备 create 入参：旧 Core 改名，双键同传拒绝。

    跨仓键统一的发布窗口内，code 模式按实际安装的 Core 取名，避免整体不可用。
    两个容器键同时出现必须拒绝：旧 Core 只读 tasks，静默转发会让落盘内容与模型
    声明的新键内容不一致。
    """
    if not isinstance(inputs, dict):
        return inputs
    if "todos" in inputs and "tasks" in inputs:
        raise build_error(
            StatusCode.TOOL_TODOS_VALIDATION_INVALID,
            reason="'todos' and 'tasks' are the same container; pass exactly one of them",
        )
    if key == "todos" or "todos" not in inputs:
        return inputs
    aligned = {name: value for name, value in inputs.items() if name != "todos"}
    aligned[key] = inputs["todos"]
    return aligned


def _build_code_todo_card(
    name: str,
    tool_id_prefix: str,
    *,
    agent_id: Optional[str] = None,
) -> ToolCard:
    description, input_params = CODE_TODO_TOOL_PROMPTS[name]
    tool_id = f"{tool_id_prefix}_{agent_id}" if agent_id else f"{tool_id_prefix}_{uuid.uuid4().hex}"
    return ToolCard(
        id=tool_id,
        name=name,
        description=description,
        input_params=input_params or {"type": "object", "properties": {}, "required": []},
    )


class CodeTodoCreateTool(TodoCreateTool):
    def __init__(
        self,
        operation: SysOperation,
        workspace: Optional[str] = None,
        language: str = "en",
        agent_id: Optional[str] = None,
        lock_manager: Optional[TodoLockManager] = None,
    ) -> None:
        _ = language
        super().__init__(operation, workspace, "en", agent_id, lock_manager)
        self._card = _build_code_todo_card("todo_create", "TodoCreateTool", agent_id=agent_id)

    async def invoke(self, inputs, **kwargs):
        # 模型面契约固定 todos；旧 Core 只认 tasks，入口处按已安装版本改名。
        return await super().invoke(_align_core_container(inputs, core_todo_container_key()), **kwargs)


class CodeTodoListTool(TodoListTool):
    def __init__(
        self,
        operation: SysOperation,
        workspace: Optional[str] = None,
        language: str = "en",
        agent_id: Optional[str] = None,
        lock_manager: Optional[TodoLockManager] = None,
    ) -> None:
        _ = language
        super().__init__(operation, workspace, "en", agent_id, lock_manager)
        self._card = _build_code_todo_card("todo_list", "TodoListTool", agent_id=agent_id)

    async def invoke(self, inputs, **kwargs):
        # 下游按 todos 读取列表；旧 Core 返回 tasks 时在此归一，保留双版本一致出口。
        result = await super().invoke(inputs, **kwargs)
        if isinstance(result, dict) and "todos" not in result and isinstance(result.get("tasks"), list):
            result["todos"] = result.pop("tasks")
        return result


class CodeTodoGetTool(TodoGetTool):
    def __init__(
        self,
        operation: SysOperation,
        workspace: Optional[str] = None,
        language: str = "en",
        agent_id: Optional[str] = None,
        lock_manager: Optional[TodoLockManager] = None,
    ) -> None:
        _ = language
        super().__init__(operation, workspace, "en", agent_id, lock_manager)
        self._card = _build_code_todo_card("todo_get", "TodoGetTool", agent_id=agent_id)


class CodeTodoModifyTool(TodoModifyTool):
    def __init__(
        self,
        operation: SysOperation,
        workspace: Optional[str] = None,
        language: str = "en",
        agent_id: Optional[str] = None,
        lock_manager: Optional[TodoLockManager] = None,
    ) -> None:
        _ = language
        super().__init__(operation, workspace, "en", agent_id, lock_manager)
        self._card = _build_code_todo_card("todo_modify", "TodoModifyTool", agent_id=agent_id)
