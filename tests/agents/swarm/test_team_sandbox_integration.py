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
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec, SysOperationSpec
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperation,
    SysOperationCard,
)

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
# create_sandbox_sysop_spec id 策略：借用句柄别名 id（keyless），resolve() 时登记
# ---------------------------------------------------------------------------

_OWNER_ID = "f0d3af13c98a45d084f483cf1fb4c87b"


class _FakeTagMgr:
    """``ResourceMgr._tag_mgr`` 的最小替身。

    库 ``_inner_get_resources`` 要求 ``_tag_mgr.has_resource(id)`` 才 dispatch
    （``resource_manager.py:171``）——只写 ``_sys_operations`` 是隐形的，
    故假注册表必须照抄这道门，否则测试会漏掉"登记了但查不到"。
    """

    def __init__(self, seeded=()):
        self._tagged = set(seeded)

    def has_resource(self, resource_id):
        return resource_id in self._tagged

    def tag_resource(self, resource_id, tag=None):
        self._tagged.add(resource_id)

    def remove_resource(self, resource_id):
        self._tagged.discard(resource_id)


class _FakeSysOpMgr:
    """``SysOperationMgr`` 的最小替身：照抄 key 冲突与 row pop 两条真实语义。

    - add：同 isolation key 已有**别的** id 时抛 ValueError（一个 key 一个 owner）；
    - remove：无条件 pop 该实例所声明 key 的 owner 行 —— 这正是借用句柄必须
      keyless 的原因（``sys_operation_manager.py:67-68``）。
    """

    def __init__(self, owner_map, sysops):
        self._sandbox_key_owner_map = owner_map
        self._sys_operations = sysops

    def add_sys_operation(self, sys_operation_id, sys_operation_instance):
        if sys_operation_id in self._sys_operations:
            raise ValueError(f"already exists sys_operation_card {sys_operation_id}")
        template = sys_operation_instance.isolation_key_template
        if template:
            existing = self._sandbox_key_owner_map.get(template)
            if existing is not None and existing != sys_operation_id:
                raise ValueError(
                    f"Isolation key template '{template}' is already registered by "
                    f"operation '{existing}'."
                )
            self._sandbox_key_owner_map[template] = sys_operation_id
        self._sys_operations[sys_operation_id] = sys_operation_instance

    def remove_sys_operation(self, sys_operation_id):
        instance = self._sys_operations.pop(sys_operation_id, None)
        if instance is not None and instance.isolation_key_template:
            self._sandbox_key_owner_map.pop(instance.isolation_key_template, None)
        return instance

    def get_sys_operation(self, sys_operation_id):
        return self._sys_operations.get(sys_operation_id)


class _FakeRegistry:
    def __init__(self, owner_map, sysops):
        self._owner_map = owner_map
        self._sysops = sysops

    def sys_operation(self):
        return _FakeSysOpMgr(self._owner_map, self._sysops)


class _FakeOk:
    """openjiuwen ``Ok`` 的最小替身。"""

    def __init__(self, value=None):
        self._value = value

    def is_err(self):
        return False

    def is_ok(self):
        return True

    def msg(self):
        return ""

    def error(self):
        return None


class _FakeErr:
    """openjiuwen ``Error`` 的最小替身。"""

    def __init__(self, msg="boom"):
        self._msg = msg

    def is_err(self):
        return True

    def is_ok(self):
        return False

    def msg(self):
        return self._msg

    def error(self):
        return ValueError(self._msg)


class _FakeResourceMgr:
    """按 openjiuwen ``ResourceMgr`` 的关键语义实现，记录变更以便断言。"""

    def __init__(self, owner_map, sysops):
        self._resource_registry = _FakeRegistry(owner_map, sysops)
        self._sysops = sysops
        # 初始 sysops 视为已登记资源（库只有经 ResourceMgr.add 才盖章）
        self._tag_mgr = _FakeTagMgr(seeded=list(sysops))
        self._id_to_card: dict[str, object] = {}
        self.removed: list[str] = []
        self.added: list[str] = []
        #: 测试可置为 False 让 add 一律失败（验证 Result 检查路径）
        self.add_succeeds = True

    def get_sys_operation(self, sysop_id=None, **_kwargs):
        # 无参调用（库按 GLOBAL tag 列全部）是沙箱开关 teardown 的用法
        if sysop_id is None:
            return [
                op for rid, op in self._sysops.items() if self._tag_mgr.has_resource(rid)
            ]
        if not self._tag_mgr.has_resource(sysop_id):
            return None
        return self._resource_registry.sys_operation().get_sys_operation(sysop_id)

    def add_sys_operation(self, card):
        if not self.add_succeeds:
            return _FakeErr("simulated add failure")
        # 存**真实** SysOperation 实例（而非SimpleNamespace 替身）：借用句柄
        # ``_BorrowedSandboxHandle`` 会读 source._run_config / source._instances 并
        # 委托 fs()/shell()，替身没有这些成员。
        instance = SysOperation(card)
        try:
            self._resource_registry.sys_operation().add_sys_operation(card.id, instance)
        except ValueError as exc:
            # 与库一致：key 冲突 / id 重复被吞成 Error 而非抛出
            return _FakeErr(str(exc))
        self.added.append(card.id)
        self._tag_mgr.tag_resource(card.id)
        self._id_to_card[card.id] = card
        return _FakeOk(card.id)

    def remove_sys_operation(self, sysop_id):
        if not self._tag_mgr.has_resource(sysop_id):
            # 库对未盖章 id 不 dispatch；此处保持"摘不到"的语义
            return None
        self._tag_mgr.remove_resource(sysop_id)
        op = self._resource_registry.sys_operation().remove_sys_operation(sysop_id)
        self._id_to_card.pop(sysop_id, None)
        if op is None:
            return None
        self.removed.append(sysop_id)
        return op


def _patch_resource_mgr(monkeypatch, owner_map, sysops):
    """让 Runner.resource_mgr 返回指向 owner_map / sysops 的假注册表。"""
    import openjiuwen.core.runner.runner as runner_mod

    fake = _FakeResourceMgr(owner_map, sysops)
    monkeypatch.setattr(runner_mod.Runner, "resource_mgr", fake, raising=False)
    return fake


def _owner_map_with(card):
    """按库 SysOperation 的真实算法算 key template，填 owner map。

    owner 用**真实** ``SysOperation`` 实例（id 改写成 ``_OWNER_ID``）而非
    SimpleNamespace：``_BorrowedSandboxHandle`` 会读 source._run_config /
    source._instances 并委托 fs()/shell()，替身没有这些成员。
    """
    owner = SysOperation(card.model_copy(update={"id": _OWNER_ID}))
    return {owner.isolation_key_template: _OWNER_ID}, {_OWNER_ID: owner}


