# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm-local prompt text patches for tool results provided by dependencies."""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_PATCH_IDS_ATTR = "__jiuwenswarm_prompt_patch_ids__"

_READ_FILE_VISION_HINT = (
    "If a vision tool is configured, call image_ocr or "
    "visual_question_answering with this file path."
)
_READ_FILE_SKILL_HINT = (
    "To inspect or understand this image, load the "
    "`xiaoyi-image-understanding-win` skill and follow its instructions "
    "using this exact file path. Do not infer image content unless the "
    "skill workflow completes successfully."
)


def patch_async_dict_content(
    owner: type[Any],
    method_name: str,
    *,
    patch_id: str,
    replacements: Mapping[str, str],
) -> bool:
    """Wrap an async method and apply exact replacements to dict ``content``.

    Returns ``True`` when a wrapper is installed and ``False`` when the same
    patch has already been applied to the current method.
    """
    original = getattr(owner, method_name)
    applied_ids = frozenset(getattr(original, _PATCH_IDS_ATTR, ()))
    if patch_id in applied_ids:
        return False

    @functools.wraps(original)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        if not isinstance(result, dict) or not isinstance(result.get("content"), str):
            return result

        content = result["content"]
        for old_text, new_text in replacements.items():
            content = content.replace(old_text, new_text)
        result["content"] = content
        return result

    setattr(wrapped, _PATCH_IDS_ATTR, applied_ids | {patch_id})
    setattr(owner, method_name, wrapped)
    logger.info("Applied tool prompt patch %s to %s.%s", patch_id, owner.__name__, method_name)
    return True


def _patch_read_file_tool(read_file_tool: type[Any]) -> None:
    patch_async_dict_content(
        read_file_tool,
        "_read_image",
        patch_id="read-file-image-understanding-skill",
        replacements={_READ_FILE_VISION_HINT: _READ_FILE_SKILL_HINT},
    )


def apply_tool_prompt_patches() -> None:
    """Apply all Swarm-owned tool-result prompt patches."""
    from openjiuwen.harness.tools.filesystem import ReadFileTool

    _patch_read_file_tool(ReadFileTool)


def register_tool_prompt_import_hook() -> None:
    """Patch ReadFileTool when ``filesystem`` loads, without importing it now."""
    from jiuwenswarm.common.import_hook import register_post_import_hook

    def _on_filesystem_loaded(module: Any) -> None:
        _patch_read_file_tool(module.ReadFileTool)

    register_post_import_hook(
        "openjiuwen.harness.tools.filesystem",
        _on_filesystem_loaded,
    )


__all__ = [
    "apply_tool_prompt_patches",
    "patch_async_dict_content",
    "register_tool_prompt_import_hook",
]
