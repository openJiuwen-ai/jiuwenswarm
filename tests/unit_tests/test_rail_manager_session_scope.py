# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RailManager session-scope tests (issue #3711).

0.2.3 的 session-scoped adapter 设计中，同一进程内每个会话都有独立的
DeepAgent 实例。RailManager 作为全局单例，其"已注册"状态和 rail 实例
缓存必须按 agent 隔离；否则第二个会话会被全局缓存跳过，自定义 Rail
不生效（不注册、不 init）。
"""

import pytest

import jiuwenswarm.agents.harness.common.plugins.rail_manager as rail_manager_module
from jiuwenswarm.agents.harness.common.plugins.rail_manager import (
    RailManager,
    get_rail_manager,
)

# openjiuwen 顶部导入链会拉入 pysbd（其源码含 invalid escape sequence，
# 在 filterwarnings=error 下变成 SyntaxError）。这是已知的上游依赖问题，
# 与本测试无关，这里局部放行 SyntaxWarning。
pytestmark = pytest.mark.filterwarnings("ignore::SyntaxWarning")

RAIL_PY = '''from openjiuwen.harness.rails import DeepAgentRail


class CustomRail(DeepAgentRail):
    """测试用自定义 Rail"""

    priority: int = 50
'''


class FakeDeepAgent:
    """记录 register/unregister 调用的最小 DeepAgent 替身."""

    def __init__(self, label: str):
        self.label = label
        self.registered: list = []
        self.unregistered: list = []

    async def register_rail(self, rail):
        self.registered.append(rail)

    async def unregister_rail(self, rail):
        self.unregistered.append(rail)


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    """提供绑定临时 workspace 的全新 RailManager 单例，含一个启用的扩展."""
    monkeypatch.setattr(RailManager, "_instance", None)
    monkeypatch.setattr(
        rail_manager_module, "get_agent_workspace_dir", lambda: tmp_path
    )

    source = tmp_path / "source" / "demo_ext"
    source.mkdir(parents=True)
    (source / "rail.py").write_text(RAIL_PY, encoding="utf-8")

    mgr = get_rail_manager()
    mgr.import_extension(str(source))
    mgr.toggle_extension("demo_ext", True)
    return mgr


async def test_second_agent_registers_enabled_rail(manager):
    """第二个会话的 DeepAgent 也必须注册启用的 rail（issue #3711 核心）."""
    agent1 = FakeDeepAgent("session-1")
    agent2 = FakeDeepAgent("session-2")

    manager.set_agent_instance(agent1)
    await manager.hot_reload_rail("demo_ext", True)
    manager.set_agent_instance(agent2)
    await manager.hot_reload_rail("demo_ext", True)

    assert len(agent1.registered) == 1
    assert len(agent2.registered) == 1, (
        "第二个 agent 的注册被全局 _registered_rails 缓存跳过 (issue #3711)"
    )


async def test_each_agent_gets_independent_rail_instance(manager):
    """rail 实例带 per-agent 状态（init 绑定 agent），不能跨 agent 共享."""
    agent1 = FakeDeepAgent("session-1")
    agent2 = FakeDeepAgent("session-2")

    manager.set_agent_instance(agent1)
    await manager.hot_reload_rail("demo_ext", True)
    manager.set_agent_instance(agent2)
    await manager.hot_reload_rail("demo_ext", True)

    assert agent1.registered[0] is not agent2.registered[0]


async def test_same_agent_hot_reload_twice_skips_duplicate(manager):
    """同一 agent 重复热更新仍保持幂等（不重复注册）."""
    agent1 = FakeDeepAgent("session-1")

    manager.set_agent_instance(agent1)
    await manager.hot_reload_rail("demo_ext", True)
    await manager.hot_reload_rail("demo_ext", True)

    assert len(agent1.registered) == 1


async def test_unregister_only_affects_current_agent(manager):
    """注销只作用于当前 agent；其他 agent 的注册状态保持完整."""
    agent1 = FakeDeepAgent("session-1")
    agent2 = FakeDeepAgent("session-2")

    manager.set_agent_instance(agent1)
    await manager.hot_reload_rail("demo_ext", True)
    manager.set_agent_instance(agent2)
    await manager.hot_reload_rail("demo_ext", True)

    await manager.hot_reload_rail("demo_ext", False)

    # 注销的必须是 agent2 自己注册的那个实例（按身份注销才能生效）
    assert agent2.unregistered == agent2.registered
    assert agent1.unregistered == []
    # agent1 上仍然注册着
    assert manager.get_registered_rail_names() == {"demo_ext"}
    assert manager.is_rail_registered("demo_ext")


async def test_release_agent_state_drops_bookkeeping(manager):
    """会话 adapter 清理后，该 agent 的注册状态必须被释放."""
    agent1 = FakeDeepAgent("session-1")
    agent2 = FakeDeepAgent("session-2")

    manager.set_agent_instance(agent1)
    await manager.hot_reload_rail("demo_ext", True)
    manager.set_agent_instance(agent2)
    await manager.hot_reload_rail("demo_ext", True)

    manager.release_agent_state(agent2)
    assert manager.get_registered_rail_names() == {"demo_ext"}

    manager.release_agent_state(agent1)
    assert manager.get_registered_rail_names() == set()
    assert not manager.is_rail_registered("demo_ext")


async def test_delete_extension_clears_all_agent_states(manager):
    """删除扩展时，所有 agent 的注册状态与实例缓存都要清除."""
    agent1 = FakeDeepAgent("session-1")
    agent2 = FakeDeepAgent("session-2")

    manager.set_agent_instance(agent1)
    await manager.hot_reload_rail("demo_ext", True)
    manager.set_agent_instance(agent2)
    await manager.hot_reload_rail("demo_ext", True)

    manager.delete_extension("demo_ext")

    assert manager.get_registered_rail_names() == set()