def _patch_card_build(monkeypatch, card):
    """绕开 create_sandbox_sysop_card 的重策略构造，直接喂固定 card。"""
    monkeypatch.setattr(builder, "create_sandbox_sysop_card", lambda *a, **kw: card)


def _make_sandbox_card(custom_id: str = "project_unit_ep_unit"):
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
                custom_id=custom_id,
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
    """逐行复现 ``native_harness.py:467-474`` 的销毁判定。

    ⚠️ 传入的必须是 ``spec.resolve().id``（**实例** id），不是 ``spec.id``。
    ``deep_agent_spec.py:587`` 用 ``resolve()`` 的返回值整个替换 spec，
    ``factory.py:391`` 再把它塞进 DeepAgentConfig，dispose 读的是这个实例的
    ``.id``——``spec.id`` 在那一刻已被丢弃，从未进入任何注销判定。传错参数
    会让本用例恒真（传 spec.id 查不到任何东西，判定必然为假），是假阴性的直接来源。
    """
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
    """owner map 指向 LOCAL 实例时不复用（避免成员对齐到 LOCAL）。

    LOCAL 实例在真实库里**永远不会**占 isolation key 行——``SysOperation.__init__``
    对 LOCAL 走 ``_run_config = card.work_config``（``LocalWorkConfig``，无
    ``isolation_key_template``），``add_sys_operation`` 的写行分支不执行。这里手工
    塞一行是"被污染的行"场景：成员**不得**借用它；该行同时会挡住自持 add（库强制
    一个 key 一个 owner），成员因此回落 LOCAL——降级的告警由
    ``test_resolve_returns_none_when_no_owner_and_add_fails`` 覆盖。
    """
    from openjiuwen.core.sys_operation.sys_operation import SysOperation

    card = _make_sandbox_card()
    template = SysOperation(card).isolation_key_template
    fake_local = SimpleNamespace(id="local_owner", mode=OperationMode.LOCAL)
    spec, rm = _build_spec(
        monkeypatch,
        owner_map={template: fake_local.id},
        sysops={fake_local.id: fake_local},
    )

    assert spec.id == "team_sandbox_ref_unit_team"
    assert builder.registered_sandbox_owner_by_key(template) is None, (
        "指向 LOCAL 的 owner 行不得被复用"
    )

    assert spec.resolve() is None, "被污染的 owner 行挡住自持 add 时应回落 LOCAL"
    assert rm.added == [], f"不该新建任何 card 注册：{rm.added}"
    assert rm.get_sys_operation("team_sandbox_owned_unit_team") is None


# ---------------------------------------------------------------------------
# resolve()：真正把成员绑到已注册沙箱的那一步
# ---------------------------------------------------------------------------


def test_resolve_returns_registered_sandbox_owner(monkeypatch):
    """有 owner 时 ``resolve()`` 必须**借用**那个 SANDBOX 实例——而非 None。

    这是原 bug 的行为性断言：库 ``resolve()`` 丢 ``Result`` 后静默回落 LOCAL，
    成员工具落宿主机。此处直接断言拿到的是 mode=SANDBOX 的 owner 实例。

    关键不变式：``resolve()`` 返回的是 :class:`_BorrowedSandboxHandle` 视图，
    它的 ``.id`` 是**与 owner 解耦的别名 id**（dispose 唯一读到的 id），底层绑定的
    仍是 owner 实例。返回 owner 本身会让成员 dispose 摘掉主 agent 的沙箱。

    别名**必须已登记**：库有多条"按 id 回查注册表"的链路
    （``deep_agent.py:879`` → ``react_agent.py:646`` 的 ContextEngine、
    ``skill_manager.py:66``），未登记时它们全部拿到 ``None``——成员 offload 写
    退化成宿主 ``open()``（绕沙箱）而读恒返回 ``[]``。但别名**不拥有** isolation key，
    否则 dispose 时会被 ``sys_operation_manager.py:67-68`` 连带摘掉 owner 行。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    spec, rm = _build_spec(monkeypatch, owner_map=owner_map, sysops=sysops)

    resolved = spec.resolve()

    assert resolved is not None, "resolve() 返回 None，成员会回落 LOCAL"
    assert resolved.mode == OperationMode.SANDBOX
    assert resolved.id == "team_sandbox_ref_unit_team", (
        "返回的实例 .id 必须是指向别名的视图，而非 owner id"
    )
    # 按 id 回查必须命中，且拿到的正是与 owner 共享容器配置的那一个
    by_id = rm.get_sys_operation(resolved.id)
    assert by_id is not None, "别名未登记：库按 id 回查 ContextEngine / SkillManager 会拿到 None"
    assert by_id._run_config is sysops[_OWNER_ID]._run_config, "别名必须复用 owner 的容器配置"
    # 别名不拥有 key：dispose 摘它时才不会 pop owner 行
    assert by_id.isolation_key_template is None, "别名不得声明 isolation key"
    assert rm._resource_registry._owner_map[next(iter(owner_map))] == _OWNER_ID, (
        "别名登记不得顶掉 owner 行"
    )
    assert rm.get_sys_operation(_OWNER_ID) is sysops[_OWNER_ID], "底层 owner 未被动过"


def test_resolve_without_owner_uses_team_owned_fallback(monkeypatch):
    """无 owner 时必须**自持一个 team 沙箱**，而不是直接落回 LOCAL。

    这是 db988de28 引入的回退点：owner 查不到时直接返回 None，成员工具会落
    宿主机。而分布式 bootstrap 下两侧 project_dir 不同源，key 必然不匹配，
    必然走这条路径——故必须有兑底。
    """
    spec, rm = _build_spec(monkeypatch, owner_map={}, sysops={})

    resolved = spec.resolve()

    assert resolved is not None, "无 owner 时不得回落 LOCAL"
    assert resolved.mode == OperationMode.SANDBOX
    assert resolved.id == "team_sandbox_ref_unit_team", "自持沙箱同样要包借用句柄"
    assert rm.added == ["team_sandbox_owned_unit_team"]
    assert rm.get_sys_operation("team_sandbox_owned_unit_team") is not None


def test_resolve_returns_none_when_no_owner_and_add_fails(monkeypatch, caplog):
    """自持注册**失败**时才回落 LOCAL，并打 WARNING（Result 检查路径）。

    注意：库 SysOperationSpec.resolve（deep_agent_spec.py:259）恰恰漏检 Result，
    同 key 冲突被静默吞成 None 后回落 LOCAL 且无日志——此处必须区别对待。
    """
    spec, rm = _build_spec(monkeypatch, owner_map={}, sysops={})
    rm.add_succeeds = False

    with caplog.at_level("WARNING", logger="jiuwenswarm.server.runtime.agent_adapter.sysop_builder"):
        assert spec.resolve() is None

    assert "team 自持沙箱注册失败" in caplog.text
    assert "team_sandbox_owned_unit_team" in caplog.text


def test_team_owned_sandbox_registered_once(monkeypatch):
    """多个成员各自 resolve，只应 add 一次自持沙箱（全队共用）。"""
    card = _make_sandbox_card()
    rm = _patch_resource_mgr(monkeypatch, {}, {})
    _patch_card_build(monkeypatch, card)

    first = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="unit_team", startup_mode="external",
    )
    second = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="unit_team", startup_mode="external",
    )
    first.resolve()
    second.resolve()

    assert rm.added == ["team_sandbox_owned_unit_team"], f"自持沙箱被重复注册：{rm.added}"
    assert builder.release_team_owned_sandbox("unit_team") is None


def test_member_dispose_does_not_remove_team_owned_sandbox(monkeypatch):
    """**走兜底后**，成员 dispose 仍不得摘掉自持沙箱。

    自持沙箱全队共用一个注册 id：一个成员退出会打断其余所有人。dispose 摘的是那个
    **keyless 别名**（别名登记只是为了让库按 id 回查能命中），摘它不 pop owner 行，
    故 ``team_sandbox_owned_*`` 条目分毫不动。本条与
    ``test_member_dispose_leaves_owner_registered`` 一起锁住两条分支各自的不变式。
    """
    spec, rm = _build_spec(monkeypatch, owner_map={}, sysops={})
    resolved = spec.resolve()
    assert resolved is not None, "前置条件：先成功自持"

    _simulate_native_harness_dispose(rm, resolved.id)

    assert rm.removed == ["team_sandbox_ref_unit_team"], (
        f"dispose 只该摘别名自己：{rm.removed}"
    )
    assert rm.get_sys_operation("team_sandbox_owned_unit_team") is not None, (
        "自持沙箱被摘掉了"
    )


def test_member_dispose_keeps_owner_row_and_key_lookup(monkeypatch):
    """dispose 摘别名后，**owner 行与按 key 回查必须完好**。

    这是别名必须 keyless 的直接理由：库 ``SysOperationMgr.remove_sys_operation``
    会无条件 pop 掉被删实例所声明 isolation key 的 owner 行
    （``sys_operation_manager.py:67-68``）。若别名声明真实 key，成员 dispose 就会把
    owner 无主化——下一个成员按 key 查 owner 落空 → 自持新容器 → 静默易主。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    spec, rm = _build_spec(monkeypatch, owner_map=owner_map, sysops=sysops)
    resolved = spec.resolve()
    assert resolved is not None
    key_template = next(iter(owner_map))

    _simulate_native_harness_dispose(rm, resolved.id)

    assert rm.get_sys_operation(resolved.id) is None, "别名应已注销"
    assert rm.get_sys_operation(_OWNER_ID) is sysops[_OWNER_ID], "dispose 摘掉了 owner"
    assert rm._resource_registry._owner_map.get(key_template) == _OWNER_ID, (
        "owner 行被连带摘掉 → 下一个成员会自持新容器"
    )
    owner = builder.registered_sandbox_owner_by_key(key_template)
    assert owner is sysops[_OWNER_ID], "按 key 回查必须仍能命中 owner"

    # 下一个成员重新 resolve：仍借到 owner，而不是自持新容器
    again = spec.resolve()
    assert again is not None
    assert again._run_config is sysops[_OWNER_ID]._run_config, "下一个成员丢了 owner 容器"
    assert rm.added == [], f"不该新增任何 card 注册：{rm.added}"



