# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for lightweight skill reload after uninstall (no full create_instance)."""

# pylint: disable=protected-access

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.harness.rails import SkillUseRail

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class _FakeAdapter:
    """Fake adapter that records calls to refresh_skill_rails / create_instance."""

    def __init__(self):
        self.refresh_called = False
        self.create_called = False
        self._skill_manager = None

    def set_skill_manager(self, mgr):
        self._skill_manager = mgr

    async def refresh_skill_rails(self):
        self.refresh_called = True

    async def create_instance(self, *args, **kwargs):
        self.create_called = True


class _FakeFs:
    async def read_file(self, path, *args, **kwargs):
        return SimpleNamespace(
            code=0,
            data=SimpleNamespace(content=Path(path).read_text(encoding="utf-8")),
        )


class _FakeSysOperation:
    def __init__(self):
        self._fs = _FakeFs()

    def fs(self):
        return self._fs


class _NoDisabledSkillManager:
    def list_execution_disabled_skills(self):
        return ["disabled_skill"]


def _write_skill(skills_root: Path, name: str, description: str) -> Path:
    skill_dir = skills_root / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
async def swarm_with_fake_adapter(monkeypatch):
    """Build a JiuWenSwarm with a fake adapter and a stubbed skill_manager."""
    swarm = JiuWenSwarm.__new__(JiuWenSwarm)
    swarm._adapter = _FakeAdapter()
    swarm._sdk_name = "test"

    # Stub skill_manager: handle_skills_uninstall returns controlled payload
    class _StubSkillManager:
        async def handle_skills_uninstall(self, params):
            if params.get("name") == "nonexistent":
                return {"success": False, "detail": "未找到 skill: nonexistent"}
            return {"success": True}

    swarm._skill_manager = _StubSkillManager()

    # Stub the live team skill-view reload (no-op)
    async def _noop_reload(session_id=None):
        return None

    swarm._reload_team_skill_rails = _noop_reload

    return swarm


@pytest.mark.asyncio
async def test_uninstall_success_triggers_light_refresh_not_full_reload(
    swarm_with_fake_adapter,
):
    """成功卸载后应走轻量 refresh_skill_rails，不触发 create_instance."""
    swarm = swarm_with_fake_adapter
    adapter = swarm._adapter

    request = AgentRequest(
        request_id="test-1",
        req_method=ReqMethod.SKILLS_UNINSTALL,
        params={"name": "docx"},
    )

    response = await swarm._handle_skills_request(request)

    assert response is not None
    assert response.ok is True
    assert response.payload == {"success": True}
    assert adapter.refresh_called is True
    assert adapter.create_called is False


@pytest.mark.asyncio
async def test_uninstall_failure_does_not_trigger_any_reload(swarm_with_fake_adapter):
    """卸载失败（skill 未找到）时不应触发轻量刷新也不应触发全量重建."""
    swarm = swarm_with_fake_adapter
    adapter = swarm._adapter

    request = AgentRequest(
        request_id="test-2",
        req_method=ReqMethod.SKILLS_UNINSTALL,
        params={"name": "nonexistent"},
    )

    response = await swarm._handle_skills_request(request)

    assert response is not None
    assert response.ok is True
    assert response.payload == {"success": False, "detail": "未找到 skill: nonexistent"}
    assert adapter.refresh_called is False
    assert adapter.create_called is False


@pytest.mark.asyncio
async def test_uninstall_refreshes_skill_rail_via_adapter(swarm_with_fake_adapter, monkeypatch):
    """验证 _refresh_skill_rails_after_change 委托到 adapter.refresh_skill_rails."""
    swarm = swarm_with_fake_adapter

    await swarm._refresh_skill_rails_after_change()

    assert swarm._adapter.refresh_called is True


@pytest.mark.asyncio
async def test_refresh_skill_rails_after_change_noop_when_adapter_none():
    """adapter 未初始化时应安全跳过."""
    swarm = JiuWenSwarm.__new__(JiuWenSwarm)
    swarm._adapter = None

    await swarm._refresh_skill_rails_after_change()


@pytest.mark.asyncio
async def test_refresh_skill_rails_removes_deleted_skill_from_real_skill_use_rail(tmp_path, monkeypatch):
    """Real SkillUseRail.reload_skills should evict deleted skill cache entries."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    deleted_skill_dir = _write_skill(skills_root, "delete_me", "deleted skill")
    _write_skill(skills_root, "keep_me", "kept skill")
    _write_skill(skills_root, "disabled_skill", "disabled skill")

    skill_rail = SkillUseRail(str(skills_root), include_tools=False)
    skill_rail.set_sys_operation(_FakeSysOperation())
    await skill_rail.reload_skills()

    assert [skill.name for skill in skill_rail.skills] == ["delete_me", "disabled_skill", "keep_me"]
    assert any("delete_me" in key for key in skill_rail._skill_cache)

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._skill_manager = _NoDisabledSkillManager()
    adapter._skill_rail = skill_rail
    adapter._skill_evolution_rail = None

    # refresh_skill_rails rebuilds scan roots via _skill_scan_dirs(), which
    # always prepends get_agent_skills_dir() (the global skills dir). Point
    # it at our temp skills_root so no real global skills leak into the rail.
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_agent_skills_dir",
        lambda: skills_root,
    )

    shutil.rmtree(deleted_skill_dir)

    await adapter.refresh_skill_rails()

    assert [skill.name for skill in skill_rail.skills] == ["keep_me"]
    assert skill_rail.disabled_skills == {"disabled_skill"}
    assert all("delete_me" not in key for key in skill_rail._skill_cache)
    assert all("delete_me" not in key for key in skill_rail._skill_order)
    assert skill_rail._skills_snapshot_signature == skill_rail._build_skills_snapshot_signature()
