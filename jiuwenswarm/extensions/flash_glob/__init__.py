# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashGlob extension: enhanced glob tool with mtime sorting and gitignore filtering.

When FLASH_ENABLED=1 (or FLASH_GLOB_ENABLED=1), runtime-patches the filesystem
module so SlimSysOperationRail / SysOperationRail pick up FlashGlobTool instead
of stock GlobTool:

- Results sorted by modification time (newest first)
- .gitignore patterns from search root applied as exclusions
- exclude_patterns parameter exposed in schema
- Improved description with use-case guidance

Zero modification to any stock jiuwenswarm source file.
"""

from jiuwenswarm.extensions.flash_glob.extension import register_extensions

__all__ = ["register_extensions"]