def test_sibling_member_survives_peer_dispose(monkeypatch):
    """成员 A dispose 后，成员 B 的绑定必须仍然可用（自持沙箱是全队共用）。

    ⚠️ 已知的共享别名窗口：全队共用**一个**别名 id，而 dispose 由库驱动
    （``native_harness.dispose`` 读 ``config.sys_operation.id`` 就摘），无法在库外
    拦下。故 A 退出会把别名那条一并摘掉。影响面被限定在三处，且都比"从不登记"
    更好：B 的 ContextEngine 持的是**句柄对象**（不受影响）；B 若重建 harness 会在
    ``resolve()`` 里重新登记（顺序保证：先 resolve 再 ``apply_deep_agent_parts``）；
    唯一残留是 B 在 A 退出之后、自身重建之前去按 id 回查（如
    ``SkillManager._load_yaml``），那与本次改动之前一致，不算回归。
    """
    rm = _patch_resource_mgr(monkeypatch, {}, {})
    _patch_card_build(monkeypatch, _make_sandbox_card())
    specs = [
        builder.create_sandbox_sysop_spec(
            "http://sandbox.invalid", "jiuwenbox", team_id="unit_team",
            startup_mode="external",
        )
        for _ in range(2)
    ]
    handle_a, handle_b = (s.resolve() for s in specs)
    assert handle_a is not None and handle_b is not None, "前置条件：两名成员均自持成功"

    _simulate_native_harness_dispose(rm, handle_a.id)

    owned = rm.get_sys_operation("team_sandbox_owned_unit_team")
    assert owned is not None, "A 退出会摘掉 B 仍在用的自持沙箱"
    assert handle_b.shell() is owned.shell(), "B 的绑定应仍指向同一个容器连接"
    # B 重建（每次 TeamHarness.build 都会 resolve）后别名重新登记
    re_registered = specs[1].resolve()
    assert re_registered is not None
    assert rm.get_sys_operation(handle_b.id) is not None, "B 重建后别名未重新登记"


def test_sandbox_toggle_teardown_also_removes_keyless_alias(monkeypatch):
    """关沙箱开关时，keyless 别名（mode=SANDBOX）也必须被清掉，LOCAL 留下。

    别名刻意声明 ``isolation_key_template = None``（否则成员 dispose 会连带 pop 掉主
    agent 的 key 归属）。只按 key 过滤的旧判据会把它当成 local 留下 → 关开关后仍能经
    ``get_sys_operation()`` 无参兜底命中沙箱实例，正是该 teardown 要防的事。
    """
    from jiuwenswarm.server import sandbox_config_rpc

    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    _patch_card_build(monkeypatch, card)
    spec = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="unit_team", startup_mode="external",
    )
    handle = spec.resolve()
    assert handle is not None
    rm.add_sys_operation(
        SysOperationCard(
            id="local_keep.sysop",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(shell_allowlist=None),
        )
    )
    template = next(iter(owner_map))

    removed = sandbox_config_rpc._teardown_registered_sandbox_sysops()

    assert removed == 2, f"owner 与别名都该清掉，实际 {removed}"
    assert rm.get_sys_operation(handle.id) is None, "keyless 别名被当成 local 留下了"
    assert rm.get_sys_operation(_OWNER_ID) is None
    assert rm.get_sys_operation("local_keep.sysop") is not None, "local sysop 不该被动"
    assert rm._resource_registry._owner_map.get(template) is None


