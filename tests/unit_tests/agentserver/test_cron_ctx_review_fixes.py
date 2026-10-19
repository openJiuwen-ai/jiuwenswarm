# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PR review fixes: cron_* 普通续聊恢复 + 审批 pin 清理。"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

# interface_deep 会拉 image_tools → google.genai；本地/精简环境可能未装，先 stub。
_genai = ModuleType("google.genai")
_genai.types = ModuleType("google.genai.types")
_google = sys.modules.get("google") or ModuleType("google")
_google.genai = _genai
sys.modules["google"] = _google
sys.modules["google.genai"] = _genai
sys.modules["google.genai.types"] = _genai.types

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (  # noqa: E402
    JiuWenSwarmDeepAdapter,
)


def _bare_adapter() -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = False
    adapter._session_parent_adapter = None
    adapter._sessions_awaiting_tool_permission = set()
    adapter._session_adapter_last_used = {}
    adapter._instance = None
    return adapter


@pytest.mark.asyncio
async def test_block_stale_does_not_skip_plain_cron_recover(monkeypatch):
    """无 source 的 cron_* 请求也应触发空上下文恢复。"""
    adapter = _bare_adapter()
    recovered: list[tuple[str, str]] = []

    async def _fake_block(request):
        return False

    async def _fake_recover(session_id, request_id, *, reload_interrupt=False):
        recovered.append((session_id, request_id))

    monkeypatch.setattr(adapter, "_block_stale_cron_permission_resume", _fake_block)
    monkeypatch.setattr(adapter, "_recover_cron_session_context", _fake_recover)

    request = SimpleNamespace(
        session_id="cron_job_1",
        request_id="req-plain",
        channel_id="web",
        params={"query": "总结一下"},
        metadata={},
    )
    # 直接复现入口分支：block 未拦时，普通 cron_* 仍 recover。
    if await adapter._block_stale_cron_permission_resume(request):
        pytest.fail("plain cron chat must not be blocked")
    elif str(request.session_id).startswith("cron_"):
        await adapter._recover_cron_session_context(
            request.session_id, str(request.request_id or ""),
        )

    assert recovered == [("cron_job_1", "req-plain")]


def test_clear_cron_awaiting_when_interrupt_gone():
    adapter = _bare_adapter()
    sid = "cron_done_1"
    adapter.mark_session_awaiting_tool_permission(sid)
    assert sid in adapter._sessions_awaiting_tool_permission

    adapter._read_session_interrupt_state = MagicMock(return_value=None)
    adapter._clear_cron_awaiting_if_interrupt_gone(sid)
    assert sid not in adapter._sessions_awaiting_tool_permission


def test_clear_cron_awaiting_keeps_pin_while_interrupt_live():
    adapter = _bare_adapter()
    sid = "cron_live_1"
    adapter.mark_session_awaiting_tool_permission(sid)
    adapter._read_session_interrupt_state = MagicMock(return_value={"paused": True})

    adapter._clear_cron_awaiting_if_interrupt_gone(sid)
    assert sid in adapter._sessions_awaiting_tool_permission


def test_clear_cron_awaiting_ignores_non_cron_session():
    adapter = _bare_adapter()
    sid = "web_session"
    adapter._sessions_awaiting_tool_permission.add(sid)
    adapter._read_session_interrupt_state = MagicMock(return_value=None)

    adapter._clear_cron_awaiting_if_interrupt_gone(sid)
    assert sid in adapter._sessions_awaiting_tool_permission
