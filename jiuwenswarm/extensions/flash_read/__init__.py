# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashRead extension: enhanced read_file tool with parallel multi-file support.

When FLASH_ENABLED=1 (or FLASH_READ_ENABLED=1), runtime-patches the filesystem
module so SlimSysOperationRail / SysOperationRail pick up FlashReadFileTool
instead of stock ReadFileTool:

- Single file_path: identical to stock ReadFileTool (full backward compat)
- Multiple file_paths: parallel reading via asyncio.gather (up to 10 files)
- Each file independently typed (text/image/PDF/notebook/office) and read
- Per-file errors isolated (one failure does not kill the batch)
- Read state registry updated per text file for EditFileTool/WriteFileTool

Zero modification to any stock jiuwenswarm source file.
"""

from jiuwenswarm.extensions.flash_read.extension import register_extensions

__all__ = ["register_extensions"]
