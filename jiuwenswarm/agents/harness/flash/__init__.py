# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""flash 模式工具面包：统一/精简工具类（tools/）与配套 rail（rails/）。

由 :class:`JiuwenSwarmFlashAdapter
<jiuwenswarm.server.runtime.agent_adapter.interface_flash.JiuwenSwarmFlashAdapter>`
的各 override 接线（todo / memory / 文件系统 / skill 发现），normal 会话
不经过本包。
"""

from .rails import FlashMemoryRail, FlashTodoRail, SlimSysOperationRail
from .tools import (
    FlashGlobTool,
    FlashMemoryTool,
    FlashReadFileTool,
    SlimSkillToolkit,
    UnifiedTodoTool,
)

__all__ = [
    "FlashGlobTool",
    "FlashMemoryRail",
    "FlashMemoryTool",
    "FlashReadFileTool",
    "FlashTodoRail",
    "SlimSkillToolkit",
    "SlimSysOperationRail",
    "UnifiedTodoTool",
]
