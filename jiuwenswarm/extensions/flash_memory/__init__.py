# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashMemory extension: unified memory tool (5-in-1) as a loadable extension.

When FLASH_MEMORY_ENABLED=1, runtime-patches MemoryRail._register_memory_tools
to register a single unified `memory` tool (mode dispatch: write / edit / read /
search) instead of the stock 5 separate tools. Also patches the memory section
prompt text and the interface_deep group-chat/disable tool-name linkage.

Zero modification to any stock jiuwenswarm source file.
"""

from jiuwenswarm.extensions.flash_memory.extension import register_extensions

__all__ = ["register_extensions"]
