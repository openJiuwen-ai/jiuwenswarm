# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code 模式必须挂技能链两条 rail（凭证注入 + 活跃状态）。

回归 BUG20261124415174：code 会话首次创建时 `_build_agent_rails` 的固定
清单缺 `SkillCredentialInjectionRail` / `SkillActiveStateRail`，
`react.skill_envs` 没有注入通道，技能脚本读不到凭据 env，以参数缺失类
错误失败。reload 路径（基类 `_get_current_agent_rails`）会补建，所以
故障只在「新会话 + 未中途 configure」时稳定复现。

NOTE: 复用 lsp_rail / overflow 测试的 mock 注入模式 —— InMemoryTrajectoryRegistry
在已发布的 openjiuwen 包里尚不存在，需在 import 链解析前注入 mock。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import openjiuwen.agent_evolving.trajectory as _traj_mod
if not hasattr(_traj_mod, "InMemoryTrajectoryRegistry"):
    _traj_mod.InMemoryTrajectoryRegistry = MagicMock

import jiuwenswarm.server.runtime.agent_adapter.interface_code as _ic_mod

JiuwenSwarmCodeAdapter = _ic_mod.JiuwenSwarmCodeAdapter
_RAIL_BUILD_NAMES = getattr(_ic_mod, "_RAIL_BUILD_NAMES")
_FIXED_RAIL_NAMES = JiuwenSwarmCodeAdapter._FIXED_RAIL_NAMES


def _capture_rail_infos(config=None, config_base=None):
    """桩掉 _instantiate_rails，回放 _build_agent_rails 组装的 RailBuildInfo 列表。

    _build_agent_rails 末尾会调 _instantiate_rails 把列表实例化成 rail；桩成
    原样返回可避免真实 rail 构造的重依赖，只校验条目本身。
    """
    adapter = JiuwenSwarmCodeAdapter()
    captured: list = []
    real_instantiate = adapter._instantiate_rails

    def _passthrough_instantiate(rail_infos, config_base_arg, *args, **kwargs):
        captured.extend(rail_infos)
        return rail_infos

    object.__setattr__(adapter, "_instantiate_rails", _passthrough_instantiate)
    try:
        adapter._build_agent_rails(
            config=config or {},
            config_base=config_base or {},
            mode="code",
        )
    finally:
        object.__setattr__(adapter, "_instantiate_rails", real_instantiate)
    return adapter, captured


def test_build_agent_rails_mounts_skill_credential_and_active_state():
    """两条技能 rail 必须与 interface_deep 一样建在固定清单最前面。"""
    _, rail_infos = _capture_rail_infos()
    attr_names = [ri.attr_name for ri in rail_infos]

    assert attr_names, "_build_agent_rails should return a non-empty rail list"
    assert attr_names[0] == "_skill_credential_injection_rail", (
        "code mode must mount SkillCredentialInjectionRail first (parity with "
        f"interface_deep._build_agent_rails insert(0)); got {attr_names[:3]}"
    )
    assert attr_names[1] == "_skill_active_state_rail", (
        "code mode must mount SkillActiveStateRail second; "
        f"got {attr_names[:3]}"
    )


def test_skill_rails_use_inherited_builders():
    """装配条目必须复用父类 builder，凭证与活跃状态共享同一会话 id 契约。"""
    adapter, rail_infos = _capture_rail_infos()
    by_attr = {ri.attr_name: ri for ri in rail_infos}

    credential = by_attr["_skill_credential_injection_rail"]
    active_state = by_attr["_skill_active_state_rail"]
    assert credential.build_func == adapter._build_skill_credential_injection_rail
    assert active_state.build_func == adapter._build_skill_active_state_rail


def test_skill_rails_receive_runtime_config_not_config_base():
    """react.skill_envs 在 runtime config 里；传 config_base 会让注入目录恒空。"""
    skill_envs = {"huawei-cloud-skill-submit": {"DOMAIN_ID": "demo-domain"}}
    config = {"skill_envs": skill_envs}
    config_base = {"react": {"skill_envs": {}}}
    _, rail_infos = _capture_rail_infos(config=config, config_base=config_base)
    by_attr = {ri.attr_name: ri for ri in rail_infos}

    for attr in ("_skill_credential_injection_rail", "_skill_active_state_rail"):
        assert by_attr[attr].params == {"config": config}, (
            f"{attr} must be built from runtime config (skill_envs lives there)"
        )
    assert (
        by_attr["_skill_credential_injection_rail"].params["config"]["skill_envs"]
        == skill_envs
    )


def test_build_agent_rails_does_not_mount_skill_sleep_rail():
    """SkillSleepRail 属演进能力，code 的 _reconcile_evolution_rails 会主动停用。"""
    _, rail_infos = _capture_rail_infos()
    assert "_skill_sleep_rail" not in [ri.attr_name for ri in rail_infos]


def test_fixed_rail_names_contains_skill_rails():
    """两个名字必须在固定集里：config.yaml 配了也只跳过，不重复注册。"""
    assert "SkillCredentialInjectionRail" in _FIXED_RAIL_NAMES
    assert "SkillActiveStateRail" in _FIXED_RAIL_NAMES
    # 固定 rail 不走动态映射（配在 modes.code.rails 会被 unknown 分支跳过）
    assert "SkillCredentialInjectionRail" not in _RAIL_BUILD_NAMES
    assert "SkillActiveStateRail" not in _RAIL_BUILD_NAMES
