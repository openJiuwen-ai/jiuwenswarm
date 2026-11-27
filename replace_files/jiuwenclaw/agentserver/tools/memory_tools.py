# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Memory tools for JiuWenClaw - Using @tool decorator for openjiuwen."""

import contextvars
import logging
import os
import re
from typing import Optional, Dict, Any, List

from openjiuwen.core.foundation.tool.tool import tool

from ..memory import (
    MemoryIndexManager,
    MemorySettings,
    create_memory_settings,
    is_memory_enabled,
)

logger = logging.getLogger(__name__)

# 群聊模式标记：群聊中禁止 write_memory / edit_memory
_GROUP_CHAT_MODE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "group_chat_mode", default=False,
)


def set_group_chat_mode(enabled: bool) -> contextvars.Token:
    return _GROUP_CHAT_MODE.set(enabled)


def is_group_chat_mode() -> bool:
    return _GROUP_CHAT_MODE.get()


_global_manager: Optional[MemoryIndexManager] = None
_global_workspace_dir: str = "."
_global_settings: Optional[MemorySettings] = None
_global_agent_id: str = "default"


def _is_path_traversal_attempt(normalized: str) -> bool:
    """Check if path contains directory traversal patterns.
    
    Args:
        normalized: Normalized path with forward slashes
    
    Returns:
        True if path traversal is detected
    """
    if ".." in normalized:
        return True
    if normalized.startswith("/"):
        return True
    if len(normalized) >= 2 and normalized[1] == ":":
        return True
    return False


def _validate_memory_path(path: str) -> tuple[bool, str]:
    """Validate that path is within memory directory.
    
    Only allows:
    - memory/YYYY-MM-DD.md (date format files)
    - memory/USER.md
    - memory/MEMORY.md
    
    Returns:
        (is_valid, resolved_path_or_error)
    """
    normalized = path.replace("\\", "/")
    if _is_path_traversal_attempt(normalized):
        return (False, "Invalid path: directory traversal not allowed")
    
    if path in ("memory/USER.md", "memory/MEMORY.md"):
        return (True, path)
    
    if path.startswith("memory/"):
        filename = path[7:]
        if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", filename):
            return (True, path)
    
    return (False, f"Path must be memory/YYYY-MM-DD.md, memory/USER.md, or memory/MEMORY.md. Got: {path}")


def set_global_memory_manager(
    manager: Optional[MemoryIndexManager],
    workspace_dir: str = ".",
    settings: Optional[MemorySettings] = None,
    agent_id: str = "default"
):
    """Set global memory manager for tool functions."""
    global _global_manager, _global_workspace_dir, _global_settings, _global_agent_id
    _global_manager = manager
    _global_workspace_dir = workspace_dir
    _global_settings = settings
    _global_agent_id = agent_id


async def init_memory_manager_async(
    workspace_dir: str = ".",
    agent_id: str = "default"
) -> Optional[MemoryIndexManager]:
    """初始化记忆管理器（带文件监控）.
    
    Args:
        workspace_dir: 工作区目录
        agent_id: Agent ID
    
    Returns:
        MemoryIndexManager 实例，如果 memory 未启用则返回 None
    """
    global _global_manager, _global_workspace_dir, _global_settings, _global_agent_id
    
    if not is_memory_enabled():
        logger.info("Memory system is disabled")
        return None
    
    if _global_manager is not None and _global_workspace_dir == workspace_dir:
        return _global_manager
    
    settings = create_memory_settings(workspace_dir)
    
    _global_workspace_dir = workspace_dir
    _global_settings = settings
    _global_agent_id = agent_id
    
    try:
        _global_manager = await MemoryIndexManager.get(
            agent_id=agent_id,
            workspace_dir=workspace_dir,
            settings=settings
        )
        
        if _global_manager:
            logger.info(f"Memory manager initialized for: {workspace_dir}")
        
        return _global_manager
        
    except Exception as e:
        logger.error(f"Failed to initialize memory manager: {e}")
        return None


async def _ensure_global_manager() -> bool:
    """Ensure global memory manager is initialized."""
    global _global_manager, _global_settings, _global_workspace_dir, _global_agent_id
    
    if _global_manager is not None:
        return True
    
    try:
        _global_settings = _global_settings or MemorySettings()
        _global_manager = await MemoryIndexManager.get(
            agent_id=_global_agent_id,
            workspace_dir=_global_workspace_dir,
            settings=_global_settings
        )
        return True
    except Exception as e:
        logger.error(f"Failed to initialize global memory manager: {e}")
        return False


