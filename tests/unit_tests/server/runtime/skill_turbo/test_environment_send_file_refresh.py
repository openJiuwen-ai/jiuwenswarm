# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""send_file_to_user 按请求刷新与回收的单元测试（issue #5908）。

覆盖两个修复点：
- ``_refresh_send_file_tools`` 只让位本环境绑定，绝不移除父 agent 注册在
  进程级 resource_mgr 中的 ``send_file_to_user_<owner>`` 实例——否则技能
  管道结束后父 agent 再发文件会报 instance not found；
- ``release_registered_tools`` 在 invocation 结束后回收环境自注册的 uuid
  实例，且不触碰父实例与他人实例，重复调用幂等。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import jiuwenswarm.server.runtime.skill_turbo.environment as environment_module
from jiuwenswarm.server.runtime.skill_turbo.agent import SkillTurbo
from jiuwenswarm.server.runtime.skill_turbo.environment import SkillTurboEnvironment
from openjiuwen.core.runner.resources_manager.base import Error, Ok

# 模拟生产形态的两种 id：父 agent 限定 id / 技能环境自注册的 uuid id
_PARENT_TOOL_ID = "send_file_to_user_jiuwenswarm_s_officeclaw_session01"
_TURBO_TOOL_ID = "3fa85f64626f4c7a9d2e1b8f0a4c5d6e"
_UNRELATED_TOOL_ID = "bash_jiuwenswarm_s_officeclaw_session01"


class _FakeResourceMgr:
    """进程级 resource_mgr 的最小替身，记录所有 remove 调用便于断言。"""

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}
        self.removed: list[str] = []
        self.fail_add = False  # 置 True 模拟 add_tool 返回 Err（如重复 id）

    def get_tool(self, tool_id: str) -> Any:
        return self._tools.get(tool_id)

    def add_tool(self, tool: Any) -> Any:
        if self.fail_add:
            return Error(RuntimeError("duplicate tool id"))
        if tool.card.id in self._tools:
            # 对齐真实 resource_mgr 严格语义：id 已注册（refresh=False）→ Err
            return Error(RuntimeError(f"tool already registered: {tool.card.id}"))
        self._tools[tool.card.id] = tool
        return Ok(tool.card)

    def remove_tool(self, tool_id: str) -> Any:
        self.removed.append(tool_id)
        return self._tools.pop(tool_id, None)


def _make_card(tool_id: str, name: str = "send_file_to_user") -> SimpleNamespace:
    return SimpleNamespace(name=name, description="send file", id=tool_id, input_params=None)


def _make_loader(tool_id: str):
    """返回一个 loader，产出带指定 id 的技能自有 send_file 工具实例。"""
    fresh_tool = SimpleNamespace(card=_make_card(tool_id))
    return lambda ctx: [fresh_tool]


@pytest.fixture()
def fake_mgr(monkeypatch):
    """把 environment 模块引用的 Runner 替换为持有假 resource_mgr 的桩。"""
    mgr = _FakeResourceMgr()
    monkeypatch.setattr(environment_module, "Runner", SimpleNamespace(resource_mgr=mgr))
    return mgr


def _make_env_with_parent(mgr: _FakeResourceMgr) -> SkillTurboEnvironment:
    """构造已继承父 agent send_file_to_user 卡片的环境（同 build_skill_turbo_config）。"""
    env = SkillTurboEnvironment({"tools": {}, "model": None})
    parent_card = _make_card(_PARENT_TOOL_ID)
    env.register_tool(parent_card)
    mgr.add_tool(SimpleNamespace(card=parent_card))  # 父实例已在全局注册表
    return env