def test_resolve_is_idempotent_across_members(monkeypatch):
    """全队共用一个别名 id：N 个成员 resolve 只登记一次，owner 不换人。"""
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    _patch_card_build(monkeypatch, card)
    spec = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="unit_team", startup_mode="external",
    )

    handles = [spec.resolve() for _ in range(3)]

    assert all(h is not None for h in handles)
    assert rm.added == [], f"复用 owner 时不该新增 card 注册：{rm.added}"
    assert rm.removed == [], f"重复 resolve 不该反复摘登：{rm.removed}"
    registered = rm.get_sys_operation("team_sandbox_ref_unit_team")
    assert registered is not None
    assert registered._run_config is sysops[_OWNER_ID]._run_config
    assert all(h._run_config is registered._run_config for h in handles), (
        "三次 resolve 必须指向同一个容器配置"
    )
    assert rm._resource_registry._owner_map[next(iter(owner_map))] == _OWNER_ID


def test_owner_teardown_cascades_to_its_aliases(monkeypatch):
    """owner 被 adapter cleanup 注销时，委托给它的别名必须联动摘除（P1）。

    别名注册不知道 owner 的生命周期：owner 先亡时别名悬空（非 None 但无人认领）。
    悬空别名不是"拿到 None"的回归——句柄共享的 ``_run_config``/``_instances`` 是
    Python 对象，解引用依然有效——问题是残留注册会被无参兜底命中。联动清理按
    ``_run_config`` 同一性精确摘除，不波及其他 team 的别名。
    """
    from openjiuwen.core.sys_operation.sys_operation import SysOperation

    card_a = _make_sandbox_card(custom_id="cascade_a")
    owner_map_a, sysops_a = _owner_map_with(card_a)
    # 第二个 team 的别名指向别的 owner，不得被误伤
    card_b = _make_sandbox_card(custom_id="cascade_b")
    owner_b = SysOperation(card_b.model_copy(update={"id": "owner_b"}))
    owner_map_a[owner_b.isolation_key_template] = "owner_b"
    sysops_a["owner_b"] = owner_b

    rm = _patch_resource_mgr(monkeypatch, owner_map_a, sysops_a)
    _patch_card_build(monkeypatch, card_a)
    spec_a = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="team_a", startup_mode="external",
    )
    # team_b 的 spec 指向 owner_b（手工构造，绕开 _patch_card_build 的固定 card）
    spec_b = builder.TeamSharedSandboxSpec(
        id="team_sandbox_ref_team_b",
        mode=OperationMode.SANDBOX,
        work_config=LocalWorkConfig(shell_allowlist=None),
        gateway_config=card_b.gateway_config,
        team_id="team_b",
    )
    alias_a = spec_a.resolve()
    alias_b = spec_b.resolve()
    assert alias_a is not None and alias_b is not None

    removed = builder.release_borrowed_sandbox_aliases_of(sysops_a[_OWNER_ID])

    assert removed == 1, f"只该摘 team_a 的别名，实际 {removed}"
    assert rm.get_sys_operation(alias_a.id) is None, "team_a 的别名未随 owner 联动摘除"
    assert rm.get_sys_operation(alias_b.id) is not None, "误摘了 team_b 的别名"
    assert rm.get_sys_operation("owner_b") is owner_b
    # 幂等
    assert builder.release_borrowed_sandbox_aliases_of(sysops_a[_OWNER_ID]) == 0