@tool(
    name="memory_search",
    description="在长期记忆系统中搜索用户的记忆信息。在回答关于之前的工作内容、决策、日期、人物、偏好或待办事项的问题之前，必须先调用此工具。",
)
async def memory_search(
    query: str,
    maxResults: Optional[int] = None,
    minScore: Optional[float] = None,
    sessionKey: Optional[str] = None
) -> Dict[str, Any]:
    """在长期记忆系统中搜索用户的记忆信息。在回答关于之前的工作内容、决策、日期、人物、偏好或待办事项的问题之前，必须先调用此工具。

    Args:
        query: 搜索查询内容
        maxResults: 最大返回结果数量 (1-50)
        minScore: 最小相关性分数 (0-1)
        sessionKey: 可选的会话键

    Returns:
        搜索结果字典，包含 results 列表
    """
    if not await _ensure_global_manager():
        return {
            "results": [],
            "disabled": True,
            "error": "Memory manager not available"
        }
    
    if not _global_manager:
        return {
            "results": [],
            "disabled": True,
            "error": "Memory manager not initialized"
        }
    
    try:
        opts = {}
        if maxResults is not None:
            opts["maxResults"] = maxResults
        if minScore is not None:
            opts["minScore"] = minScore
        if sessionKey is not None:
            opts["sessionKey"] = sessionKey
        
        results = await _global_manager.search(query, opts=opts if opts else None)
        
        for r in results:
            if r["startLine"] == r["endLine"]:
                r["citation"] = f"{r['path']}#L{r['startLine']}"
            else:
                r["citation"] = f"{r['path']}#L{r['startLine']}-L{r['endLine']}"
        
        status = _global_manager.status()
        
        return {
            "results": results,
            "provider": status.get("provider"),
            "model": status.get("model"),
            "disabled": False
        }
        
    except Exception as e:
        logger.error(f"Memory search failed: {e}")
        return {
            "results": [],
            "disabled": True,
            "error": str(e)
        }


@tool
async def memory_get(
    path: str,
    from_line: Optional[int] = None,
    lines: Optional[int] = None
) -> Dict[str, Any]:
    """安全地读取 memory/*.md 文件的指定行。在 memory_search 之后使用，只读取需要的行，保持上下文简洁。

    Args:
        path: 文件路径 (相对于工作区)
        from_line: 起始行号 (从1开始)
        lines: 读取的行数

    Returns:
        文件内容字典
    """
    if not await _ensure_global_manager():
        return {
            "path": path,
            "text": "",
            "disabled": True,
            "error": "Memory manager not available"
        }
    
    if not _global_manager:
        return {
            "path": path,
            "text": "",
            "disabled": True,
            "error": "Memory manager not initialized"
        }
    
    try:
        result = await _global_manager.read_file(
            rel_path=path,
            from_line=from_line,
            lines=lines
        )
        return {
            **result,
            "disabled": False
        }
        
    except Exception as e:
        logger.error(f"Memory get failed: {e}")
        return {
            "path": path,
            "text": "",
            "disabled": True,
            "error": str(e)
        }


