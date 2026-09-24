# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RailManager.set_agent_instance 跨 session 实例切换的回归测试.

背景: RailManager 是进程级单例, 而 DeepAgent 实例 per-session 新建。
``_registered_rails`` 记录的是"哪些 rail 已挂到当前 ``_agent_instance``"，
agent 实例更换后必须清空该集合, 否则 ``hot_reload_rail`` 会误判"已注册"
而跳过挂载, 导致用户 rail 扩展自第二个 session 起静默失效。
"""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.agents.harness.common.plugins.rail_manager import (
    RailExtension,
    RailManager,
)


class _FakeAgent:
    """记录 register_rail / unregister_rail 调用的假 DeepAgent 实例."""

    def __init__(self) -> None:
        self.registered: list[Any] = []
        self.unregistered: list[Any] = []

    async def register_rail(self, rail_instance: Any) -> None:
        self.registered.append(rail_instance)

    async def unregister_rail(self, rail_instance: Any) -> None:
        self.unregistered.append(rail_instance)


def _make_manager() -> RailManager:
    """构造不触碰文件系统/单例状态的 RailManager（仅初始化被测方法所需属性）."""
    manager = object.__new__(RailManager)
    manager._agent_instance = None
    manager._registered_rails = set()
    manager._rail_instances = {}
    manager._extensions = {}
    return manager


def test_same_instance_keeps_registered_rails() -> None:
    """重复设置同一 agent 实例时, 注册记录保持不变（不触发无谓重挂）."""
    manager = _make_manager()
    agent = _FakeAgent()

    manager.set_agent_instance(agent)
    manager._registered_rails.add("demo_rail")
    manager.set_agent_instance(agent)

    assert manager._registered_rails == {"demo_rail"}
    assert manager._agent_instance is agent


def test_new_instance_clears_registered_rails() -> None:
    """更换 agent 实例时清空注册记录, 使 hot_reload_rail 能重挂到新实例."""
    manager = _make_manager()
    agent_old = _FakeAgent()
    agent_new = _FakeAgent()

    manager.set_agent_instance(agent_old)
    manager._registered_rails.update({"rail_a", "rail_b"})

    manager.set_agent_instance(agent_new)

    assert manager._agent_instance is agent_new
    assert manager._registered_rails == set()


def test_rail_instances_cache_survives_instance_switch() -> None:
    """rail 实例缓存跨 session 保留（实例不绑定具体 agent, 可复用）."""
    manager = _make_manager()
    cached_rail = object()
    manager._rail_instances["demo_rail"] = cached_rail

    manager.set_agent_instance(_FakeAgent())
    manager._registered_rails.add("demo_rail")
    manager.set_agent_instance(_FakeAgent())

    assert manager._rail_instances["demo_rail"] is cached_rail


def test_hot_reload_rail_remounts_on_new_agent_instance() -> None:
    """回归: 第二个 session 的新 agent 实例必须重新挂上 rail.

    修复前: 实例切换后 ``_registered_rails`` 残留, ``hot_reload_rail``
    命中"已注册, 跳过"分支, 新实例上 rail 永不生效（静默失效）。
    """
    manager = _make_manager()
    rail = object()
    manager._extensions["demo_rail"] = RailExtension(name="demo_rail")
    manager.load_rail_instance_without_enabled_check = lambda name: rail  # type: ignore[method-assign]

    # session 1: 首次挂载
    agent_1 = _FakeAgent()
    manager.set_agent_instance(agent_1)
    asyncio.run(manager.hot_reload_rail("demo_rail", True))
    assert agent_1.registered == [rail]

    # session 2: 新实例必须重挂（修复前此处 agent_2.registered 为空）
    agent_2 = _FakeAgent()
    manager.set_agent_instance(agent_2)
    asyncio.run(manager.hot_reload_rail("demo_rail", True))
    assert agent_2.registered == [rail]
    assert manager._registered_rails == {"demo_rail"}