def test_flattened_subagent_spec_self_heals_via_registered_alias(monkeypatch):
    """压平的 subagent spec（rebind 不下潜 subagents）经库基类 resolve() 自愈（P3b）。

    ``SubAgentSpec.build():406`` 对嵌套的 ``sys_operation`` 调库基类 ``resolve()``——
    get-or-create 恰好命中父成员先注册的别名：拿到共享 owner 容器的句柄，dispose 时
    keyless 不伤 owner 行。故 rebind 只扫 ``spec.agents`` 顶层是**够**的——前提是
    父成员先构建（实际顺序由 ``resolve_parts`` 先成员后 subagents 保证）。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    _patch_card_build(monkeypatch, card)
    spec = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="unit_team", startup_mode="external",
    )
    assert spec.resolve() is not None, "前置条件：父成员先注册别名"

    # 压平的 subagent spec（无 team_id、无 rebind）
    from openjiuwen.harness.schema.deep_agent_spec import SysOperationSpec as _Base

    flat = _Base(
        id="team_sandbox_ref_unit_team",
        mode=OperationMode.SANDBOX,
        work_config=LocalWorkConfig(shell_allowlist=None),
        gateway_config=card.gateway_config,
    )
    resolved = flat.resolve()

    assert type(resolved).__name__ == "_BorrowedSandboxHandle", (
        f"自愈失败：{type(resolved).__name__}"
    )
    assert resolved._run_config is sysops[_OWNER_ID]._run_config, "未借到 owner 容器"
    assert resolved.isolation_key_template is None
    _simulate_native_harness_dispose(rm, resolved.id)
    assert rm.get_sys_operation(_OWNER_ID) is sysops[_OWNER_ID], "subagent dispose 误伤 owner"
    assert rm._resource_registry._owner_map[next(iter(owner_map))] == _OWNER_ID


def test_register_refreshes_stale_alias_when_owner_changed(monkeypatch):
    """owner 换容器后重建成员：别名必须刷新到新容器（P4 刷新路径）。

    摘旧登新整段同步、无 await 边界，不存在可观察的半登记窗口；旧别名指向已废弃
    容器，刷新只会更好。
    """
    from openjiuwen.core.sys_operation.sys_operation import SysOperation

    card_v1 = _make_sandbox_card(custom_id="refresh_v1")
    owner_map, sysops = _owner_map_with(card_v1)
    rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)

    # owner v1：别名登记指向 v1
    _patch_card_build(monkeypatch, card_v1)
    spec_v1 = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="unit_team", startup_mode="external",
    )
    assert spec_v1.resolve() is not None
    alias_v1 = rm.get_sys_operation("team_sandbox_ref_unit_team")
    assert alias_v1._run_config is sysops[_OWNER_ID]._run_config

    # owner v2：新容器（新 id、新 key）。走注册表 API 换血——与真实 re-register 一致
    # （直接改 sysops 字典会绕过 tag 盖章，get 就看不见了）。
    card_v2 = _make_sandbox_card(custom_id="refresh_v2")
    rm.remove_sys_operation(_OWNER_ID)
    rm.add_sys_operation(card_v2.model_copy(update={"id": "owner_v2"}))
    owner_v2 = rm.get_sys_operation("owner_v2")
    assert owner_v2 is not None, "前置条件：owner v2 已注册"

    spec_v2 = builder.TeamSharedSandboxSpec(
        id="team_sandbox_ref_unit_team",
        mode=OperationMode.SANDBOX,
        work_config=LocalWorkConfig(shell_allowlist=None),
        gateway_config=card_v2.gateway_config,
        team_id="unit_team",
    )
    handle_v2 = spec_v2.resolve()

    refreshed = rm.get_sys_operation("team_sandbox_ref_unit_team")
    assert refreshed is not None
    assert refreshed._run_config is owner_v2._run_config, "别名未刷新到新容器"
    assert refreshed._run_config is handle_v2._run_config
    assert rm._resource_registry._owner_map[owner_v2.isolation_key_template] == "owner_v2"


def test_release_removes_team_owned_sandbox(monkeypatch):
    """release 后 registry 与记账表都清空（自持 id + 借用别名一并回收）。"""
    spec, rm = _build_spec(monkeypatch, owner_map={}, sysops={})
    assert spec.resolve() is not None

    builder.release_team_owned_sandbox("unit_team")

    # 记账表是 set，遍历顺序不定
    assert sorted(rm.removed) == [
        "team_sandbox_owned_unit_team",
        "team_sandbox_ref_unit_team",
    ]
    assert rm.get_sys_operation("team_sandbox_owned_unit_team") is None
    assert rm.get_sys_operation("team_sandbox_ref_unit_team") is None


def test_release_requires_runtime_scoped_name(monkeypatch):
    """**只**按运行态全名精确释放；base 名查不到即不释放（宁漏勿误杀）。

    记账表的键是 ``team_id``，而 ``assembly`` 在 ``_apply_session_scoped_team_name``
    之后才调 enrich，故该值就是 ``{base}_{suffix}`` 运行态名。删除入口拿到的却是
    base 名——按前缀反推会误杀（见下一条），所以这里刻意不接受 base 名。
    """
    rm = _patch_resource_mgr(monkeypatch, {}, {})
    _patch_card_build(monkeypatch, _make_sandbox_card(custom_id="p_abc"))
    spec = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="abc_sess1", startup_mode="external",
    )
    assert spec.resolve() is not None

    builder.release_team_owned_sandbox("abc")

    assert rm.removed == [], "base 名不得触发任何释放"
    assert rm.get_sys_operation("team_sandbox_owned_abc_sess1") is not None

    builder.release_team_owned_sandbox("abc_sess1")

    assert sorted(rm.removed) == [
        "team_sandbox_owned_abc_sess1",
        "team_sandbox_ref_abc_sess1",
    ], "运行态名必须精确命中（自持 id + 借用别名）"


@pytest.mark.parametrize(
    ("released", "survivor"),
    [
        ("abc", "abc_sess1"),
        ("abc_sess1", "abc"),
        ("abc_sess1", "abc_sess1_sess2"),
    ],
    ids=["base-abc", "runtime-abc_sess1", "runtime-vs-deeper"],
)
def test_release_never_crosses_team_name_prefix(monkeypatch, released, survivor):
    """``abc`` 与 ``abc_sess1`` 共存时，释放任一方都不得波及另一方。

    旧实现按 ``team_sandbox_owned_<name>`` 前缀反推归属，**两个方向都会越界**：
      - 传 base 名 ``abc`` → 连带摘掉 ``abc_sess1`` 的沙箱；
      - 传运行态名 ``abc_sess1`` → 连带摘掉 ``abc_sess1_sess2`` 的沙箱。
    两者都不是"加个边界判断"能解决的：运行态名与 base 名互相是前缀的情形完全合法
    （``team_binding_store.TEAM_NAME_RE`` 不排除下划线，team 名由用户显式传入）。
    唯一无歧义的做法是记账时记全名、释放时精确查表。

    被误杀的后果不可自愈：``remove_sys_operation`` 连带 pop 掉
    ``_sandbox_key_owner_map`` 的那一行（``sys_operation_manager.py:67-68``），
    受害者下次 resolve 重新 add 出**新 uuid** → 容器重建、会话状态丢失。

    两条 team 必须用**不同**的 isolation key，否则它们本就该共用一个沙箱，
    根本走不到自持分支（那正是共享语义，不是缺陷）。
    """
    rm = _patch_resource_mgr(monkeypatch, {}, {})
    for team in (released, survivor):
        _patch_card_build(monkeypatch, _make_sandbox_card(custom_id=f"p_{team}"))
        spec = builder.create_sandbox_sysop_spec(
            "http://sandbox.invalid", "jiuwenbox", team_id=team, startup_mode="external",
        )
        assert spec.resolve() is not None, f"{team} 应自持成功"
        assert rm.get_sys_operation(f"team_sandbox_owned_{team}") is not None

    builder.release_team_owned_sandbox(released)

    assert sorted(rm.removed) == sorted(
        [f"team_sandbox_owned_{released}", f"team_sandbox_ref_{released}"]
    ), f"释放 {released} 摘掉了不属于它的资源：{rm.removed}"
    assert rm.get_sys_operation(f"team_sandbox_owned_{survivor}") is not None, (
        f"释放 {released} 误伤了 {survivor} 正在用的沙箱"
    )
    assert rm.get_sys_operation(f"team_sandbox_ref_{survivor}") is not None, (
        f"释放 {released} 误伤了 {survivor} 的借用别名"
    )


def test_release_is_idempotent_per_team(monkeypatch):
    """同一 team 重复释放只摘一次（记账项已 pop，第二次查不到即 no-op）。"""
    rm = _patch_resource_mgr(monkeypatch, {}, {})
    _patch_card_build(monkeypatch, _make_sandbox_card(custom_id="p_dup"))
    spec = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="dup_sess1", startup_mode="external",
    )
    assert spec.resolve() is not None

    builder.release_team_owned_sandbox("dup_sess1")
    builder.release_team_owned_sandbox("dup_sess1")

    assert sorted(rm.removed) == [
        "team_sandbox_owned_dup_sess1",
        "team_sandbox_ref_dup_sess1",
    ]


def test_teams_with_same_isolation_key_share_sandbox(monkeypatch):
    """同 isolation key 的两个 team 应共用一个沙箱，而非各建一个。

    这是"一 key 一 owner"语义的直接体现，也解释上一条用例为何必须给两个 team
    分配不同 key。
    """
    card = _make_sandbox_card()
    rm = _patch_resource_mgr(monkeypatch, {}, {})
    _patch_card_build(monkeypatch, card)

    first = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="team_a", startup_mode="external",
    )
    second = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="team_b", startup_mode="external",
    )

    # 共享的是底层沙箱实例（同一 _run_config / 容器连接），不是句柄 id——
    # 两个 team 的句柄名本就不同，比 id 没有意义。
    assert first.resolve()._run_config is second.resolve()._run_config, "同 key 应共用一个沙箱"
    assert rm.added == ["team_sandbox_owned_team_a"], "第二个 team 不应重复注册"
    builder.release_team_owned_sandbox("team_a")
    builder.release_team_owned_sandbox("team_b")


def test_resolve_returns_none_without_team_id(monkeypatch, caplog):
    """既无 owner 又无 team_id 时返回 None + WARNING，不抛异常。"""
    card = _make_sandbox_card()
    rm = _patch_resource_mgr(monkeypatch, {}, {})
    _patch_card_build(monkeypatch, card)
    spec = builder.TeamSharedSandboxSpec(
        id="team_sandbox_ref_",
        mode=OperationMode.SANDBOX,
        work_config=LocalWorkConfig(shell_allowlist=None),
        gateway_config=card.gateway_config,
    )

    with caplog.at_level("WARNING", logger="jiuwenswarm.server.runtime.agent_adapter.sysop_builder"):
        assert spec.resolve() is None

    assert rm.added == [], "无 team_id 时不应注册任何沙箱"
    assert "team sandbox 不可用" in caplog.text


# ---------------------------------------------------------------------------
# 回归锁：成员销毁不得影响主 agent 沙箱
# ---------------------------------------------------------------------------


def test_member_dispose_leaves_owner_registered(monkeypatch):
    """成员 harness 销毁后，主 agent 沙箱实例与其 isolation key 必须仍在。

    这是本次修复的核心不变量：``resolve()`` 返回 :class:`_BorrowedSandboxHandle`
    视图，其 ``.id`` 是**keyless 的借用别名 id** ⇒ ``native_harness.dispose()`` 只
    摘掉别名那一条，而别名声明 ``isolation_key_template = None``
    ⇒ ``sys_operation_manager.py:67-68`` 不会连带 pop 掉 owner 的 key 归属。

    传入的是 ``resolve().id``（实例 id，dispose 实际读到的那个），不是
    ``spec.id``——后者已被 ``deep_agent_spec.py:587`` 丢弃，传它会让本用例恒真。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    spec, rm = _build_spec(monkeypatch, owner_map=owner_map, sysops=sysops)
    template = next(iter(owner_map))

    resolved = spec.resolve()
    assert resolved is not None, "前置条件：成员先成功复用 owner"

    _simulate_native_harness_dispose(rm, resolved.id)

    assert rm.removed == ["team_sandbox_ref_unit_team"], (
        f"dispose 只该摘别名自己：{rm.removed}"
    )
    assert rm.get_sys_operation(_OWNER_ID) is not None, "主 agent沙箱被误注销"
    assert rm._resource_registry._owner_map.get(template) == _OWNER_ID, "isolation key 被误释放"


