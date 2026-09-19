# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""请求期读盘 rail 从沙箱切回本地。"""

from __future__ import annotations

from pathlib import Path

_INTERFACE_DEEP = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm"
    / "server"
    / "runtime"
    / "agent_adapter"
    / "interface_deep.py"
)


def _interface_deep_source() -> str:
    return _INTERFACE_DEEP.read_text(encoding="utf-8")


def _slice_after(text: str, marker: str) -> str:
    start = text.find(marker)
    if start < 0:
        return ""
    return text[start:]


def test_rebind_does_not_copy_tool_channel() -> None:
    """只换 rail 通道，不摸工具手里那份 sys_operation。"""
    body = _slice_after(_interface_deep_source(), "def _rebind_late_read_rails_to_local")
    assert body
    body = body[:1600]
    assert "_set_rail_sys_operation" in body
    assert "_tool_ctx" not in body
    assert "tool_ctx.sys_operation" not in body


def test_reload_rebinds_after_memory_rail_refresh() -> None:
    """热更新刷新记忆 rail 后必须再切回本地。"""
    reload_src = _slice_after(_interface_deep_source(), "async def reload_agent_config")
    next_def = reload_src.find("\n    def ", 1)
    if next_def != -1:
        reload_src = reload_src[:next_def]
    mem_at = reload_src.find("_handle_memory_rail_by_config")
    rebind_at = reload_src.find("_rebind_late_read_rails_to_local")
    assert mem_at != -1
    assert rebind_at != -1
    assert mem_at < rebind_at
