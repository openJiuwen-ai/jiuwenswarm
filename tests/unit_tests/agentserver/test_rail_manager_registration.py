"""RailManager 实例更换时 rail 注册状态的回归测试。

Bug 背景：``_registered_rails`` 是全局集合，记录的是"已注册到上一个
DeepAgent 实例"。实例更换（如每个 chat run 新建 DeepAgent）后，
``hot_reload_rail`` 误判"已注册"而跳过，新实例永远拿不到 rail。
修复：``set_agent_instance`` 检测到实例更换时清空注册状态。
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common.plugins.rail_manager import (
    RailExtension,
    RailManager,
)


class _FakeAgent:
    def __init__(self) -> None:
        self.register_rail = AsyncMock()
        self.unregister_rail = AsyncMock()


def _fresh_manager() -> RailManager:
    """绕过单例，拿到干净状态的 RailManager。"""
    mgr = object.__new__(RailManager)
    mgr._extensions = {}
    mgr._registered_rails = set()
    mgr._agent_instance = None
    mgr._rail_instances = {}
    return mgr


def _register_fake_extension(mgr: RailManager, name: str = "dummy") -> None:
    mgr._extensions[name] = RailExtension(name=name, enabled=True)
    mgr.load_rail_instance_without_enabled_check = (  # type: ignore[method-assign]
        lambda n: object()
    )


async def test_second_agent_instance_gets_rail_registered() -> None:
    mgr = _fresh_manager()
    _register_fake_extension(mgr)

    agent_a = _FakeAgent()
    mgr.set_agent_instance(agent_a)
    await mgr.hot_reload_rail("dummy", True)
    assert agent_a.register_rail.await_count == 1

    # 实例更换：同一个 rail 必须能注册到新实例，而不是被"已注册"跳过
    agent_b = _FakeAgent()
    mgr.set_agent_instance(agent_b)
    await mgr.hot_reload_rail("dummy", True)
    assert agent_b.register_rail.await_count == 1


async def test_same_instance_keeps_skip_semantics() -> None:
    mgr = _fresh_manager()
    _register_fake_extension(mgr)

    agent = _FakeAgent()
    mgr.set_agent_instance(agent)
    await mgr.hot_reload_rail("dummy", True)
    # 同一实例重复设置：注册状态保留，再次注册仍跳过
    mgr.set_agent_instance(agent)
    await mgr.hot_reload_rail("dummy", True)
    assert agent.register_rail.await_count == 1


async def test_unregister_then_reregister_on_same_instance() -> None:
    mgr = _fresh_manager()
    _register_fake_extension(mgr)

    agent = _FakeAgent()
    mgr.set_agent_instance(agent)
    await mgr.hot_reload_rail("dummy", True)
    await mgr.hot_reload_rail("dummy", False)
    assert agent.unregister_rail.await_count == 1
    await mgr.hot_reload_rail("dummy", True)
    assert agent.register_rail.await_count == 2