class TestRefreshSendFileTools:
    """_refresh_send_file_tools：只让位本环境绑定，不动父实例。"""

    def test_refresh_does_not_remove_parent_instance(self, fake_mgr):
        env = _make_env_with_parent(fake_mgr)

        env._refresh_send_file_tools(SimpleNamespace(), _make_loader(_TURBO_TOOL_ID))

        # 核心回归（issue #5908）：父实例必须仍注册在全局注册表
        assert fake_mgr.get_tool(_PARENT_TOOL_ID) is not None
        assert _PARENT_TOOL_ID not in fake_mgr.removed
        # 本环境绑定已让位给技能自有实例
        assert env._tools["send_file_to_user"].id == _TURBO_TOOL_ID
        assert fake_mgr.get_tool(_TURBO_TOOL_ID) is not None
        assert env._self_registered_tool_ids == {_TURBO_TOOL_ID}

    def test_refresh_twice_only_removes_self_registered_stale_instance(self, fake_mgr):
        env = _make_env_with_parent(fake_mgr)

        env._refresh_send_file_tools(SimpleNamespace(), _make_loader(_TURBO_TOOL_ID))
        env._refresh_send_file_tools(SimpleNamespace(), _make_loader(_TURBO_TOOL_ID))

        # 允许移除的只有环境自注册的旧实例，父实例始终不动
        assert fake_mgr.removed == [_TURBO_TOOL_ID]
        assert fake_mgr.get_tool(_PARENT_TOOL_ID) is not None
        assert fake_mgr.get_tool(_TURBO_TOOL_ID) is not None
        assert env._self_registered_tool_ids == {_TURBO_TOOL_ID}

    def test_refresh_skips_bookkeeping_and_binding_when_add_fails(self, fake_mgr):
        """add_tool 返回 Err（Result 风格失败）时不记账、不绑定环境。"""
        env = _make_env_with_parent(fake_mgr)
        fake_mgr.fail_add = True

        env._refresh_send_file_tools(SimpleNamespace(), _make_loader(_TURBO_TOOL_ID))

        assert env._self_registered_tool_ids == set()  # 未注册成功不得记账
        assert "send_file_to_user" not in env._tools  # 不得绑定，防 executor 解析到 None
        assert fake_mgr.get_tool(_PARENT_TOOL_ID) is not None

    def test_refresh_never_removes_globally_registered_foreign_id(self, fake_mgr):
        """已全局注册但非本环境自注册的 id（模拟父限定 id）不得被 remove。

        移除旧实例以 `_self_registered_tool_ids` 白名单为准；否则一旦 loader
        将来传入父限定 id，此处会 1:1 重演 #5908 误删父实例。"""
        env = _make_env_with_parent(fake_mgr)

        env._refresh_send_file_tools(SimpleNamespace(), _make_loader(_PARENT_TOOL_ID))

        # 父实例绝不能被 remove（防线核心断言）
        assert _PARENT_TOOL_ID not in fake_mgr.removed
        assert fake_mgr.get_tool(_PARENT_TOOL_ID) is not None
        # add 因重复 id 返回 Err → 不记账不绑定（add 失败防护接住）
        assert _PARENT_TOOL_ID not in env._self_registered_tool_ids
        assert "send_file_to_user" not in env._tools


class TestReleaseRegisteredTools:
    """release_registered_tools：只回收自注册实例，幂等。"""

    def test_release_removes_only_self_registered_ids(self, fake_mgr):
        env = _make_env_with_parent(fake_mgr)
        env._refresh_send_file_tools(SimpleNamespace(), _make_loader(_TURBO_TOOL_ID))
        fake_mgr.add_tool(SimpleNamespace(card=_make_card(_UNRELATED_TOOL_ID, name="bash")))

        env.release_registered_tools()

        assert fake_mgr.get_tool(_TURBO_TOOL_ID) is None  # 自注册实例已回收
        assert fake_mgr.get_tool(_PARENT_TOOL_ID) is not None  # 父实例不受影响
        assert fake_mgr.get_tool(_UNRELATED_TOOL_ID) is not None  # 他人实例不受影响
        assert env._self_registered_tool_ids == set()

    def test_release_is_idempotent(self, fake_mgr):
        env = _make_env_with_parent(fake_mgr)
        env._refresh_send_file_tools(SimpleNamespace(), _make_loader(_TURBO_TOOL_ID))

        env.release_registered_tools()
        removed_after_first = len(fake_mgr.removed)
        env.release_registered_tools()  # 第二次调用应无副作用、无异常

        assert len(fake_mgr.removed) == removed_after_first
        assert env._self_registered_tool_ids == set()

    def test_release_noop_when_nothing_registered(self, fake_mgr):
        env = SkillTurboEnvironment({"tools": {}, "model": None})

        env.release_registered_tools()

        assert fake_mgr.removed == []


def test_skill_turbo_delegates_release_to_environment(fake_mgr):
    """SkillTurbo.release_registered_tools 透传给环境，回收自注册实例。"""
    env = _make_env_with_parent(fake_mgr)
    env._refresh_send_file_tools(SimpleNamespace(), _make_loader(_TURBO_TOOL_ID))
    turbo = SkillTurbo.__new__(SkillTurbo)  # 跳过重组件的 planner/executor 初始化
    turbo._env = env

    turbo.release_registered_tools()

    assert fake_mgr.get_tool(_TURBO_TOOL_ID) is None
    assert fake_mgr.get_tool(_PARENT_TOOL_ID) is not None
