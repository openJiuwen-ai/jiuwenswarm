# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team acceleration rails: cost discipline and flash read/edit."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from openjiuwen.core.sys_operation import OperationMode
from openjiuwen.harness.prompts import SystemPromptBuilder

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import LocalSectionName
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import (
    ResponsePromptRail,
)
from jiuwenswarm.agents.harness.flash.tools.flash_edit_tool import FlashEditFileTool
from jiuwenswarm.agents.harness.flash.tools.flash_read_tool import FlashReadFileTool
from jiuwenswarm.agents.harness.team.accel.accel_prompt_rail import AccelPromptRail
from jiuwenswarm.agents.harness.team.accel.accel_sys_operation_rail import (
    AccelSysOperationRail,
)
from jiuwenswarm.agents.harness.team.accel.prompts import normalize_team_role
from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
    MemberInfo,
    RuntimeInfo,
    build_member_rails,
)
from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import (
    _CODE_RAIL_NAMES,
    _COMMON_RAIL_NAMES,
)
from jiuwenswarm.agents.swarm.providers.team_progressive import (
    TEAM_CODE_MODE_EXTRA_EAGER_TOOLS,
    TEAM_SHARED_EAGER_TOOLS,
)


class _AbilityManager:
    def __init__(self, cards: dict | None = None) -> None:
        self.cards = dict(cards or {})
        self.registered: dict = {}

    def get(self, name: str):
        return self.cards.get(name)

    def add_ability(self, card, tool) -> None:
        self.registered[card.name] = tool
        self.cards[card.name] = card


def _agent(manager: _AbilityManager, language: str = "cn"):
    builder = SystemPromptBuilder(language=language)
    return SimpleNamespace(
        system_prompt_builder=builder,
        card=SimpleNamespace(id="member-1"),
        ability_manager=manager,
        deep_config=None,
    ), builder


@pytest.mark.asyncio
async def test_accel_prompt_adds_member_cost_only():
    manager = _AbilityManager()
    agent, builder = _agent(manager)
    response = ResponsePromptRail()
    accel = AccelPromptRail(role="teammate")
    response.init(agent)
    accel.init(agent)

    await response.before_model_call(SimpleNamespace(inputs=None))
    await accel.before_model_call(SimpleNamespace())

    assert builder.has_section(LocalSectionName.THINKING_DISCIPLINE)
    assert builder.has_section(LocalSectionName.TEAM_COST_DISCIPLINE)
    rendered = builder.build()
    assert "团队成本意识（成员）" in rendered
    assert "成本会放大" in rendered
    assert "跟随任务卡 / leader 口径" in rendered
    assert "一次 write_file" in rendered
    assert "hunks" in rendered
    assert "send_message 只传" in rendered
    assert "写文件轮" in rendered
    assert "团队成本意识（leader）" not in rendered


@pytest.mark.asyncio
async def test_accel_prompt_leader_gets_leader_cost_copy():
    manager = _AbilityManager()
    agent, builder = _agent(manager)
    accel = AccelPromptRail(role="leader")
    accel.init(agent)
    await accel.before_model_call(SimpleNamespace())
    rendered = builder.build()
    assert "团队成本意识（leader）" in rendered
    assert "多成员成本近似相乘" in rendered
    assert "不要自己下场完成交付物" in rendered
    assert "能单人完成就不拆" not in rendered
    assert "看板完成即回执" in rendered
    assert "何时压成本 / 何时保质量" in rendered
    assert "团队成本意识（成员）" not in rendered


def test_normalize_team_role():
    assert normalize_team_role("leader") == "leader"
    assert normalize_team_role("LEAD") == "leader"
    assert normalize_team_role("teammate") == "teammate"
    assert normalize_team_role(None) == "teammate"
    assert normalize_team_role("worker") == "teammate"


def test_accel_sys_keeps_stock_names_and_mounts_flash_io():
    manager = _AbilityManager()
    agent, _builder = _agent(manager)
    rail = AccelSysOperationRail()
    rail.sys_operation = SimpleNamespace(mode=OperationMode.SANDBOX)
    rail.init(agent)

    by_name = manager.registered
    assert isinstance(by_name["read_file"], FlashReadFileTool)
    assert isinstance(by_name["edit_file"], FlashEditFileTool)
    assert by_name["read_file"].card.name == "read_file"
    assert by_name["edit_file"].card.name == "edit_file"
    expected = {"read_file", "write_file", "edit_file", "glob", "grep", "bash", "list_files"}
    if os.name == "nt":
        expected.add("powershell")
    assert expected <= set(by_name)
    assert "code" not in by_name


