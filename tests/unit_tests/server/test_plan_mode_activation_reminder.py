# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Plan-mode activation reminder regression tests.

方向一：弱化激活提醒的流程强制语义，使只读命令（/review、/security-review）
在 plan 模式下可直接执行。该提醒不再强制 LLM 把 ``enter_plan_mode`` 作为
第一个动作，但仍保留只读约束说明。
"""

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.agent_ws_server import _inject_plan_mode_activation_reminder


def _make_request(params: dict) -> AgentRequest:
    return AgentRequest(request_id="test-1", session_id="sess-1", params=params)


class TestInjectActivationReminder:
    """弱化后的激活提醒：约束保留，流程强制移除。"""

    @staticmethod
    def test_reminder_states_plan_mode_active() -> None:
        request = _make_request({"query": "review PR 123"})
        _inject_plan_mode_activation_reminder(request)
        assert "Plan mode is active." in request.params["query"]

    @staticmethod
    def test_reminder_does_not_force_enter_plan_mode_as_first_action() -> None:
        """弱化后不再强制要求用户先执行 enter_plan_mode。"""
        request = _make_request({"query": "review PR 123"})
        _inject_plan_mode_activation_reminder(request)
        query = request.params["query"]
        assert "as your very first action" not in query
        assert "MUST call `enter_plan_mode`" not in query

    @staticmethod
    def test_reminder_allows_read_only_actions_directly() -> None:
        """弱化后显式说明只读操作可直接执行，对齐 Claude Code。"""
        request = _make_request({"query": ""})
        _inject_plan_mode_activation_reminder(request)
        query = request.params["query"]
        assert "read-only" in query.lower()
        # 仍提及 enter_plan_mode，但作为可选的规划入口而非强制首动作。
        assert "enter_plan_mode" in query

    @staticmethod
    def test_reminder_prepended_before_existing_query() -> None:
        original = "existing user query"
        request = _make_request({"query": original})
        _inject_plan_mode_activation_reminder(request)
        # 原始 query 必须保留在 reminder 之后。
        assert request.params["query"].endswith(original)
        assert request.params["query"].startswith("\n\n<system-reminder>")
