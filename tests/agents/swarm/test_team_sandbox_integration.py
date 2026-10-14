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
# create_sandbox_sysop_spec id 策略：借用句柄永不注册，resolve() 时才查 owner
# ---------------------------------------------------------------------------

_OWNER_ID = "f0d3af13c98a45d084f483cf1fb4c87b"


class _FakeRegistry:
    def __init__(self, owner_map, sysops):
        self._owner_map = owner_map
        self._sysops = sysops

    def sys_operation(self):
        return SimpleNamespace(_sandbox_key_owner_map=self._owner_map)


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
        self.removed: list[str] = []
        self.added: list[str] = []
        #: 测试可置为 False 让 add 一律失败（验证 Result 检查路径）
        self.add_succeeds = True

    def get_sys_operation(self, sysop_id):
        return self._sysops.get(sysop_id)

    def add_sys_operation(self, card):
        if not self.add_succeeds:
            return _FakeErr("simulated add failure")
        # 存**真实** SysOperation 实例（而非SimpleNamespace 替身）：借用句柄
        # ``_BorrowedSandboxHandle`` 会读 source._run_config / source._instances 并
        # 委托 fs()/shell()，替身没有这些成员。
        instance = SysOperation(card)
        self._sysops[card.id] = instance
        self.added.append(card.id)
        registry = self._resource_registry
        template = instance.isolation_key_template
        if template:
            registry._owner_map[template] = card.id
        return _FakeOk(card.id)

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
    会让本用例恒真（句柄未注册，判定必然为假），是假阴性的直接来源。
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

    LOCAL owner 不可复用，但**不应**阻断自持兑底——否则会退化成 LOCAL。
    """
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
    resolved = spec.resolve()
    assert resolved is not None, "LOCAL owner 不该阻断自持兑底"
    assert resolved.id == "team_sandbox_ref_unit_team", (
        "resolve() 必须返回借用句柄视图，其 .id 永不注册"
    )
    assert resolved.mode == OperationMode.SANDBOX
    assert _rm.get_sys_operation("team_sandbox_owned_unit_team") is not None, (
        "自持沙箱应已注册，且其注册 id 与句柄 id 解耦"
    )


# ---------------------------------------------------------------------------
# resolve()：真正把成员绑到已注册沙箱的那一步
# ---------------------------------------------------------------------------


def test_resolve_returns_registered_sandbox_owner(monkeypatch):
    """有 owner 时 ``resolve()`` 必须**借用**那个 SANDBOX 实例——而非 None。

    这是原 bug 的行为性断言：库 ``resolve()`` 丢 ``Result`` 后静默回落 LOCAL，
    成员工具落宿主机。此处直接断言拿到的是 mode=SANDBOX 的 owner 实例。

    关键不变式：``resolve()`` 返回的是 :class:`_BorrowedSandboxHandle` 视图，
    它的 ``.id`` 是**未注册的句柄名**（dispose 唯一读到的 id），而底层绑定的
    仍是 owner 实例。返回 owner 本身会让成员 dispose 摘掉主 agent 的沙箱。
    """
    card = _make_sandbox_card()
    owner_map, sysops = _owner_map_with(card)
    spec, rm = _build_spec(monkeypatch, owner_map=owner_map, sysops=sysops)

    resolved = spec.resolve()

    assert resolved is not None, "resolve() 返回 None，成员会回落 LOCAL"
    assert resolved.mode == OperationMode.SANDBOX
    assert resolved.id == "team_sandbox_ref_unit_team", (
        "返回的实例 .id 必须是指向未注册句柄的视图，而非 owner id"
    )
    assert rm.get_sys_operation(resolved.id) is None, "句柄不得真的注册进 registry"
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
    """**走兑底后**，成员 dispose 仍不得摘掉自持沙箱。

    自持沙箱全队共用一个注册 id：一个成员退出会打断其余所有人。本条与
    ``test_member_dispose_leaves_owner_registered`` 一起锁住两条分支各自的不变式。
    """
    spec, rm = _build_spec(monkeypatch, owner_map={}, sysops={})
    resolved = spec.resolve()
    assert resolved is not None, "前置条件：先成功自持"

    _simulate_native_harness_dispose(rm, resolved.id)

    assert rm.removed == [], f"成员销毁摘掉了自持沙箱：{rm.removed}"
    assert rm.get_sys_operation("team_sandbox_owned_unit_team") is not None


def test_sibling_member_survives_peer_dispose(monkeypatch):
    """成员 A dispose 后，成员 B 的绑定必须仍然可用（自持沙箱是全队共用）。"""
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


def test_release_removes_team_owned_sandbox(monkeypatch):
    """release 后 registry 与记账表都清空。"""
    spec, rm = _build_spec(monkeypatch, owner_map={}, sysops={})
    assert spec.resolve() is not None

    builder.release_team_owned_sandbox("unit_team")

    assert rm.removed == ["team_sandbox_owned_unit_team"]
    assert rm.get_sys_operation("team_sandbox_owned_unit_team") is None


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

    assert rm.removed == ["team_sandbox_owned_abc_sess1"], "运行态名必须精确命中"


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

    assert rm.removed == [f"team_sandbox_owned_{released}"], (
        f"释放 {released} 摘掉了不属于它的资源：{rm.removed}"
    )
    assert rm.get_sys_operation(f"team_sandbox_owned_{survivor}") is not None, (
        f"释放 {released} 误伤了 {survivor} 正在用的沙箱"
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

    assert rm.removed == ["team_sandbox_owned_dup_sess1"]


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
    视图，其 ``.id`` 是未注册的借用句柄 ⇒ ``native_harness.dispose()`` 的
    ``get_sys_operation(id) is not None`` 判定恒假 ⇒ 不会走到 remove。

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

    assert rm.removed == [], f"成员销毁摘掉了非己方资源：{rm.removed}"
    assert rm.get_sys_operation(_OWNER_ID) is not None, "主 agent沙箱被误注销"
    assert template in rm._resource_registry._owner_map, "isolation key 被误释放"


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
    ``recover_from_session`` / ``team_runner`` 读持久化 spec。``agents`` 是
    ``dict[str, DeepAgentSpec]``，而 ``DeepAgentSpec.sys_operation`` 的**声明
    类型**是基类 ``SysOperationSpec``——pydantic 按声明类型反序列化，子类即被压平。

    注意**顶层** dump 不压平（保留 ``team_id``），所以只测顶层往返的用例永远发现
    不了这个问题。
    """
    payload = spec.model_dump(mode="json")
    return TeamAgentSpec.model_validate(payload)