@tool
async def write_memory(
    path: str,
    content: str,
    append: bool = False
) -> Dict[str, Any]:
    """在 memory 目录下创建或更新记忆文件。仅用于写入记忆相关内容，如 memory/USER.md、memory/MEMORY.md 或 memory/*.md 文件。
    禁止用于创建代码文件、配置文件或其他非记忆类文件。

    Args:
        path: 文件路径，仅允许 memory/ 目录下的文件（如 "memory/xxx.md" 或 "memory/USER.md"）
        content: 要写入的内容
        append: 是否追加模式 (默认覆盖)

    Returns:
        操作结果字典
    """
    if is_group_chat_mode():
        return {"success": False, "error": "群聊模式下禁止写入记忆文件"}
    try:
        is_valid, result = _validate_memory_path(path)
        if not is_valid:
            return {
                "success": False,
                "path": path,
                "error": result
            }
        
        resolved_path = result
        full_path = os.path.join(_global_workspace_dir, resolved_path)
        
        parent_dir = os.path.dirname(full_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        
        file_existed = os.path.exists(full_path)
        
        mode = "a" if append else "w"
        with open(full_path, mode, encoding="utf-8") as f:
            f.write(content)
            f.write("\n")
        
        logger.info(f"{'Appended to' if append else 'Wrote'} file: {resolved_path}")

        return {
            "success": True,
            "path": resolved_path,
            "fullPath": full_path,
            "appended": append,
            "fileExisted": file_existed
        }
        
    except Exception as e:
        logger.error(f"Write failed: {e}")
        return {
            "success": False,
            "path": path,
            "error": str(e)
        }


@tool
async def edit_memory(
    path: str,
    oldText: str,
    newText: str
) -> Dict[str, Any]:
    """精确编辑 memory 目录下的文件内容。仅用于更新记忆文件（如 memory/USER.md、memory/MEMORY.md）。
    oldText 必须完全匹配文件中的内容。如果 oldText 出现多次，需要更具体地指定。

    Args:
        path: 文件路径，仅允许 memory/ 目录下的文件
        oldText: 要查找的文本 (必须完全匹配)
        newText: 替换的文本

    Returns:
        操作结果字典
    """
    if is_group_chat_mode():
        return {"success": False, "error": "群聊模式下禁止编辑记忆文件"}
    try:
        is_valid, result = _validate_memory_path(path)
        if not is_valid:
            return {
                "success": False,
                "path": path,
                "error": result
            }
        
        resolved_path = result
        full_path = os.path.join(_global_workspace_dir, resolved_path)
        
        if not os.path.exists(full_path):
            return {
                "success": False,
                "path": path,
                "error": f"File not found: {path}"
            }
        
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        if oldText not in content:
            return {
                "success": False,
                "path": path,
                "error": "oldText not found in file. Use read_memory tool to check exact content."
            }
        
        occurrences = content.count(oldText)
        if occurrences > 1:
            return {
                "success": False,
                "path": path,
                "error": f"oldText appears {occurrences} times in file. Be more specific."
            }
        
        new_content = content.replace(oldText, newText, 1)
        
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)
            f.write("\n")
        
        logger.info(f"Edited file: {resolved_path}")

        return {
            "success": True,
            "path": resolved_path,
            "replaced": oldText,
            "with": newText
        }
        
    except Exception as e:
        logger.error(f"Edit failed: {e}")
        return {
            "success": False,
            "path": path,
            "error": str(e)
        }


@tool
async def read_memory(
    path: str,
    offset: Optional[int] = None,
    limit: Optional[int] = None
) -> Dict[str, Any]:
    """读取 memory 目录下的文件内容。仅用于读取记忆文件（如 memory/USER.md、memory/MEMORY.md 或 memory/*.md）。

    Args:
        path: 文件路径，仅允许 memory/ 目录下的文件
        offset: 起始行号 (从1开始)
        limit: 读取的行数

    Returns:
        文件内容字典
    """
    try:
        is_valid, result = _validate_memory_path(path)
        if not is_valid:
            return {
                "success": False,
                "path": path,
                "content": "",
                "error": result
            }
        
        resolved_path = result
        full_path = os.path.join(_global_workspace_dir, resolved_path)
        
        if not os.path.exists(full_path):
            return {
                "success": False,
                "path": path,
                "content": "",
                "error": f"File not found: {path}"
            }
        
        if not os.path.isfile(full_path):
            return {
                "success": False,
                "path": path,
                "content": "",
                "error": f"Not a file: {path}"
            }
        
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        
        total_lines = len(lines)
        
        if offset is not None:
            start = max(0, offset - 1)
        else:
            start = 0
        
        if limit is not None:
            end = min(start + limit, total_lines)
        else:
            end = total_lines
        
        selected_lines = lines[start:end]
        content = "".join(selected_lines)
        
        return {
            "success": True,
            "path": resolved_path,
            "content": content,
            "totalLines": total_lines,
            "startLine": start + 1,
            "endLine": end,
            "truncated": limit is not None and end < total_lines
        }
        
    except Exception as e:
        logger.error(f"Read failed: {e}")
        return {
            "success": False,
            "path": path,
            "content": "",
            "error": str(e)
        }


def get_decorated_tools() -> List:
    """获取使用 @tool 装饰器的工具列表"""
    return [memory_search, memory_get, write_memory, edit_memory, read_memory]
