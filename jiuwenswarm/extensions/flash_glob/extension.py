# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashGlob extension entry — discovered by ExtensionLoader at startup.

Gate: FLASH_ENABLED=1 (shared) or FLASH_GLOB_ENABLED=1.
When enabled, runtime-patches ``openjiuwen.harness.tools.filesystem.GlobTool``
so that SlimSysOperationRail / SysOperationRail pick up FlashGlobTool in
``init()``:

1. Results sorted by modification time (newest first)
2. .gitignore patterns from search root applied as exclusions
3. ``exclude_patterns`` parameter exposed in schema
4. Improved description with use-case guidance

SlimSysOperationRail and stock SysOperationRail both import GlobTool lazily
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
    raw = os.getenv("FLASH_GLOB_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _patch_glob_tool_class() -> None:
    """Patch filesystem.GlobTool to FlashGlobTool.

    Both SlimSysOperationRail and stock SysOperationRail do a lazy import
    inside ``init()``::

        from openjiuwen.harness.tools.filesystem import GlobTool

    By replacing the module attribute before any rail ``init()`` runs,
    the enhanced subclass is transparently picked up.
    """
    import openjiuwen.harness.tools.filesystem as _fs_module
    from jiuwenswarm.extensions.flash_glob.flash_glob_tool import FlashGlobTool

    if getattr(_fs_module.GlobTool, "__name__", "") == "FlashGlobTool":
        logger.info("[FlashGlob] GlobTool already patched, skip")
        return

    _fs_module.GlobTool = FlashGlobTool
    logger.info(
        "[FlashGlob] patched filesystem.GlobTool -> FlashGlobTool "
        "(mtime sorting + gitignore + exclude_patterns)"
    )


async def register_extensions(registry):
    """ExtensionLoader entry point."""
    global _PATCH_APPLIED

    if not _is_enabled():
        logger.info("[FlashGlob] disabled (FLASH_ENABLED / FLASH_GLOB_ENABLED not set)")
        return []

    if _PATCH_APPLIED:
        return []

    try:
        _patch_glob_tool_class()
        _PATCH_APPLIED = True
        logger.info(
            "[FlashGlob] switch ARMED: enhanced glob tool "
            "(mtime + gitignore + exclude_patterns)"
        )
    except Exception as exc:
        logger.warning("[FlashGlob] setup failed: %s", exc)

    return []