def test_nested_roundtrip_flattens_team_shared_sandbox_spec(monkeypatch):
    """**锁住前提**：嵌套往返确实会压平——本组后续用例都建立在它之上。

    若上游把 ``DeepAgentSpec.sys_operation`` 改成 ``SerializeAsAny[...]``，本条会转
    红；届时 :func:`rebind_team_sandbox_specs` 变成纯冗余（不会出错，但可以删）。
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
    """行为断言：rebind 后 resolve 仍返回借用句柄，dispose 不摘他人资源。

    这是 P0-2 的真正不变量。压平状态下基类 ``resolve()`` 是纯 get-or-create，会把
    ``team_sandbox_ref_<team>`` 真正注册进 registry——既泄漏（不在自持记账表里，
    ``release_team_owned_sandbox`` 收不到），又会被成员 dispose 照摘。
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

    # 反证（脏环境）：未 rebind 时基类 resolve 会把借用句柄真正注册，并把 owner map
    # 那一行顶成句柄 id——此后主 agent 的 isolation-key 复用也会命中成员的私有注册。
    dirty = _patch_resource_mgr(monkeypatch, dict(owner_map), dict(sysops))
    _nested_roundtrip(spec).agents["leader"].sys_operation.resolve()
    assert dirty.get_sys_operation("team_sandbox_ref_unit_team") is not None, (
        "前置条件：压平状态下基类 resolve 会真注册借用句柄"
    )
    assert dirty._resource_registry._owner_map[isolation_key] == "team_sandbox_ref_unit_team"

    # 干净环境：rebind 后借主 agent 的沙箱，句柄不落注册表
    rm = _patch_resource_mgr(monkeypatch, owner_map, sysops)
    restored = _nested_roundtrip(spec)
    assert builder.rebind_team_sandbox_specs(restored) == 1
    resolved = restored.agents["leader"].sys_operation.resolve()

    assert resolved.id == "team_sandbox_ref_unit_team"
    assert rm.get_sys_operation(resolved.id) is None, "句柄不得落注册表"
    assert resolved._run_config is rm.get_sys_operation(_OWNER_ID)._run_config

    _simulate_native_harness_dispose(rm, resolved.id)
    assert rm.removed == [], f"成员销毁摘掉了非己方资源：{rm.removed}"
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
