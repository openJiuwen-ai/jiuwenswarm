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
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperationCard

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


# ---------------------------------------------------------------------------
# create_sandbox_sysop_spec id 策略：借用句柄永不注册，resolve() 时才查 owner
# ---------------------------------------------------------------------------

_OWNER_ID = "f0d3af13c98a45d084f483cf1fb4c87b"


class _FakeRegistry:
    def __init__(self, owner_map, sysops):
        self._owner_map = owner_map
        self._sysops = sysops

    def sys_operation(self):
        return SimpleNamespace(_sandbox_key_owner_map=self._owner_map)


class _FakeResourceMgr:
    """按 openjiuwen ``ResourceMgr`` 的关键语义实现，记录 remove 以便断言。"""

    def __init__(self, owner_map, sysops):
        self._resource_registry = _FakeRegistry(owner_map, sysops)
        self._sysops = sysops
        self.removed: list[str] = []

    def get_sys_operation(self, sysop_id):
        return self._sysops.get(sysop_id)

    def remove_sys_operation(self, sysop_id):
        op = self._sysops.pop(sysop_id, None)
        if op is None:
            return None
        self.removed.append(sysop_id)
        registry = self._resource_registry
        template = getattr(op, "isolation_key_template", None)
        if template:
            registry._owner_map.pop(template, None)
        return op


def _patch_resource_mgr(monkeypatch, owner_map, sysops):
    """让 Runner.resource_mgr 返回指向 owner_map / sysops 的假注册表。"""
    import openjiuwen.core.runner.runner as runner_mod

    fake = _FakeResourceMgr(owner_map, sysops)
    monkeypatch.setattr(runner_mod.Runner, "resource_mgr", fake, raising=False)
    return fake


def _owner_map_with(card):
    """按库 SysOperation 的真实算法算 key template，填 owner map。"""
    from openjiuwen.core.sys_operation.sys_operation import SysOperation

    template = SysOperation(card).isolation_key_template
    fake_sandbox = SimpleNamespace(
        id=_OWNER_ID,
        mode=OperationMode.SANDBOX,
        isolation_key_template=template,
    )
    return {template: fake_sandbox.id}, {_OWNER_ID: fake_sandbox}


def _patch_card_build(monkeypatch, card):
    """绕开 create_sandbox_sysop_card 的重策略构造，直接喂固定 card。"""
    monkeypatch.setattr(builder, "create_sandbox_sysop_card", lambda *a, **kw: card)


def _make_sandbox_card():
    from openjiuwen.core.sys_operation.config import (
        ContainerScope,
        PreDeployLauncherConfig,
        SandboxGatewayConfig,
        SandboxIsolationConfig,
    )

    return SysOperationCard(
        mode=OperationMode.SANDBOX,
        work_config=LocalWorkConfig(shell_allowlist=None),
        gateway_config=SandboxGatewayConfig(
            isolation=SandboxIsolationConfig(
                container_scope=ContainerScope.CUSTOM,
                custom_id="project_unit_ep_unit",
            ),
            launcher_config=PreDeployLauncherConfig(
                base_url="http://sandbox.invalid",
                sandbox_type="jiuwenbox",
            ),
        ),
    )


def _build_spec(monkeypatch, *, owner_map, sysops):
    """走完整 create_sandbox_sysop_spec，返回 (spec, fake_resource_mgr)。"""
    card = _make_sandbox_card()
    rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    _patch_card_build(monkeypatch, card)
    spec = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid",
        "jiuwenbox",
        team_id="unit_team",
        startup_mode="external",
    )
    assert spec is not None
    return spec, rm


def _simulate_native_harness_dispose(rm, sysop_id):
    """逐行复现 ``native_harness.py:474-475`` 的销毁判定。"""
    if rm.get_sys_operation(sysop_id) is not None:
        rm.remove_sys_operation(sysop_id)


# ---------------------------------------------------------------------------
# id 策略
# ---------------------------------------------------------------------------


def test_spec_id_is_borrowed_handle_not_owner_id(monkeypatch):
    """有已注册 owner 时 spec id 仍是借用句柄，**不得**对齐 owner id。

    对齐 owner id 虽能让 ``resolve()`` 零 add 命中，但成员销毁时 dispose 会按同一
    id 执行 ``remove_sys_operation``，连带摘掉主 agent 的沙箱并释放其 isolation
    key。句柄与 owner 解耦是消除该串扰的前提。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    spec, _rm = _build_spec(monkeypatch, owner_map=owner_map, sysops=sysops)

    assert spec.id == "team_sandbox_ref_unit_team"
    assert spec.id != _OWNER_ID
    assert spec.mode == OperationMode.SANDBOX


def test_spec_id_is_stable_ref_handle_without_owner(monkeypatch):
    """无已注册 owner（首个注册方）：id 仍为同一个借用句柄，保持稳定。"""
    spec, _rm = _build_spec(monkeypatch, owner_map={}, sysops={})

    assert spec.id == "team_sandbox_ref_unit_team"
    assert spec.mode == OperationMode.SANDBOX


def test_spec_ignores_local_owner(monkeypatch):
    """owner map 指向 LOCAL 实例时不复用（避免成员对齐到 LOCAL）。"""
    from openjiuwen.core.sys_operation.sys_operation import SysOperation

    card = _make_sandbox_card()
    template = SysOperation(card).isolation_key_template
    fake_local = SimpleNamespace(id="local_owner", mode=OperationMode.LOCAL)
    spec, _rm = _build_spec(
        monkeypatch,
        owner_map={template: fake_local.id},
        sysops={fake_local.id: fake_local},
    )

    assert spec.id == "team_sandbox_ref_unit_team"
    assert spec.resolve() is None


# ---------------------------------------------------------------------------
# resolve()：真正把成员绑到已注册沙箱的那一步
# ---------------------------------------------------------------------------


def test_resolve_returns_registered_sandbox_owner(monkeypatch):
    """有 owner 时 ``resolve()`` 必须返回那个 SANDBOX 实例——而非 None。

    这是原 bug 的行为性断言：库 ``resolve()`` 丢 ``Result`` 后静默回落 LOCAL，
    成员工具落宿主机。此处直接断言拿到的是 mode=SANDBOX 的 owner 实例。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    spec, _rm = _build_spec(monkeypatch, owner_map=owner_map, sysops=sysops)

    resolved = spec.resolve()

    assert resolved is not None, "resolve() 返回 None，成员会回落 LOCAL"
    assert resolved.id == _OWNER_ID
    assert resolved.mode == OperationMode.SANDBOX


