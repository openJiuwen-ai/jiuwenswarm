# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CR-5: shutdown_context_session_memory 三态日志语义.

区分"属性缺失（框架改名，保留 WARNING 防双记忆后端）"与"属性合法
为 None（未配置/已关闭的幂等态，仅 debug）"与"manager 在位（关闭
并返回 True）"。
"""

from __future__ import annotations

import logging

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _FakeManager:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _LiveRail:
    def __init__(self) -> None:
        self._session_memory_mgr = _FakeManager()


class _NoneRail:
    def __init__(self) -> None:
        self._session_memory_mgr = None


class _MissingRail:
    """完全不含该属性（框架改名 / 异型 rail 场景）。"""


def test_live_manager_is_shutdown_and_reset() -> None:
    rail = _LiveRail()
    manager = rail._session_memory_mgr
    assert JiuWenSwarmDeepAdapter.shutdown_context_session_memory(rail) is True
    assert manager.shutdown_calls == 1
    assert rail._session_memory_mgr is None


def test_none_attribute_is_silent_idempotent_state(caplog) -> None:
    rail = _NoneRail()
    with caplog.at_level(logging.WARNING):
        assert JiuWenSwarmDeepAdapter.shutdown_context_session_memory(rail) is False
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_missing_attribute_keeps_guard_warning(caplog) -> None:
    rail = _MissingRail()
    with caplog.at_level(logging.WARNING):
        assert JiuWenSwarmDeepAdapter.shutdown_context_session_memory(rail) is False
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "double memory backend" in warnings[0].getMessage()
