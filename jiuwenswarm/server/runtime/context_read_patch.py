# coding: utf-8
"""读人设文件时跳过跨进程读写锁。

AGENT.md、SOUL.md 这类说明文件只是读进提示词。每个会话的文件还不一样，
不需要为了「别的进程正在写同一份」去领一把 sqlite 锁。
那把锁的领锁窗口是全局的，20 个会话会排成一队。

``only_read=True`` 是本地读文件函数已有的开关，表示跳过这把锁。
写文件的路径不改。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Callable, Optional

from openjiuwen.core.common.logging import logger

__all__ = [
    "apply_context_read_patch",
    "remove_context_read_patch",
]

_PATCHED = False
_skip_lock: ContextVar[bool] = ContextVar("context_read_skip_lock", default=False)
_original_read_file: Optional[Callable] = None
_original_read_context_file: Optional[Callable] = None


async def _read_file_skip_lock(self: Any, path: str, *args: Any, **kwargs: Any) -> Any:
    """人设读取进行中时，强制只读、不上锁。其它调用保持原样。"""
    if _skip_lock.get():
        kwargs["only_read"] = True
    return await _original_read_file(self, path, *args, **kwargs)


async def _read_context_file_skip_lock(
    sys_operation: Any,
    workspace: Any,
    file_key: str,
) -> Any:
    token: Token[bool] = _skip_lock.set(True)
    try:
        return await _original_read_context_file(sys_operation, workspace, file_key)
    finally:
        _skip_lock.reset(token)


def apply_context_read_patch() -> None:
    """安装补丁。重复调用无效果。"""
    global _PATCHED, _original_read_file, _original_read_context_file
    if _PATCHED:
        return

    import openjiuwen.harness.prompts.sections.context as context_mod
    from openjiuwen.core.sys_operation.local.fs_operation import FsOperation

    _original_read_file = getattr(FsOperation, "read_file")
    _original_read_context_file = getattr(context_mod, "_read_context_file")
    setattr(FsOperation, "read_file", _read_file_skip_lock)
    setattr(context_mod, "_read_context_file", _read_context_file_skip_lock)
    _PATCHED = True
    logger.info("[ContextRead] patch applied (persona files read with only_read=True)")


def remove_context_read_patch() -> None:
    """卸下补丁。只给单测用。"""
    global _PATCHED, _original_read_file, _original_read_context_file
    if not _PATCHED:
        return

    import openjiuwen.harness.prompts.sections.context as context_mod
    from openjiuwen.core.sys_operation.local.fs_operation import FsOperation

    setattr(FsOperation, "read_file", _original_read_file)
    setattr(context_mod, "_read_context_file", _original_read_context_file)
    _original_read_file = None
    _original_read_context_file = None
    _PATCHED = False
