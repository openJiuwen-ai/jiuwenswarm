# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashMemoryTool — unified memory tool (5-in-1, mode dispatch).

Replaces the stock 5 separate memory tools with a single Tool whose `mode`
parameter dispatches to:
- write  → write_memory_with_context   (was write_memory)
- edit   → edit_memory_with_context    (was edit_memory)
- read   → read_memory_with_context    (was read_memory + memory_get)
- search → memory_search_with_context  (was memory_search)

Must be a Tool subclass (not LocalFunction): ability_manager calls
``invoke(inputs, **kwargs)`` and threads context through.

Security: paths are validated via ``validate_memory_path`` (memory/ prefix
lock, prevents traversal). Read-only mode (cron/heartbeat) rejects write/edit
while read/search remain available.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Optional

from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput

logger = logging.getLogger(__name__)

_UNIFIED_MEMORY_NAME = "memory"


def _unified_memory_description(language: str) -> str:
    if str(language).lower().startswith("en"):
        return (
            "Unified memory tool (cross-session persistence). Pick write / edit / read / search via mode.\n\n"
            "[Storage] memory/USER.md (user profile & stable preferences), memory/MEMORY.md "
            "(long-term facts & key decisions), memory/daily_memory/YYYY-MM-DD.md (daily notes & task progress).\n\n"
            "[mode=write] Write content to path; append=true to append instead of overwrite.\n"
            "[mode=edit] Replace old_text with new_text in path (old_text must be unique in the file).\n"
            "[mode=read] Read memory file with offset/limit pagination.\n"
            "[mode=search] Semantic vector search by query. Requires embedding config; "
            "returns disabled if not configured.\n\n"
            "[Path security] Paths must start with memory/ (e.g. memory/USER.md, "
            "memory/daily_memory/2026-09-10.md). Paths outside this prefix are rejected.\n\n"
            "[Read-only mode] In cron/heartbeat/group-chat digital-avatar mode, "
            "write/edit are rejected (read/search still work).\n\n"
            "Prefer this tool (mode=write/edit) for any memory file write/edit; "
            "do NOT use edit_file/write_file for memory/ paths — this tool validates paths "
            "and prevents traversal, the general file tools do not."
        )
    return (
        "统一记忆工具（跨会话持久化）。用 mode 选写、编辑、读取或语义检索。\n\n"
        "【存储】memory/USER.md（用户画像、稳定偏好）、memory/MEMORY.md（长期事实、重要决策）、"
        "memory/daily_memory/YYYY-MM-DD.md（每日记录、任务进展）。\n\n"
        "【mode=write】把 content 写入 path；append=true 追加而非覆盖。\n"
        "【mode=edit】在 path 文件中将 old_text 精确替换为 new_text（old_text 须在该文件中唯一出现）。\n"
        "【mode=read】分页读取记忆文件（offset/limit）。\n"
        "【mode=search】按 query 语义检索记忆（向量相似度）。依赖 embedding 配置；未配置时返回 disabled。\n\n"
        "【路径安全】path 必须以 memory/ 开头（如 memory/USER.md、memory/daily_memory/2026-09-10.md）。"
        "越界路径会被拒绝。\n\n"
        "【只读态】定时任务/心跳/群聊数字分身模式下，write/edit 被拒（read/search 可用）。\n\n"
        "记忆文件的写、改一律用本工具（mode=write/edit），不要用 edit_file/write_file——"
        "本工具带路径校验（锁死 memory/、防越权），通用文件工具无此保护。"
    )


def _unified_memory_input_params(language: str) -> dict:
    en = str(language).lower().startswith("en")

    def d(cn: str, en_: str) -> str:
        return en_ if en else cn

    return {
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["write", "edit", "read", "search"],
                "description": d(
                    "操作模式：write=写入/追加；edit=精确替换；read=分页读取；search=语义检索",
                    "write=write/append; edit=exact replace; read=paginated read; search=semantic search",
                ),
            },
            "path": {
                "type": "string",
                "description": d(
                    "记忆文件相对路径，以 memory/ 开头（write/edit/read 必填；search 不需要）",
                    "Memory file relative path starting with memory/ (required for write/edit/read; not for search)",
                ),
            },
            "content": {
                "type": "string",
                "description": d("mode=write 时必填：写入的内容", "Required for write: content to write"),
            },
            "append": {
                "type": "boolean",
                "default": False,
                "description": d(
                    "mode=write 时：true 追加而非覆盖，默认 false",
                    "For write: true to append instead of overwrite (default false)",
                ),
            },
            "old_text": {
                "type": "string",
                "description": d(
                    "mode=edit 时必填：待替换的原文（须在该文件中唯一出现）",
                    "Required for edit: text to replace (must be unique in file)",
                ),
            },
            "new_text": {
                "type": "string",
                "description": d("mode=edit 时必填：替换后的新文本", "Required for edit: replacement text"),
            },
            "offset": {
                "type": "integer",
                "description": d("mode=read 时：起始行号（默认从头读）", "For read: starting line (default from beginning)"),
            },
            "limit": {
                "type": "integer",
                "description": d("mode=read 时：读取行数上限", "For read: max lines to read"),
            },
            "query": {
                "type": "string",
                "description": d("mode=search 时必填：自然语言检索词", "Required for search: natural language query"),
            },
            "max_results": {
                "type": "integer",
                "description": d("mode=search 时：返回条数上限", "For search: max results"),
            },
            "min_score": {
                "type": "number",
                "description": d("mode=search 时：最低相关分阈值", "For search: minimum relevance score"),
            },
        },
    }


