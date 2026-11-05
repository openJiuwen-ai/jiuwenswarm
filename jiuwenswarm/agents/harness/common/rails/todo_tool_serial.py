# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""把主 agent 的 todo 工具设为不与其他工具并行执行（供 TodoProgressRail 使用）。

todo_modify 的"读→改→存"整体没有锁，同一批并行的两个写入会互相覆盖；设为
``parallel_safe=False`` 后，调度器把每个 todo 调用当作独占屏障逐个执行，每个写入
都能读到上一个已提交的状态。其他工具不受影响，仍按原规则并行。
"""

from __future__ import annotations

from typing import Any

TODO_TOOL_NAMES = ("todo_create", "todo_list", "todo_get", "todo_modify")
TODO_WRITE_TOOLS = frozenset({"todo_create", "todo_modify"})


def serialize_todo_tools(ability_manager: Any) -> int:
    """把已注册的 todo 工具设为串行，返回找到的 todo 工具数。

    幂等，每次 invoke 调一次：配置热重载可能重新注册 ToolCard。
    """
    if ability_manager is None:
        return 0
    found = 0
    for name in TODO_TOOL_NAMES:
        card = ability_manager.get(name)
        if card is None:
            continue
        found += 1
        if getattr(card, "parallel_safe", True):
            card.parallel_safe = False
    return found


__all__ = ["TODO_TOOL_NAMES", "TODO_WRITE_TOOLS", "serialize_todo_tools"]
