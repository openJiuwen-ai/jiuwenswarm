# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill_turbo ContextVar 7 通道 bind/reset 契约（S_09 N1-N3，架构铁律 9/10）。

防护意图：ContextVar 通道断裂是"契约接线与配置债"根因（11 个 PR）的
高发形态——bind 漏绑通道、reset 漏复位导致跨请求状态泄漏（PR 5015
parent session 绑定缺失事故、PR 5093 interactive_ask 未绑定事故）。
此前 7 通道仅 outer_todo 有测试，本文件补齐全部通道的绑定/复位/条件
语义/隔离性。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo import context_binding


def _read_all_channels() -> dict[str, Any]:
    """读取 7 通道当前值（各通道权威 getter）。"""
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
        get_effective_request_workspace_dir,
        get_interactive_ask,
        get_subagent_parent_session,
    )
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        get_current_request_metadata,
        get_current_skill_turbo_adapter,
        get_skill_turbo_outer_todo_active,
        get_skill_turbo_resume_answers,
    )

    return {
        "adapter": get_current_skill_turbo_adapter(),
        "metadata": get_current_request_metadata(),
        "workspace": get_effective_request_workspace_dir(),
        "interactive_ask": get_interactive_ask(),
        "subagent_parent": get_subagent_parent_session(),
        "resume_answers": get_skill_turbo_resume_answers(),
        "outer_todo": get_skill_turbo_outer_todo_active(),
    }


class TestBindAndResetAllChannels:
    def test_full_bind_sets_all_seven_channels(self):
        """全量 bind：7 通道各自可读到绑定值。"""
        ctx = SimpleNamespace()
        adapter = SimpleNamespace(name="adapter")
        metadata = {"effective_project_dir": "D:/ws/proj", "interactive_ask": True}
        parent = SimpleNamespace(name="session")
        resume_answers = [{"question": "Q", "selected_options": ["A"]}]

        context_binding.bind_skill_turbo_context(
            ctx,
            adapter=adapter,
            request_metadata=metadata,
            parent_session=parent,
            resume_answers=resume_answers,
            outer_todo_active=True,
        )
        try:
            values = _read_all_channels()
            assert values["adapter"] is adapter
            assert values["metadata"] is metadata
            assert values["workspace"] == "D:/ws/proj", "workspace 应从 metadata.effective_project_dir 派生"
            assert values["interactive_ask"] is True
            assert values["subagent_parent"] is parent
            assert values["resume_answers"] is resume_answers
            assert values["outer_todo"] is True
        finally:
            context_binding.reset_skill_turbo_context(ctx)

    def test_reset_restores_all_channels_to_defaults(self):
        """reset 后 7 通道回到默认（无跨用例泄漏）。"""
        ctx = SimpleNamespace()
        context_binding.bind_skill_turbo_context(
            ctx,
            adapter=SimpleNamespace(),
            request_metadata={"output_dir": "D:/ws/x", "interactive_ask": False},
            parent_session=SimpleNamespace(),
            resume_answers=[],
            outer_todo_active=False,
        )
        context_binding.reset_skill_turbo_context(ctx)

        values = _read_all_channels()
        assert values["adapter"] is None
        assert values["metadata"] is None
        assert values["workspace"] is None
        assert values["interactive_ask"] in (None, False)
        assert values["subagent_parent"] is None
        assert values["resume_answers"] is None
        assert values["outer_todo"] is None


class TestConditionalBindSemantics:
    def test_none_values_not_bound(self):
        """条件语义（S_09 N3）：值缺失不绑、不产 token——reset 后无副作用。"""
        ctx = SimpleNamespace()
        before = _read_all_channels()
        context_binding.bind_skill_turbo_context(ctx)  # 全部缺省
        context_binding.reset_skill_turbo_context(ctx)
        assert _read_all_channels() == before

    def test_workspace_requires_output_dir_in_metadata(self):
        """workspace 只从 metadata 派生：无 effective_project_dir 的 metadata 不绑 workspace。"""
        ctx = SimpleNamespace()
        context_binding.bind_skill_turbo_context(ctx, request_metadata={"foo": "bar"})
        try:
            from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
                get_effective_request_workspace_dir,
            )

            assert get_effective_request_workspace_dir() is None
        finally:
            context_binding.reset_skill_turbo_context(ctx)

    def test_outer_todo_requires_bool(self):
        """outer_todo 仅 bool 产生 token（非 bool 值不绑）。"""
        ctx = SimpleNamespace()
        context_binding.bind_skill_turbo_context(ctx, outer_todo_active="yes")
        try:
            from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                get_skill_turbo_outer_todo_active,
            )

            assert get_skill_turbo_outer_todo_active() is None
        finally:
            context_binding.reset_skill_turbo_context(ctx)

    def test_parent_session_unwraps_parent_attr(self):
        """parent_session 解包 _parent：绑定的必须是 DeepAgent 主会话本体。"""
        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            get_subagent_parent_session,
        )

        main_session = SimpleNamespace(name="main")
        wrapper = SimpleNamespace(name="wrapper", _parent=main_session)
        ctx = SimpleNamespace()
        context_binding.bind_skill_turbo_context(ctx, parent_session=wrapper)
        try:
            assert get_subagent_parent_session() is main_session
        finally:
            context_binding.reset_skill_turbo_context(ctx)


class TestResetRobustness:
    def test_reset_is_idempotent(self):
        """reset 幂等：二次调用不抛错（token 已 pop）。"""
        ctx = SimpleNamespace()
        context_binding.bind_skill_turbo_context(ctx, adapter=SimpleNamespace())
        context_binding.reset_skill_turbo_context(ctx)
        context_binding.reset_skill_turbo_context(ctx)  # 不应抛出

    def test_single_reset_failure_isolated(self, monkeypatch):
        """单通道 reset 失败不影响其余通道复位（异常路径不漏 reset）。"""
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            reset_current_skill_turbo_adapter,
        )

        ctx = SimpleNamespace()
        context_binding.bind_skill_turbo_context(
            ctx, adapter=SimpleNamespace(), resume_answers=[]
        )
        # 记录 token：被测的失败 resetter 会 pop 但不复位，测试自身须补复位防泄漏
        adapter_token = context_binding._bind_tokens(ctx)["adapter"]

        def _boom(_token):
            raise RuntimeError("adapter reset 失败")

        monkeypatch.setitem(context_binding._RESETTERS, "adapter", _boom)
        try:
            context_binding.reset_skill_turbo_context(ctx)  # 不应抛出
        finally:
            reset_current_skill_turbo_adapter(adapter_token)

        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            get_skill_turbo_resume_answers,
        )

        assert get_skill_turbo_resume_answers() is None, "adapter 复位失败不应阻断 resume_answers 复位"

    def test_reset_without_bind_is_noop(self):
        """未 bind 的 ctx reset 直接返回（after_invoke 等无绑定路径）。"""
        context_binding.reset_skill_turbo_context(SimpleNamespace())  # 不应抛出