def test_id_aligned_handle_would_break_owner_on_dispose(monkeypatch):
    """反证：若 ``resolve()`` 直接返回 owner 实例，dispose 确实会误伤。

    这条用例锁住的是设计决策本身：任何人把 :meth:`TeamSharedSandboxSpec.resolve`
    改回 ``return owner``（去掉句柄包装）都会红。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    _rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    spec = builder.TeamSharedSandboxSpec(
        id="team_sandbox_ref_unit_team",
        mode=OperationMode.SANDBOX,
        work_config=LocalWorkConfig(shell_allowlist=None),
        gateway_config=card.gateway_config,
        team_id="unit_team",
    )

    # 回归模拟：resolve() 绕过 _BorrowedSandboxHandle，直接把 owner 交出去。
    owner = builder.registered_sandbox_owner_by_key(
        builder.sandbox_isolation_key_of(spec.build_card())
    )
    assert owner is not None and owner.id == _OWNER_ID, "前置条件：owner 已注册"

    _simulate_native_harness_dispose(_rm, owner.id)

    assert _rm.removed == [_OWNER_ID], "实例 id 为 owner 时 dispose 必然误伤主 agent"
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
        resolved = member_sysop.resolve()
        assert resolved is not None, f"{role} 未绑定到已注册沙箱"
        assert resolved.id == "team_sandbox_ref_unit_team", (
            f"{role} 的 resolve() 未返回借用句柄视图，dispose 会摘掉主 agent 沙箱"
        )
        assert resolved.mode == OperationMode.SANDBOX
        assert rm.get_sys_operation(resolved.id) is None, f"{role} 的句柄不得真的注册"

    assert rm.removed == []


# ---------------------------------------------------------------------------
# 序列化边界：嵌套 dump 会把子类压平成基类
# ---------------------------------------------------------------------------


def _nested_roundtrip(spec):
    """模拟 ``TeamAgentSpec.model_dump() -> TeamAgentSpec.model_validate()``。

    复现库侧四条边界共用的那一步：``from_spawn_payload`` /
    ``recover_from_session`` / ``team_runner`` / ``child_process`` 读序列化 spec。
    ``agents`` 是 ``dict[str, DeepAgentSpec]``，而 ``DeepAgentSpec.sys_operation``
    的**声明类型**是基类 ``SysOperationSpec``——pydantic 按声明类型序列化，
    ``model_dump`` 这一步 ``team_id`` 就已经不进 dump（顶层单独 dump
    ``DeepAgentSpec`` 同样丢），``model_validate`` 拿到的自然是基类形状。

    所以"子类丢失"发生在 dump 而非 validate：只测顶层 dump 的用例看不到
    team_id，但也别因此以为顶层往返是安全的——该字段一概不进 dump。
    """
    payload = spec.model_dump(mode="json")
    return TeamAgentSpec.model_validate(payload)


def test_nested_roundtrip_flattens_team_shared_sandbox_spec(monkeypatch):
    """**锁住前提**：往返确实会丢子类——本组后续用例都建立在它之上。

    若上游把 ``DeepAgentSpec.sys_operation`` 改成 ``SerializeAsAny[...]``（dump 与
    validate 两侧都要生效），本条会转红；届时
    :func:`install_team_sandbox_spec_rebind` 变成纯冗余（不会出错，但可以删）。
    """
    card = _make_sandbox_card()
    _patch_card_build(monkeypatch, card)
    spec = _make_team_spec()
    spec.agents["leader"] = spec.agents["leader"].model_copy(
        update={
            "sys_operation": builder.create_sandbox_sysop_spec(
                "http://sandbox.invalid", "jiuwenbox", team_id="unit_team",
                startup_mode="external",
            )
        }
    )

    before = spec.agents["leader"].sys_operation
    assert isinstance(before, builder.TeamSharedSandboxSpec)

    after = _nested_roundtrip(spec).agents["leader"].sys_operation

    assert type(after) is SysOperationSpec and not isinstance(
        after, builder.TeamSharedSandboxSpec
    ), "上游若已修（SerializeAsAny），本用例应转绿并可删除 rebind"
    assert type(after).resolve is SysOperationSpec.resolve, "resolve() 已退回库实现"
    assert not hasattr(after, "team_id"), "team_id 已被 pydantic 按 extra=ignore 丢弃"


def test_rebind_restores_subclass_and_team_id_after_roundtrip(monkeypatch):
    """rebind 必须把压平的基类换回子类，并从句柄 id 后缀反解出 team_id。"""
    card = _make_sandbox_card()
    _patch_card_build(monkeypatch, card)
    original = _make_team_spec()
    injected = builder.create_sandbox_sysop_spec(
        "http://sandbox.invalid", "jiuwenbox", team_id="unit_team", startup_mode="external",
    )
    original.agents["leader"] = original.agents["leader"].model_copy(
        update={"sys_operation": injected}
    )
    original.agents["teammate"] = original.agents["teammate"].model_copy(
        update={"sys_operation": injected}
    )

    restored = _nested_roundtrip(original)
    assert not isinstance(restored.agents["leader"].sys_operation, builder.TeamSharedSandboxSpec)

    rebound = builder.rebind_team_sandbox_specs(restored)

    assert rebound == 2, "两名成员都应被换回"
    for role in ("leader", "teammate"):
        sysop = restored.agents[role].sys_operation
        assert isinstance(sysop, builder.TeamSharedSandboxSpec), f"{role} 未换回子类"
        assert sysop.team_id == "unit_team", f"{role} 的 team_id 未从 id 后缀反解"
        assert sysop.id == "team_sandbox_ref_unit_team"
        assert sysop.mode == OperationMode.SANDBOX
        assert sysop.gateway_config == injected.gateway_config

    # 幂等：已全是子类时不得重复替换
    assert builder.rebind_team_sandbox_specs(restored) == 0


def test_rebind_after_roundtrip_restores_sandbox_binding(monkeypatch):
    """行为断言：rebind 后 resolve 返回借用句柄，别名可回查且 dispose 不摘 owner。

    这是 P0-2 的真正不变量。压平状态下基类 ``resolve()`` 是纯 get-or-create，会拿
    别名 id 去 add——而别名与 owner 同 isolation key，撞上"一个 key 一个 owner"被
    静默吞成 ``None``，成员回落 LOCAL（工具落宿主机）；key 不同源时才会真注册，
    那时又泄漏（不在记账表里）+ dispose 照摘。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    isolation_key = next(iter(owner_map))
    _patch_card_build(monkeypatch, card)
    spec = _make_team_spec()
    spec.agents["leader"] = spec.agents["leader"].model_copy(
        update={
            "sys_operation": builder.create_sandbox_sysop_spec(
                "http://sandbox.invalid", "jiuwenbox", team_id="unit_team",
                startup_mode="external",
            )
        }
    )

    # 反证（脏环境）：未 rebind 时基类 resolve 拿别名 id 去 add，撞同 key 冲突 →
    # 被吞成 None → 成员静默回落 LOCAL（这正是线上症状）。
    dirty = _patch_resource_mgr(monkeypatch, dict(owner_map), dict(sysops))
    assert _nested_roundtrip(spec).agents["leader"].sys_operation.resolve() is None, (
        "前置条件：压平状态下基类 resolve 撞 key 冲突后静默返回 None"
    )
    assert dirty.get_sys_operation("team_sandbox_ref_unit_team") is None
    assert dirty._resource_registry._owner_map[isolation_key] == _OWNER_ID

    # 干净环境：rebind 后借主 agent 的沙箱，别名登记但 keyless
    rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    restored = _nested_roundtrip(spec)
    assert builder.rebind_team_sandbox_specs(restored) == 1
    resolved = restored.agents["leader"].sys_operation.resolve()

    assert resolved.id == "team_sandbox_ref_unit_team"
    assert resolved.isolation_key_template is None, "别名不得声明 isolation key"
    by_id = rm.get_sys_operation(resolved.id)
    assert by_id is not None, "别名必须可按 id 回查（ContextEngine / SkillManager 依赖它）"
    assert by_id._run_config is rm.get_sys_operation(_OWNER_ID)._run_config
    assert rm._resource_registry._owner_map[isolation_key] == _OWNER_ID

    _simulate_native_harness_dispose(rm, resolved.id)
    assert rm.removed == ["team_sandbox_ref_unit_team"], (
        f"成员销毁只该摘自己那条别名：{rm.removed}"
    )
    assert rm.get_sys_operation(_OWNER_ID) is sysops[_OWNER_ID]
    assert rm._resource_registry._owner_map[isolation_key] == _OWNER_ID