def test_accel_sys_inits_after_stock_and_keeps_flash_edit():
    """Dual mount: stock SysOperationRail first, Accel second → FlashEdit wins."""
    from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
    from openjiuwen.harness.tools.filesystem import EditFileTool

    manager = _AbilityManager()
    agent, _builder = _agent(manager)
    sysop = SimpleNamespace(mode=OperationMode.LOCAL)
    stock = SysOperationRail()
    stock.sys_operation = sysop
    stock.init(agent)
    assert isinstance(manager.registered["edit_file"], EditFileTool)
    assert not isinstance(manager.registered["edit_file"], FlashEditFileTool)

    assert AccelSysOperationRail.priority < SysOperationRail.priority
    accel = AccelSysOperationRail()
    accel.sys_operation = sysop
    accel.init(agent)
    assert isinstance(manager.registered["edit_file"], FlashEditFileTool)
    assert isinstance(manager.registered["read_file"], FlashReadFileTool)
    params = manager.registered["edit_file"].card.input_params or {}
    props = params.get("properties") if isinstance(params, dict) else None
    assert isinstance(props, dict) and "hunks" in props
    assert "hunks" in (manager.registered["edit_file"].card.description or "")


def test_worktree_guard_recognizes_team_accel_sys_without_stock_inject():
    from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec

    from jiuwenswarm.agents.harness.team.accel.worktree_sys_rail_patch import (
        TEAM_WORKTREE_BASH_DENY_PATTERNS,
        get_patched_has_worktree_shell_guard,
        get_patched_worktree_shell_guard,
    )

    apply_guard = get_patched_worktree_shell_guard()
    has_guard = get_patched_has_worktree_shell_guard()
    assert callable(apply_guard) and callable(has_guard)
    rails = [RailSpec(type=registry.TEAM_ACCEL_SYS)]
    out = apply_guard(rails, enabled=True)
    types = [getattr(rail, "type", None) for rail in out]
    assert registry.TEAM_ACCEL_SYS in types
    assert registry.SYS_OPERATION not in types
    accel = next(rail for rail in out if rail.type == registry.TEAM_ACCEL_SYS)
    deny = accel.params.get("bash_deny_patterns") or []
    assert list(TEAM_WORKTREE_BASH_DENY_PATTERNS) == deny
    assert has_guard(out) is True


def test_worktree_guard_patch_missing_upstream_returns_false(monkeypatch):
    """Upstream rename/missing symbols must not raise at import/apply time."""
    import builtins

    import jiuwenswarm.agents.harness.team.accel.worktree_sys_rail_patch as mod

    monkeypatch.setattr(mod, "_APPLIED", False)
    real_import = builtins.__import__

    def _boom(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name.startswith("openjiuwen.agent_teams"):
            raise ImportError("simulated missing upstream")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert mod.apply_team_accel_sys_rail_patch() is False
    assert mod._APPLIED is False  # pylint: disable=protected-access


def test_member_rails_include_accel_and_response_prompt():
    assert registry.RESPONSE_PROMPT in _COMMON_RAIL_NAMES
    assert registry.TEAM_ACCEL_PROMPT in _COMMON_RAIL_NAMES
    assert registry.TEAM_ACCEL_SYS in _COMMON_RAIL_NAMES
    assert registry.SYS_OPERATION not in _COMMON_RAIL_NAMES
    assert registry.RESPONSE_PROMPT in _CODE_RAIL_NAMES
    assert registry.TEAM_ACCEL_PROMPT in _CODE_RAIL_NAMES
    assert registry.TEAM_ACCEL_SYS in _CODE_RAIL_NAMES
    assert registry.SYS_OPERATION not in _CODE_RAIL_NAMES

    for role in ("leader", "teammate"):
        rails = build_member_rails(
            member_info=MemberInfo(role=role),
            runtime=RuntimeInfo(language="cn"),
        )
        names = {type(rail).__name__ for rail in rails}
        assert "ResponsePromptRail" in names
        assert "AccelPromptRail" in names
        assert "AccelSysOperationRail" in names
        assert "SysOperationRail" not in names
        accel = next(rail for rail in rails if isinstance(rail, AccelPromptRail))
        assert accel.role == role


def test_accel_rail_specs_build_live_rails():
    from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec

    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.registry import register_swarm_providers

    register_swarm_providers()
    leader_ctx = SwarmBuildContext(language="cn", channel="web", role="leader")
    mate_ctx = SwarmBuildContext(language="cn", channel="web", role="teammate")
    leader_prompt = RailSpec(type=registry.TEAM_ACCEL_PROMPT).build(
        language="cn", context=leader_ctx
    )
    mate_prompt = RailSpec(type=registry.TEAM_ACCEL_PROMPT).build(
        language="cn", context=mate_ctx
    )
    sys_rail = RailSpec(type=registry.TEAM_ACCEL_SYS).build(
        language="cn", context=mate_ctx
    )
    assert isinstance(leader_prompt, AccelPromptRail)
    assert isinstance(mate_prompt, AccelPromptRail)
    assert leader_prompt.role == "leader"
    assert mate_prompt.role == "teammate"
    assert isinstance(sys_rail, AccelSysOperationRail)


def test_progressive_eager_names_unchanged():
    shared = set(TEAM_SHARED_EAGER_TOOLS)
    assert {"send_message", "read_file", "edit_file", "list_files", "bash", "verify_task"} <= shared
    assert "powershell" not in shared
    assert "memory_get" not in shared
    assert {"switch_mode", "enter_plan_mode", "exit_plan_mode"} <= set(
        TEAM_CODE_MODE_EXTRA_EAGER_TOOLS
    )
