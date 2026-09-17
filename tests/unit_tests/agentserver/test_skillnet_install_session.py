"""SkillNet 异步安装会话连续性：跨 SkillManager / 无状态 fallback 复用."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.runtime.skill import skill_manager as skill_manager_module
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


@pytest.fixture(autouse=True)
def _clear_skillnet_install_jobs():
    skill_manager_module._SKILLNET_INSTALL_JOBS.clear()
    yield
    skill_manager_module._SKILLNET_INSTALL_JOBS.clear()


@pytest.mark.asyncio
async def test_skillnet_install_status_visible_across_skill_manager_instances(
    tmp_path, monkeypatch
):
    """install 与 install_status 落到不同 SkillManager 时仍能查到 pending job."""
    manager_a = SkillManager(workspace_dir=str(tmp_path / "a"))
    manager_b = SkillManager(workspace_dir=str(tmp_path / "b"))

    async def _never_finish(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(manager_a, "_skillnet_install_background", _never_finish)

    install = await manager_a.handle_skills_skillnet_install(
        {"url": "https://github.com/example/skill-demo"}
    )
    assert install["success"] is True
    assert install["pending"] is True
    install_id = install["install_id"]
    assert install_id

    status = await manager_b.handle_skills_skillnet_install_status(
        {"install_id": install_id}
    )
    assert status == {"success": True, "status": "pending"}


@pytest.mark.asyncio
async def test_skillnet_install_status_missing_id_still_reports_session_expired(
    tmp_path,
):
    manager = SkillManager(workspace_dir=str(tmp_path))
    status = await manager.handle_skills_skillnet_install_status(
        {"install_id": "nonexistent"}
    )
    assert status["success"] is False
    assert status["detail_key"] == "skills.skillNet.errors.sessionExpired"


@pytest.mark.asyncio
async def test_get_stateless_agent_reuses_fallback_on_repeated_cache_miss(monkeypatch):
    server = agent_ws_server_module.AgentWebSocketServer()
    server._agent_manager.get_agent_nowait = MagicMock(return_value=None)

    created: list[object] = []

    class _FakeSwarm:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface.JiuWenSwarm",
        _FakeSwarm,
    )

    first = await server._get_stateless_agent("web")
    second = await server._get_stateless_agent("web")

    assert first is second
    assert len(created) == 1
    assert server._stateless_fallback_agents["web"] is first


@pytest.mark.asyncio
async def test_get_stateless_agent_prefers_cached_agent_mode_instance():
    server = agent_ws_server_module.AgentWebSocketServer()
    cached = SimpleNamespace(name="cached-agent")
    server._agent_manager.get_agent_nowait = MagicMock(return_value=cached)
    server._stateless_fallback_agents["web"] = SimpleNamespace(name="fallback")

    got = await server._get_stateless_agent("web")

    assert got is cached
    server._agent_manager.get_agent_nowait.assert_called_once_with(
        channel_id="web", mode="agent"
    )


@pytest.mark.asyncio
async def test_get_stateless_agent_isolates_fallback_by_channel(monkeypatch):
    server = agent_ws_server_module.AgentWebSocketServer()
    server._agent_manager.get_agent_nowait = MagicMock(return_value=None)

    class _FakeSwarm:
        def __init__(self) -> None:
            pass

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface.JiuWenSwarm",
        _FakeSwarm,
    )

    web = await server._get_stateless_agent("web")
    cli = await server._get_stateless_agent("cli")

    assert web is not cli
    assert server._stateless_fallback_agents["web"] is web
    assert server._stateless_fallback_agents["cli"] is cli


@pytest.mark.asyncio
async def test_skillnet_install_then_status_via_distinct_stateless_fallbacks(
    tmp_path, monkeypatch
):
    """模拟旧行为：两次 cache miss 各 new 一个 Swarm；共享 job 表后 status 仍连续."""
    server = agent_ws_server_module.AgentWebSocketServer()
    server._agent_manager.get_agent_nowait = MagicMock(return_value=None)

    managers = [
        SkillManager(workspace_dir=str(tmp_path / "m1")),
        SkillManager(workspace_dir=str(tmp_path / "m2")),
    ]
    idx = {"n": 0}

    class _EphemeralSwarm:
        def __init__(self) -> None:
            self._skill_manager = managers[idx["n"]]
            idx["n"] += 1

    # 强制每次 fallback miss 都 new（绕过 server 缓存），验证共享 job 仍生效
    async def _always_new(_channel_id: str):
        return _EphemeralSwarm()

    monkeypatch.setattr(server, "_get_stateless_agent", _always_new)

    swarm_install = await server._get_stateless_agent("web")
    manager_install = swarm_install._skill_manager

    async def _never_finish(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(manager_install, "_skillnet_install_background", _never_finish)

    install = await manager_install.handle_skills_skillnet_install(
        {"url": "https://github.com/example/skill-demo"}
    )
    install_id = install["install_id"]

    swarm_status = await server._get_stateless_agent("web")
    assert swarm_status is not swarm_install

    status = await swarm_status._skill_manager.handle_skills_skillnet_install_status(
        {"install_id": install_id}
    )
    assert status == {"success": True, "status": "pending"}
