# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RailManager session-scope tests (issue #3711).

0.2.3 的 session-scoped adapter 设计中，同一进程内每个会话都有独立的
DeepAgent 实例。RailManager 作为全局单例，其"已注册"状态和 rail 实例
缓存必须按 agent 隔离；否则第二个会话会被全局缓存跳过，自定义 Rail
不生效（不注册、不 init）。
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

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


async def _load_disabled_extensions(manager, *agents):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    manager.toggle_extension("demo_ext", False)
    for agent in agents:
        adapter = object.__new__(JiuWenSwarmDeepAdapter)
        adapter._instance = agent
        await adapter.load_user_rails()


async def _toggle_through_handler(enabled):
    from e2a.wire_codec import (
        encode_agent_response_for_wire,
        parse_agent_server_wire_unary,
    )
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.management.extensions import ExtensionsManagement
    from jiuwenswarm.server.ws_send import send_wire_payload

    frames = []

    async def send(data):
        frames.append(json.loads(data))

    lookup = Mock(return_value=None)
    host = SimpleNamespace(
        _agent_manager=SimpleNamespace(get_agent_nowait=lookup),
        services=SimpleNamespace(encode_response=encode_agent_response_for_wire, send=send_wire_payload),
    )
    request = AgentRequest(
        request_id="toggle", channel_id="web", req_method=ReqMethod.EXTENSIONS_TOGGLE,
        params={"name": "demo_ext", "enabled": enabled},
    )
    await ExtensionsManagement(host)._handle_extensions_toggle(
        SimpleNamespace(send=send), request, asyncio.Lock(),
    )
    assert len(frames) == 1
    return parse_agent_server_wire_unary(frames[0]), lookup


async def test_toggle_before_first_agent_saves_config_without_constructing_agent(manager):
    manager.toggle_extension("demo_ext", False)
    result, lookup = await _toggle_through_handler(True)
    assert result.ok is True, result
    assert result.payload["enabled"] is True
    lookup.assert_not_called()
    assert json.loads(manager._config_file.read_text(encoding="utf-8"))["demo_ext"]["enabled"] is True


async def test_toggle_updates_every_cached_session_and_profile_before_ack(manager):
    work = FakeDeepAgent("web/work/project-a/session-1")
    code = FakeDeepAgent("web/code/project-b/session-2")
    await _load_disabled_extensions(manager, work, code)

    enabled, lookup = await _toggle_through_handler(True)
    assert enabled.ok is True, enabled
    assert len(work.registered) == len(code.registered) == 1
    assert work.registered[0] is not code.registered[0]
    lookup.assert_not_called()

    disabled, _ = await _toggle_through_handler(False)
    assert disabled.ok is True, disabled
    assert work.unregistered == work.registered
    assert code.unregistered == code.registered


async def test_global_toggle_is_idempotent_and_reports_partial_failure_for_retry(manager):
    class FailingAgent(FakeDeepAgent):
        fail = True

        async def register_rail(self, rail):
            if self.fail:
                raise RuntimeError("plugin initialization failed")
            await super().register_rail(rail)

    failed = FailingAgent("session-failed")
    healthy = FakeDeepAgent("session-healthy")
    await _load_disabled_extensions(manager, failed, healthy)
    result, _ = await _toggle_through_handler(True)
    assert result.ok is False
    assert len(healthy.registered) == 1

    failed.fail = False
    retried, _ = await _toggle_through_handler(True)
    assert retried.ok is True, retried
    assert len(failed.registered) == len(healthy.registered) == 1


async def test_released_instance_is_not_retained_by_inflight_hot_reload(manager):
    entered = asyncio.Event()
    release = asyncio.Event()

    class ClosingAgent(FakeDeepAgent):
        async def register_rail(self, rail):
            entered.set()
            await release.wait()
            await super().register_rail(rail)

    closing = ClosingAgent("closing-session")
    healthy = FakeDeepAgent("live-session")
    await _load_disabled_extensions(manager, closing, healthy)
    task = asyncio.create_task(_toggle_through_handler(True))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        manager.release_agent_state(closing)
        release.set()
        result, _ = await asyncio.wait_for(task, 2)
        assert result.ok is True, result
        assert closing.unregistered == closing.registered
        assert id(closing) not in manager._agent_rail_states
        assert len(healthy.registered) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_new_instance_and_opposite_toggle_observe_latest_configuration(manager):
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowAgent(FakeDeepAgent):
        async def register_rail(self, rail):
            entered.set()
            await release.wait()
            await super().register_rail(rail)

    cached = SlowAgent("cached")
    fresh = FakeDeepAgent("new-project")
    await _load_disabled_extensions(manager, cached)
    enable = asyncio.create_task(_toggle_through_handler(True))
    tasks = [enable]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        loading = asyncio.create_task(manager.load_rails_for_agent(fresh))
        await asyncio.sleep(0)
        disable = asyncio.create_task(_toggle_through_handler(False))
        tasks.extend([loading, disable])
        assert not enable.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert manager.list_extensions()[0]["enabled"] is False
        for agent in (cached, fresh):
            assert {id(rail) for rail in agent.registered} == {id(rail) for rail in agent.unregistered}
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