def test_rebind_ignores_non_team_sys_operation(monkeypatch):
    """非本模块写入的 sys_operation（LOCAL / 无句柄前缀）一律不动。"""
    from openjiuwen.harness.schema.deep_agent_spec import SysOperationSpec as _Base

    spec = _make_team_spec()
    local = _Base(id="member.fs", mode=OperationMode.LOCAL)
    spec.agents["leader"] = spec.agents["leader"].model_copy(
        update={"sys_operation": local}
    )
    spec.agents["teammate"] = spec.agents["teammate"].model_copy(update={"sys_operation": None})

    assert builder.rebind_team_sandbox_specs(spec) == 0
    assert spec.agents["leader"].sys_operation is local
    assert spec.agents["teammate"].sys_operation is None


def test_rebind_skips_bare_prefix_id(monkeypatch):
    """空后缀（id 恰为前缀本身）反解不出 team_id，换回去只是个查不到 owner 的空壳。"""
    from openjiuwen.harness.schema.deep_agent_spec import SysOperationSpec as _Base

    spec = _make_team_spec()
    bare = _Base(
        id=builder._TEAM_SANDBOX_REF_ID_PREFIX,
        mode=OperationMode.SANDBOX,
        work_config=LocalWorkConfig(shell_allowlist=None),
    )
    spec.agents["leader"] = spec.agents["leader"].model_copy(
        update={"sys_operation": bare}
    )

    assert builder.rebind_team_sandbox_specs(spec) == 0
    assert type(spec.agents["leader"].sys_operation) is _Base


# ---------------------------------------------------------------------------
# P0-1：rebind 的**时机**——必须落在反序列化出口（configure 之前）
# ---------------------------------------------------------------------------