class FlashMemoryTool(Tool):
    """Unified memory tool (5-in-1): mode dispatch with path security + read-only guard."""

    TOOL_NAME = "memory"
    TOOL_ID = "FlashMemoryTool"

    def __init__(self, tool_ctx, read_only_flag=None, language: str = "cn", agent_id=None):
        card = ToolCard(
            id=f"{self.TOOL_ID}_{agent_id}" if agent_id else self.TOOL_ID,
            name=self.TOOL_NAME,
            description=_unified_memory_description(language),
            input_params=_unified_memory_input_params(language),
        )
        super().__init__(card)
        self._ctx = tool_ctx
        self._read_only_flag = read_only_flag  # callable returning bool, or None

    def _is_read_only(self) -> bool:
        if callable(self._read_only_flag):
            try:
                return bool(self._read_only_flag())
            except Exception:
                return False
        return False

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> ToolOutput:
        from openjiuwen.core.memory.lite import memory_tool_ops

        data = dict(inputs or {})
        mode = str(data.get("mode") or "").strip().lower()

        try:
            if mode == "write":
                if self._is_read_only():
                    return ToolOutput(
                        success=False,
                        error="只读模式下禁止写入记忆（定时任务/心跳/群聊数字分身）。Read-only mode rejects write.",
                    )
                path = str(data.get("path") or "").strip()
                content = data.get("content")
                if not path:
                    return ToolOutput(success=False, error="path is required for mode=write")
                if content is None:
                    return ToolOutput(success=False, error="content is required for mode=write")
                result = await memory_tool_ops.write_memory_with_context(
                    self._ctx, path, str(content), append=bool(data.get("append", False))
                )
                return ToolOutput(
                    success=bool(result.get("success", False)),
                    data=result,
                    error=result.get("error"),
                )

            if mode == "edit":
                if self._is_read_only():
                    return ToolOutput(
                        success=False,
                        error="只读模式下禁止编辑记忆。Read-only mode rejects edit.",
                    )
                path = str(data.get("path") or "").strip()
                old_text = data.get("old_text")
                new_text = data.get("new_text")
                if not path:
                    return ToolOutput(success=False, error="path is required for mode=edit")
                if old_text is None or new_text is None:
                    return ToolOutput(
                        success=False, error="old_text and new_text are required for mode=edit"
                    )
                result = await memory_tool_ops.edit_memory_with_context(
                    self._ctx, path, str(old_text), str(new_text)
                )
                return ToolOutput(
                    success=bool(result.get("success", False)),
                    data=result,
                    error=result.get("error"),
                )

            if mode == "read":
                path = str(data.get("path") or "").strip()
                if not path:
                    return ToolOutput(success=False, error="path is required for mode=read")
                result = await memory_tool_ops.read_memory_with_context(
                    self._ctx,
                    path,
                    offset=data.get("offset"),
                    limit=data.get("limit"),
                )
                return ToolOutput(
                    success=bool(result.get("success", False)),
                    data=result,
                    error=result.get("error"),
                )

            if mode == "search":
                query = str(data.get("query") or "").strip()
                if not query:
                    return ToolOutput(success=False, error="query is required for mode=search")
                result = await memory_tool_ops.memory_search_with_context(
                    self._ctx,
                    query,
                    max_results=data.get("max_results"),
                    min_score=data.get("min_score"),
                )
                disabled = bool(result.get("disabled", False))
                if disabled:
                    return ToolOutput(
                        success=False,
                        error=str(
                            result.get("error")
                            or "Semantic search is disabled (embedding not configured). "
                            "Use memory(mode=read) or read_file/grep to find specific content."
                        ),
                    )
                return ToolOutput(success=True, data=result)

            return ToolOutput(success=False, error=f"unsupported memory mode: {mode!r}")

        except Exception as exc:
            logger.warning("[FlashMemoryTool] invoke failed (mode=%s): %s", mode, exc)
            return ToolOutput(success=False, error=str(exc))

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        yield await self.invoke(inputs, **kwargs)


__all__ = ["FlashMemoryTool"]
