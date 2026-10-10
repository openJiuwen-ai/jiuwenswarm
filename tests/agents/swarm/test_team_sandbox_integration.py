# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Team 模式接入沙箱的单元测试。

覆盖两层改动：
1. ``build_filesystem_policy`` 把 ``~/.jiuwenswarm/.agent_teams`` 作为 rw 挂载
   纳入沙箱策略（Option A），保证 team 成员 workspace 在共享沙箱内可写。
2. ``enrich_team_spec_for_swarm`` 在个人版 + 沙箱启用时给每个成员注入共享的
   SANDBOX ``sys_operation`` spec；禁用 / 企业版时不注入（成员维持 LOCAL）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.core.sys_operation import OperationMode

from jiuwenswarm.agents.swarm import enrich_team_spec_for_swarm
from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder


# ---------------------------------------------------------------------------
# Option A: .agent_teams rw mount
# ---------------------------------------------------------------------------


def test_build_filesystem_policy_mounts_agent_teams_rw(tmp_path, monkeypatch):
    """build_filesystem_policy 必须把 get_agent_teams_home() 作为 rw bind 挂载。"""
    from openjiuwen.agent_teams import paths as at_paths

    agent_teams_home = tmp_path / ".agent_teams"
    monkeypatch.setattr(at_paths, "get_agent_teams_home", lambda: agent_teams_home)
    # 屏蔽其它宿主路径解析，使 bind_mounts 里只有 .agent_teams 这一条。
    monkeypatch.setattr(builder, "_resolve_shared_dir", lambda shared_dir=None: None)
    monkeypatch.setattr(builder, "_resolve_project_dir", lambda project=None: None)
    monkeypatch.setattr(builder, "_resolve_config_ro_path", lambda: None)

    policy, _upload_list = builder.build_filesystem_policy(
        None, project_dir=None, startup_mode="external", shared_dir=None,
    )

    bind_mounts = policy["filesystem_policy"].get("bind_mounts") or []
    assert any(
        m["mode"] == "rw" and m["host_path"] == str(agent_teams_home.resolve())
        for m in bind_mounts
    ), f".agent_teams rw mount missing in bind_mounts: {bind_mounts}"


def test_build_filesystem_policy_creates_agent_teams_home_if_absent(tmp_path, monkeypatch):
    """挂载前目录不存在时应自动 mkdir，不报错。"""
    from openjiuwen.agent_teams import paths as at_paths

    agent_teams_home = tmp_path / "nested" / ".agent_teams"
    assert not agent_teams_home.exists()
    monkeypatch.setattr(at_paths, "get_agent_teams_home", lambda: agent_teams_home)
    monkeypatch.setattr(builder, "_resolve_shared_dir", lambda shared_dir=None: None)
    monkeypatch.setattr(builder, "_resolve_project_dir", lambda project=None: None)
    monkeypatch.setattr(builder, "_resolve_config_ro_path", lambda: None)

    builder.build_filesystem_policy(
        None, project_dir=None, startup_mode="external", shared_dir=None,
    )

    assert agent_teams_home.is_dir()


# ---------------------------------------------------------------------------
# Injection: enrich_team_spec_for_swarm sets member sys_operation
# ---------------------------------------------------------------------------


def _make_team_spec() -> TeamAgentSpec:
    return TeamAgentSpec(
        agents={"leader": DeepAgentSpec(), "teammate": DeepAgentSpec()},
        team_name="unit_team",
        leader=LeaderSpec(member_name="team_leader"),
    )


def _patch_sandbox(monkeypatch, *, enabled: bool, enterprise: bool, spec=None):
    import jiuwenswarm.agents.swarm.assembly as asm

    monkeypatch.setattr(asm, "get_sandbox_runtime", lambda: {"enabled": enabled})
    monkeypatch.setattr(asm, "is_enterprise", lambda: enterprise)
    monkeypatch.setattr(asm, "get_sandbox_startup_mode", lambda: "external")
    monkeypatch.setattr(
        asm, "resolve_sandbox_endpoint", lambda: ("jiuwenbox", "http://sandbox.invalid")
    )
    monkeypatch.setattr(asm, "create_sandbox_sysop_spec", lambda *a, **kw: spec)


def test_enrich_injects_sandbox_spec_when_enabled(monkeypatch):
    """个人版 + 沙箱启用：每个成员注入共享 SANDBOX sys_operation spec。"""
    fake_spec = SimpleNamespace(
        id="team_sandbox_unit_team", mode=OperationMode.SANDBOX,
    )
    _patch_sandbox(monkeypatch, enabled=True, enterprise=False, spec=fake_spec)

    spec = _make_team_spec()
    enrich_team_spec_for_swarm(spec, session_id="s", mode="team", channel_id="web")

    for role in ("leader", "teammate"):
        assert spec.agents[role].sys_operation is fake_spec, f"{role} not injected"
        assert spec.agents[role].sys_operation.mode == OperationMode.SANDBOX


def test_enrich_skips_sandbox_when_disabled(monkeypatch):
    """沙箱禁用：不注入，成员 sys_operation 保持未设置。"""
    _patch_sandbox(monkeypatch, enabled=False, enterprise=False, spec=None)

    spec = _make_team_spec()
    enrich_team_spec_for_swarm(spec, session_id="s", mode="team", channel_id="web")

    for role in ("leader", "teammate"):
        assert spec.agents[role].sys_operation is None


def test_enrich_skips_sandbox_for_enterprise(monkeypatch):
    """企业版：跳过注入（成员维持 LOCAL，无回归）——shared_dir 此层不可得。"""
    fake_spec = SimpleNamespace(id="team_sandbox_unit_team", mode=OperationMode.SANDBOX)
    _patch_sandbox(monkeypatch, enabled=True, enterprise=True, spec=fake_spec)

    spec = _make_team_spec()
    enrich_team_spec_for_swarm(spec, session_id="s", mode="team", channel_id="web")

    for role in ("leader", "teammate"):
        assert spec.agents[role].sys_operation is None


def test_enrich_skips_sandbox_when_endpoint_unresolved(monkeypatch):
    """sandbox_url/type 解析失败（runner 未起）时不注入，避免建出无效 spec。"""
    import jiuwenswarm.agents.swarm.assembly as asm

    fake_spec = SimpleNamespace(id="team_sandbox_unit_team", mode=OperationMode.SANDBOX)
    monkeypatch.setattr(asm, "get_sandbox_runtime", lambda: {"enabled": True})
    monkeypatch.setattr(asm, "is_enterprise", lambda: False)
    monkeypatch.setattr(asm, "get_sandbox_startup_mode", lambda: "external")
    monkeypatch.setattr(asm, "resolve_sandbox_endpoint", lambda: (None, None))
    monkeypatch.setattr(asm, "create_sandbox_sysop_spec", lambda *a, **kw: fake_spec)

    spec = _make_team_spec()
    enrich_team_spec_for_swarm(spec, session_id="s", mode="team", channel_id="web")

    for role in ("leader", "teammate"):
        assert spec.agents[role].sys_operation is None