def test_resolve_without_owner_returns_none_and_warns(monkeypatch, caplog):
    """无 owner 可复用时返回 None 并打 WARNING（= 上线前既有行为，零回归）。

    WARNING 是该降级唯一的可诊断线索，故必须响亮，不能是 debug。
    """
    spec, _rm = _build_spec(monkeypatch, owner_map={}, sysops={})

    with caplog.at_level("WARNING", logger="jiuwenswarm.server.runtime.agent_adapter.sysop_builder"):
        assert spec.resolve() is None

    assert "team sandbox owner 未注册" in caplog.text
    assert "team_sandbox_ref_unit_team" in caplog.text


# ---------------------------------------------------------------------------
# 回归锁：成员销毁不得影响主 agent 沙箱
# ---------------------------------------------------------------------------


def test_member_dispose_leaves_owner_registered(monkeypatch):
    """成员 harness 销毁后，主 agent 沙箱实例与其 isolation key 必须仍在。

    这是本次修复的核心不变量：借用句柄永不注册 ⇒ ``native_harness.dispose()``
    的 ``get_sys_operation(id) is not None`` 判定恒假 ⇒ 不会走到 remove。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    spec, rm = _build_spec(monkeypatch, owner_map=owner_map, sysops=sysops)
    template = next(iter(owner_map))

    assert spec.resolve() is not None, "前置条件：成员先成功复用 owner"

    _simulate_native_harness_dispose(rm, spec.id)

    assert rm.removed == [], f"成员销毁摘掉了非己方资源：{rm.removed}"
    assert rm.get_sys_operation(_OWNER_ID) is not None, "主 agent 沙箱被误注销"
    assert template in rm._resource_registry._owner_map, "isolation key 被误释放"


def test_id_aligned_handle_would_break_owner_on_dispose(monkeypatch):
    """反证：若改回"id 对齐 owner"，dispose 确实会误伤——证明上一条断言有牙齿。

    这条用例锁住的是设计决策本身：任何人把 id 改回 owner id 都会红。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    _rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    gateway = card.gateway_config

    bad_spec = builder.TeamSharedSandboxSpec(
        id=_OWNER_ID,  # 回归：借用句柄被改成 owner id
        mode=OperationMode.SANDBOX,
        work_config=LocalWorkConfig(shell_allowlist=None),
        gateway_config=gateway,
    )

    _simulate_native_harness_dispose(_rm, bad_spec.id)

    assert _rm.removed == [_OWNER_ID], "id 对齐 owner 时 dispose 必然误伤主 agent"
    assert _rm.get_sys_operation(_OWNER_ID) is None


# ---------------------------------------------------------------------------
# 端到端：真实 enrich 路径必须保住 TeamSharedSandboxSpec 子类
# ---------------------------------------------------------------------------


def test_enrich_preserves_team_shared_sandbox_spec_subclass(monkeypatch):
    """走真实 ``enrich_team_spec_for_swarm`` 后成员上必须是真子类而非被压平的基类。

    整个修复依赖 pydantic 在 ``assembly`` 的 ``model_copy(update=...)`` 与库的
    ``agent_configurator`` 的 ``model_copy(update=...)`` 两处都不把子类压平成
    ``SysOperationSpec``——一旦压平，``resolve()`` 回到库实现，成员又会静默
    回落 LOCAL，且无任何日志。
    """
    import jiuwenswarm.agents.swarm.assembly as asm

    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    _patch_card_build(monkeypatch, card)
    monkeypatch.setattr(asm, "get_sandbox_runtime", lambda: {"enabled": True})
    monkeypatch.setattr(asm, "is_enterprise", lambda: False)
    monkeypatch.setattr(asm, "get_sandbox_startup_mode", lambda: "external")
    monkeypatch.setattr(
        asm, "resolve_sandbox_endpoint", lambda: ("jiuwenbox", "http://sandbox.invalid")
    )

    spec = _make_team_spec()
    enrich_team_spec_for_swarm(spec, session_id="s", mode="team", channel_id="web")

    for role in ("leader", "teammate"):
        member_sysop = spec.agents[role].sys_operation
        assert isinstance(member_sysop, builder.TeamSharedSandboxSpec), (
            f"{role} 的 sys_operation 被压平为 {type(member_sysop).__name__}，"
            "resolve() 将退回库实现并静默回落 LOCAL"
        )
        assert member_sysop.id == "team_sandbox_ref_unit_team"
        assert member_sysop.resolve() is not None, f"{role} 未绑定到已注册沙箱"
        assert member_sysop.resolve().id == _OWNER_ID

    assert rm.removed == []