@pytest.fixture
def team_spec_rebind_installed():
    """在 ``TeamAgentSpec.model_validate`` 出口装上 rebind 包装。

    必须精确还原：本文件里 ``test_nested_roundtrip_flattens_team_shared_sandbox_spec``
    断言的是"未装补丁时确实压平"，泄漏的包装会让它转红。进程里若已存在包装
    （别的路径装的），则原样复用且不拆卸。
    """
    pre_existing = "model_validate" in TeamAgentSpec.__dict__
    builder.install_team_sandbox_spec_rebind()
    yield TeamAgentSpec
    if not pre_existing:
        _uninstall_validate_rebind()


def _uninstall_validate_rebind() -> None:
    """拆掉本模块对 ``TeamAgentSpec.model_validate`` 的包装（仅测试用）。"""
    if "model_validate" in TeamAgentSpec.__dict__:
        delattr(TeamAgentSpec, "model_validate")
    builder._TEAM_SPEC_REBIND_INSTALLED = False


def _member_spec_with_team_sandbox(monkeypatch, *, role: str = "teammate") -> TeamAgentSpec:
    """造一个已注入借用句柄 spec 的 team spec（沙箱 card 固定，避免真实网关）。"""
    card = _make_sandbox_card()
    _patch_card_build(monkeypatch, card)
    spec = _make_team_spec()
    spec.agents[role] = spec.agents[role].model_copy(
        update={
            "sys_operation": builder.create_sandbox_sysop_spec(
                "http://sandbox.invalid", "jiuwenbox", team_id="unit_team",
                startup_mode="external",
            )
        }
    )
    return spec


def test_install_team_sandbox_spec_rebind_is_idempotent(monkeypatch):
    """重复安装不得叠出多层包装（每层都会重跑一次 rebind）。"""
    _uninstall_validate_rebind()
    monkeypatch.setattr(builder, "_TEAM_SPEC_REBIND_INSTALLED", False)
    try:
        builder.install_team_sandbox_spec_rebind()
        once = TeamAgentSpec.model_validate
        builder.install_team_sandbox_spec_rebind()
        assert TeamAgentSpec.model_validate is once, "重复安装叠了多层包装"
        # 无 team 句柄的 spec 走一遍包装也不应被改动
        spec = _make_team_spec()
        assert TeamAgentSpec.model_validate(spec.model_dump(mode="json")).agents[
            "leader"
        ].sys_operation is None
    finally:
        _uninstall_validate_rebind()


def test_rebind_restored_at_validate_exit_before_configure(
    team_spec_rebind_installed, monkeypatch
):
    """**P0-1 的真正不变量**：validate 的返回值（= configure 的入参）必须已是子类。

    ``from_spawn_payload`` 把 validate 与 configure 背靠背同步（``team_agent.py:988``
    / ``:1006``），而 configure 内 ``TeamHarness`` 构造期就 ``resolve()`` 掉 spec、
    把解析后的实例写进 ``deep_config``（``native_harness.py:170`` /
    ``deep_agent_spec.py:587`` / ``factory.py:391``）。因此 rebind 写在调用点之后
    必然迟到——成员拿到的是基类 resolve() 现铸的 LOCAL 句柄。
    """
    spec = _member_spec_with_team_sandbox(monkeypatch)

    validated = TeamAgentSpec.model_validate(spec.model_dump(mode="json"))

    sysop = validated.agents["teammate"].sys_operation
    assert isinstance(sysop, builder.TeamSharedSandboxSpec), (
        "反序列化出口未换回子类 → configure 会拿到压平的基类"
    )
    assert sysop.team_id == "unit_team", "team_id 未从句柄 id 后缀反解"
    assert sysop.id == "team_sandbox_ref_unit_team"


def test_build_spec_from_boundary_shares_owner_and_registers_keyless_alias(
    team_spec_rebind_installed, monkeypatch
):
    """接缝之后的行为：按库的方式取 spec（``agent_configurator.py:473`` 的 model_copy）。

    这一步之后 harness 立刻 ``resolve()``，故必须出借用句柄：与 owner 共享容器配置，
    别名可按 id 回查（否则 ``deep_agent.py:879`` → ``react_agent.py:646`` 给
    ContextEngine 的就是 None），且别名 keyless（否则 dispose 会摘掉 owner 行）。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    spec = _member_spec_with_team_sandbox(monkeypatch)

    validated = TeamAgentSpec.model_validate(spec.model_dump(mode="json"))
    build_spec = validated.agents["teammate"].model_copy(update={"enable_task_loop": True})

    resolved = build_spec.sys_operation.resolve()

    assert type(resolved).__name__ == "_BorrowedSandboxHandle", (
        f"成员拿到的是 {type(resolved).__name__}，不是借用句柄"
    )
    assert resolved.id == "team_sandbox_ref_unit_team"
    assert rm.added == [], f"复用 owner 时不该新建 card 注册：{rm.added}"

    # 库按 id 回查的那条链必须命中，且拿到的正是共享 owner 容器配置的那一个
    by_id = rm.get_sys_operation(resolved.id)
    assert by_id is not None, "别名未登记：ContextEngine / SkillManager 会拿到 None"
    assert by_id._run_config is sysops[_OWNER_ID]._run_config
    assert resolved.isolation_key_template is None, "别名不得声明 isolation key"
    assert rm._resource_registry._owner_map[next(iter(owner_map))] == _OWNER_ID

    _simulate_native_harness_dispose(rm, resolved.id)
    assert rm.removed == ["team_sandbox_ref_unit_team"], (
        f"成员销毁只该摘自己那条别名：{rm.removed}"
    )
    assert rm.get_sys_operation(_OWNER_ID) is sysops[_OWNER_ID]
    assert rm._resource_registry._owner_map[next(iter(owner_map))] == _OWNER_ID, (
        "dispose 连带摘掉了 owner 行 → 下一个成员会自持新容器"
    )


def test_installed_wrapper_leaves_foreign_sys_operation_untouched(
    team_spec_rebind_installed,
):
    """非本模块写入的 sys_operation 经过包装必须原样通过（包装不得误伤普通 spec）。"""
    spec = _make_team_spec()
    spec.agents["leader"] = spec.agents["leader"].model_copy(
        update={"sys_operation": SysOperationSpec(id="member.fs", mode=OperationMode.LOCAL)}
    )

    validated = TeamAgentSpec.model_validate(spec.model_dump(mode="json"))

    sysop = validated.agents["leader"].sys_operation
    assert type(sysop) is SysOperationSpec and not isinstance(
        sysop, builder.TeamSharedSandboxSpec
    )
    assert sysop.id == "member.fs"
    assert sysop.mode == OperationMode.LOCAL
