# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashRead extension entry — discovered by ExtensionLoader at startup.

Gate: FLASH_ENABLED=1 (shared) or FLASH_READ_ENABLED=1.
When enabled, runtime-patches ``openjiuwen.harness.tools.filesystem.ReadFileTool``
so that SlimSysOperationRail / SysOperationRail pick up FlashReadFileTool in
``init()``:

1. Single file_path: full backward compat with stock ReadFileTool
2. Multiple file_paths: parallel reading via asyncio.gather (up to 10 files)
3. Per-file type detection (text/image/PDF/notebook/office) and error isolation
4. Read state registry updated per text file

SlimSysOperationRail and stock SysOperationRail both import ReadFileTool lazily
inside ``init()`` — patching the module attribute before any adapter is
created ensures the enhanced subclass is picked up at rail-build time.

Zero modification to any stock jiuwenswarm source file — all wiring is
monkey-patched here at startup, before any adapter is created.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PATCH_APPLIED = False


def _is_enabled() -> bool:
    raw = os.getenv("FLASH_ENABLED", "").strip().lower()
    if raw:
        return raw in ("1", "true", "yes", "on")
    raw = os.getenv("FLASH_READ_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _patch_read_file_tool_class() -> None:
    """Patch filesystem.ReadFileTool to FlashReadFileTool.

    Both SlimSysOperationRail and stock SysOperationRail do a lazy import
    inside ``init()``::

        from openjiuwen.harness.tools.filesystem import ReadFileTool

    By replacing the module attribute before any rail ``init()`` runs,
    the enhanced subclass is transparently picked up.
    """
    import openjiuwen.harness.tools.filesystem as _fs_module
    from jiuwenswarm.extensions.flash_read.flash_read_tool import FlashReadFileTool

    if getattr(_fs_module.ReadFileTool, "__name__", "") == "FlashReadFileTool":
        logger.info("[FlashRead] ReadFileTool already patched, skip")
        return

    _fs_module.ReadFileTool = FlashReadFileTool
    logger.info(
        "[FlashRead] patched filesystem.ReadFileTool -> FlashReadFileTool "
        "(parallel multi-file reading)"
    )


async def register_extensions(registry):
    """ExtensionLoader entry point."""
    global _PATCH_APPLIED

    if not _is_enabled():
        logger.info("[FlashRead] disabled (FLASH_ENABLED / FLASH_READ_ENABLED not set)")
        return []

    if _PATCH_APPLIED:
        return []

    try:
        _patch_read_file_tool_class()
        _PATCH_APPLIED = True
        logger.info(
            "[FlashRead] switch ARMED: enhanced read_file tool "
            "(parallel multi-file reading)"
        )
    except Exception as exc:
        logger.warning("[FlashRead] setup failed: %s", exc)

    return []
