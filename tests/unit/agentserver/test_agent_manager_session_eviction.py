# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentManager 空闲会话逐出测试.

背景: web 通道每个新会话在 AgentManager.agents[channel][mode][session_id]
登记一个 JiuWenClaw 实例, 此前没有任何清理路径(cleanup_session 无调用方),
实例与 rail 回调随会话数无限累积 —— 每轮新建会话的时延线性增长根因。
期望: get_agent 未命中(新会话到达, 泄漏增长时刻)时, 顺带逐出同
channel/mode 下空闲超过 TTL 的会话并调用其 cleanup()。
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenclaw.agentserver.agent_manager import AgentManager

TTL_ENV = "AGENT_SESSION_IDLE_TTL_SECONDS"


def _make_mock_agent(working: bool = False) -> MagicMock:
    agent = MagicMock()
    agent.cleanup = AsyncMock()
    agent.create_instance = AsyncMock()
    agent.reload_agent_config = AsyncMock()
    agent.is_working = MagicMock(return_value=working)
    return agent


def _seed_session(
    manager: AgentManager, channel: str, mode: str, session_id: str, age_seconds: float
) -> MagicMock:
    agent = _make_mock_agent()
    manager.agents.setdefault(channel, {}).setdefault(mode, {})[session_id] = agent
    # 模拟已闲置 age_seconds 的会话
    manager._session_last_used[(channel, mode, session_id)] = (
        time.monotonic() - age_seconds
    )
    return agent


@pytest.fixture()
def manager(monkeypatch) -> AgentManager:
    monkeypatch.setenv(TTL_ENV, "120")
    mgr = AgentManager(agent_id="tenant_x", service_id="svc_x")
    return mgr


@pytest.fixture()
def mock_jiuwenclaw(monkeypatch):
    """替换 interface.JiuWenClaw, 避免真实 create_instance 装配."""
    instances = []

    def _factory(*args, **kwargs):
        agent = _make_mock_agent()
        instances.append(agent)
        return agent

    import jiuwenclaw.agentserver.interface as iface_mod

    monkeypatch.setattr(iface_mod, "JiuWenClaw", _factory)
    return instances


@pytest.mark.unit
async def test_new_session_evicts_idle_sibling(manager, mock_jiuwenclaw):
    stale = _seed_session(manager, "web", "agent", "sess_old", age_seconds=9999)

    got = await manager.get_agent(channel_id="web", mode="agent", session_id="sess_new")

    assert got is not None
    stale.cleanup.assert_awaited_once()
    assert "sess_old" not in manager.agents["web"]["agent"]
    assert "sess_new" in manager.agents["web"]["agent"]


@pytest.mark.unit
async def test_fresh_session_not_evicted(manager, mock_jiuwenclaw):
    fresh = _seed_session(manager, "web", "agent", "sess_fresh", age_seconds=1)

    await manager.get_agent(channel_id="web", mode="agent", session_id="sess_new")

    fresh.cleanup.assert_not_awaited()
    assert "sess_fresh" in manager.agents["web"]["agent"]


@pytest.mark.unit
async def test_working_session_not_evicted(manager, mock_jiuwenclaw):
    busy = _seed_session(manager, "web", "agent", "sess_busy", age_seconds=9999)
    busy.is_working = MagicMock(return_value=True)

    await manager.get_agent(channel_id="web", mode="agent", session_id="sess_new")

    busy.cleanup.assert_not_awaited()
    assert "sess_busy" in manager.agents["web"]["agent"]


@pytest.mark.unit
async def test_hit_refreshes_last_used(manager, mock_jiuwenclaw):
    agent = _seed_session(manager, "web", "agent", "sess_hot", age_seconds=9999)

    got = await manager.get_agent(channel_id="web", mode="agent", session_id="sess_hot")

    assert got is agent
    # 命中后时间戳被刷新, 再来一个新会话也不会逐出它
    await manager.get_agent(channel_id="web", mode="agent", session_id="sess_new")
    agent.cleanup.assert_not_awaited()
    assert "sess_hot" in manager.agents["web"]["agent"]


@pytest.mark.unit
async def test_zero_ttl_disables_eviction(manager, monkeypatch, mock_jiuwenclaw):
    monkeypatch.setenv(TTL_ENV, "0")
    stale = _seed_session(manager, "web", "agent", "sess_old", age_seconds=9999)

    await manager.get_agent(channel_id="web", mode="agent", session_id="sess_new")

    stale.cleanup.assert_not_awaited()
    assert "sess_old" in manager.agents["web"]["agent"]


@pytest.mark.unit
async def test_eviction_failure_does_not_block_new_session(manager, mock_jiuwenclaw):
    stale = _seed_session(manager, "web", "agent", "sess_bad", age_seconds=9999)
    stale.cleanup = AsyncMock(side_effect=RuntimeError("cleanup boom"))

    got = await manager.get_agent(channel_id="web", mode="agent", session_id="sess_new")

    assert got is not None
    assert "sess_new" in manager.agents["web"]["agent"]
