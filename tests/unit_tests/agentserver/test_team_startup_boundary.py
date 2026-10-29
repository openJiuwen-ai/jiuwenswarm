# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Single-Agent startup does not borrow Team initialization side effects."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest


def test_facade_and_heartbeat_injection_do_not_import_team_sdk(tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = f"""
import importlib.abc
import sys
sys.path.insert(0, {str(root)!r})
class RejectTeam(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'openjiuwen.agent_teams' or fullname.startswith('openjiuwen.agent_teams.'):
            raise AssertionError('single Agent imported Team SDK: ' + fullname)
sys.meta_path.insert(0, RejectTeam())
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.common.heartbeat_service import get_heartbeat_job_service
facade = JiuWenSwarm()
service = object()
facade.set_heartbeat_service(service)
manager = AgentManager()
manager.set_heartbeat_service(service)
assert facade._heartbeat_service is service
assert manager._heartbeat_service is service
assert get_heartbeat_job_service() is service
assert 'openjiuwen.agent_teams' not in sys.modules
print('SINGLE_AGENT_TEAM_BOUNDARY_OK')
"""
    env = dict(os.environ, JIUWENSWARM_DATA_DIR=str(tmp_path / "data"),
               JIUWENSWARM_HOME=str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, "-I", "-c", script], env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SINGLE_AGENT_TEAM_BOUNDARY_OK" in result.stdout


@pytest.fixture
def legacy_team_storage(tmp_path, monkeypatch):
    from openjiuwen.agent_teams import paths
    from jiuwenswarm.agents.harness.team import bootstrap
    from jiuwenswarm.common import utils

    home = tmp_path / "home"
    library = home / "agent" / "workspace" / "skills"
    for name in ("skill-a", "skill-b"):
        skill = library / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    view = home / ".agent_teams" / "demo" / "workspaces" / "reader_workspace" / "skills"
    shutil.copytree(library / "skill-a", view / "skill-a")
    monkeypatch.setattr(bootstrap, "get_user_workspace_dir", lambda: home)
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: library)
    monkeypatch.setattr(utils, "_openjiuwen_skill_library", None)
    previous_home = paths.get_openjiuwen_home()
    previous_library = paths.global_skills_dir()
    try:
        yield home, library, view
    finally:
        paths.configure_openjiuwen_home(previous_home)
        paths.configure_global_skills_dir(previous_library)


def test_team_storage_migrates_before_visibility_without_organization_installers(
    legacy_team_storage, monkeypatch,
):
    from openjiuwen.agent_teams import paths
    from openjiuwen.agent_teams.skill.visibility import (
        AUTHORITY_MIGRATION, SCOPE_MEMBER, read_skill_visibility,
    )
    from jiuwenswarm.agents.harness.team import bootstrap
    from jiuwenswarm.agents.harness.team.expert_org import wiring as expert
    from jiuwenswarm.agents.harness.team.summary_org import wiring as summary

    home, library, view = legacy_team_storage
    expert_install = Mock(side_effect=AssertionError("storage must not activate Team runtime"))
    summary_install = Mock(side_effect=AssertionError("storage must not activate Team runtime"))
    monkeypatch.setattr(expert, "register_expert_adapter_installer", expert_install)
    monkeypatch.setattr(summary, "register_summary_adapter_installer", summary_install)
    bootstrap.prepare_team_storage()

    assert paths.get_openjiuwen_home() == home
    assert paths.global_skills_dir() == library
    assert not view.exists()
    document = read_skill_visibility(
        paths.member_skill_visibility_path("demo", "reader"),
        scope=SCOPE_MEMBER, entity_id="reader",
    )
    assert document.allow == ["skill-a"]
    assert document.authority == AUTHORITY_MIGRATION
    expert_install.assert_not_called()
    summary_install.assert_not_called()


def test_team_storage_rebinds_when_workspace_changes(legacy_team_storage, tmp_path, monkeypatch):
    from openjiuwen.agent_teams import paths
    from jiuwenswarm.agents.harness.team import bootstrap
    from jiuwenswarm.common import utils

    bootstrap.prepare_team_storage()
    next_home = tmp_path / "next-home"
    next_library = next_home / "agent" / "workspace" / "skills"
    monkeypatch.setattr(bootstrap, "get_user_workspace_dir", lambda: next_home)
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: next_library)

    bootstrap.prepare_team_storage()

    assert paths.get_openjiuwen_home() == next_home
    assert paths.global_skills_dir() == next_library


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "set", "update"])
async def test_first_visibility_request_migrates_before_read_or_write(
    legacy_team_storage, operation,
):
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    home, library, view = legacy_team_storage
    manager = SkillManager(workspace_dir=str(library.parent))
    params = {"scope": "member", "team_name": "demo", "member_name": "reader"}
    if operation == "set":
        params["allow"] = ["skill-b"]
    elif operation == "update":
        params["add_allow"] = ["skill-b"]
    result = await getattr(manager, f"handle_skills_visibility_{operation}")(params)

    assert result["success"] is True
    assert not view.exists()
    assert Path(result["path"]).is_relative_to(home)
    expected = {"get": ["skill-a"], "set": ["skill-b"], "update": ["skill-a", "skill-b"]}
    assert result["allow"] == expected[operation]


def test_team_assembly_prepares_storage_before_provider_consumers(legacy_team_storage, monkeypatch):
    from openjiuwen.agent_teams import paths
    from openjiuwen.agent_teams.skill.visibility import SCOPE_MEMBER, read_skill_visibility
    from jiuwenswarm.agents.swarm import assembly
    from jiuwenswarm.agents.harness.team.expert_org import wiring as expert
    from jiuwenswarm.agents.harness.team.summary_org import wiring as summary

    home, library, view = legacy_team_storage
    expert_install = Mock()
    summary_install = Mock()
    monkeypatch.setattr(expert, "register_expert_adapter_installer", expert_install)
    monkeypatch.setattr(summary, "register_summary_adapter_installer", summary_install)

    def inspect_first_consumer():
        assert paths.get_openjiuwen_home() == home
        assert paths.global_skills_dir() == library
        assert not view.exists()
        document = read_skill_visibility(
            paths.member_skill_visibility_path("demo", "reader"),
            scope=SCOPE_MEMBER, entity_id="reader",
        )
        assert document.allow == ["skill-a"]
        raise LookupError("consumer reached after migration")

    monkeypatch.setattr(assembly, "register_swarm_providers", inspect_first_consumer)
    with pytest.raises(LookupError, match="consumer reached after migration"):
        assembly.enrich_team_spec_for_swarm(object(), session_id="session", mode="team")
    expert_install.assert_called_once_with()
    summary_install.assert_called_once_with()


def test_context_heartbeat_exports_share_neutral_service():
    from jiuwenswarm.agents.swarm import context
    from jiuwenswarm.common import heartbeat_service

    previous = heartbeat_service.get_heartbeat_job_service()
    service = object()
    try:
        heartbeat_service.set_heartbeat_job_service(service)
        assert context.get_heartbeat_job_service() is service
        assert context.SwarmBuildContext.from_seed(
            {}, config={}, trajectory_span_processor=None,
        ).heartbeat_job_service is service
        context.set_heartbeat_job_service(None)
        assert heartbeat_service.get_heartbeat_job_service() is None
    finally:
        heartbeat_service.set_heartbeat_job_service(previous)
