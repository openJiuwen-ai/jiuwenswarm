# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""flash 模式工具面包：统一/精简工具类与配套 rail。

由 :class:`JiuwenSwarmFlashAdapter
<jiuwenswarm.server.runtime.agent_adapter.interface_flash.JiuwenSwarmFlashAdapter>`
的各 override 接线（todo / memory / 文件系统 / skill 发现），normal 会话
不经过本包。
"""

from .flash_glob_tool import FlashGlobTool
from .flash_memory_rail import FlashMemoryRail
from .flash_memory_tool import FlashMemoryTool
from .flash_read_tool import FlashReadFileTool
from .flash_todo import FlashTodoRail, UnifiedTodoTool
from .slim_skill_toolkit import SlimSkillToolkit
from .slim_sys_operation_rail import SlimSysOperationRail

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
