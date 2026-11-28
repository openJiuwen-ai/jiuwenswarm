# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""skip_tool_compat：新旧 agent-core 下的否决标记读写。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.harness.common import skip_tool_compat


def _ctx():
    return AgentCallbackContext(agent=SimpleNamespace())


@pytest.mark.skipif(
    not skip_tool_compat.has_marker_support(),
    reason="旧 core 无 per-call marker，走 legacy 回落",
)
def test_marker_branch_does_not_touch_shared_extra():
    ctx = _ctx()
    skip_tool_compat.mark_skipped(ctx)
    assert skip_tool_compat.was_skipped(ctx) is True
    assert "_skip_tool" not in ctx.extra, "新 core 的标记不写入整轮共享 extra"


def test_legacy_fallback_writes_shared_extra(monkeypatch):
    monkeypatch.setattr(skip_tool_compat, "_mark_tool_skipped", None)
    monkeypatch.setattr(skip_tool_compat, "_tool_call_skipped", None)
    ctx = _ctx()
    assert skip_tool_compat.was_skipped(ctx) is False
    skip_tool_compat.mark_skipped(ctx)
    assert ctx.extra["_skip_tool"] is True
    assert skip_tool_compat.was_skipped(ctx) is True


def test_legacy_write_from_sibling_stays_visible():
    """未迁移 rail 直接写共享 extra 时，本次 was_skipped 仍要看得见。"""
    ctx = _ctx()
    ctx.extra["_skip_tool"] = True
    assert skip_tool_compat.was_skipped(ctx) is True
