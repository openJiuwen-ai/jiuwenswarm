# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""权限/确认审批回答的解析。

选项文案同时充当协议取值，渲染端回传的可能是 ``value``（web / CLI）也可能是
``label``（TUI）。解析必须落到同一个动作上，且认不出来时只能拒绝、不能批准。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.permission_options import (
    ALLOW_ONCE,
    ALWAYS_ALLOW,
    REJECT,
    SESSION_ALLOW,
    normalize_option_value,
    resolve_permission_action,
)
from jiuwenswarm.common.schema.agent import AgentRequest

# 修改前那份字面量元组接受的全部取值。放宽识别范围只能增不能减，任何一个从这里
# 掉出去都意味着一个曾经可用的按钮变成了拒绝。
LEGACY_ACCEPTED = {
    "approve": ALLOW_ONCE,
    "本次允许": ALLOW_ONCE,
    "Approve": ALLOW_ONCE,
    "Proceed": ALLOW_ONCE,
    "批准": ALLOW_ONCE,
    "开始执行": ALLOW_ONCE,
    "session_allow": SESSION_ALLOW,
    "会话内记住": SESSION_ALLOW,
    "Session Allow": SESSION_ALLOW,
    "always_allow": ALWAYS_ALLOW,
    "allow_always": ALWAYS_ALLOW,
    "永久记住": ALWAYS_ALLOW,
    "总是允许": ALWAYS_ALLOW,
    "Always Allow": ALWAYS_ALLOW,
    "reject": REJECT,
    "拒绝": REJECT,
    "Reject": REJECT,
    "继续规划": REJECT,
    "其他意见": REJECT,
}


def _decode(monkeypatch, selected_option: str, source: str = "permission_interrupt") -> dict:
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    request = AgentRequest(
        request_id="req-answer",
        channel_id="web",
        session_id="web_session",
        params={
            "query": "",
            "request_id": "call_123",
            "answers": [{"selected_options": [selected_option], "custom_input": ""}],
            "source": source,
        },
    )
    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)
    return inputs["query"].user_inputs["call_123"]


class TestVocabulary:
    """词表本身：归一化与别名解析。"""

    @staticmethod
    @pytest.mark.parametrize(("value", "action"), sorted(LEGACY_ACCEPTED.items()))
    def test_every_previously_accepted_value_still_resolves(value, action):
        """放宽识别范围不得丢掉任何一个原本认识的取值。"""
        assert resolve_permission_action(value) == action

    @staticmethod
    @pytest.mark.parametrize(
        "value",
        ["Allow Once", "allow_once", "Allow-Once", "ALLOW_ONCE", "  allow_once  "],
    )
    def test_allow_once_spellings_agree(value):
        """大小写、分隔符与首尾空白都不改变取值的含义。"""
        assert resolve_permission_action(value) == ALLOW_ONCE

    @staticmethod
    @pytest.mark.parametrize(
        ("value", "action"),
        [
            ("Session allow", SESSION_ALLOW),
            ("session-allow", SESSION_ALLOW),
            ("Always allow", ALWAYS_ALLOW),
            ("ALWAYS ALLOW", ALWAYS_ALLOW),
            ("Keep planning", REJECT),
        ],
    )
    def test_case_only_and_separator_only_variants_resolve(value, action):
        """仅大小写或分隔符不同的写法曾经会落到未知选项。"""
        assert resolve_permission_action(value) == action

    @staticmethod
    @pytest.mark.parametrize("value", ["", "   ", None, "no-such-option", "skip", "确定"])
    def test_unrecognised_values_resolve_to_none(value):
        """认不出来返回 None，由调用方决定兜底；None 不等于"用户拒绝"。"""
        assert resolve_permission_action(value) is None

    @staticmethod
    def test_normalisation_does_not_touch_cjk():
        """中文文案没有词间分隔，归一化必须原样保留。"""
        assert normalize_option_value("  本次允许 ") == "本次允许"


class TestDecodedPayloads:
    """经过 build_inputs 的端到端解析。"""

    @staticmethod
    @pytest.mark.parametrize("value", ["approve", "allow_once", "本次允许", "Allow once"])
    def test_allow_once_approves_without_auto_confirm(monkeypatch, value):
        payload = _decode(monkeypatch, value)
        assert payload["approved"] is True
        assert payload["auto_confirm"] is False

    @staticmethod
    @pytest.mark.parametrize("value", ["session_allow", "会话内记住", "Session allow"])
    def test_session_allow_does_not_persist(monkeypatch, value):
        payload = _decode(monkeypatch, value)
        assert payload["approved"] is True
        assert payload["auto_confirm"] is True
        assert payload["persist_allow"] is False

    @staticmethod
    @pytest.mark.parametrize("value", ["always_allow", "allow_always", "永久记住", "Always allow"])
    def test_always_allow_persists(monkeypatch, value):
        payload = _decode(monkeypatch, value)
        assert payload["approved"] is True
        assert payload["auto_confirm"] is True
        assert payload["persist_allow"] is True

    @staticmethod
    @pytest.mark.parametrize("value", ["reject", "拒绝", "Reject"])
    def test_reject_is_refused(monkeypatch, value):
        payload = _decode(monkeypatch, value)
        assert payload["approved"] is False
        assert payload["feedback"] == "用户拒绝"

    @staticmethod
    @pytest.mark.parametrize("value", ["继续规划", "其他意见", "Keep planning"])
    def test_keep_planning_refuses_with_its_own_feedback(monkeypatch, value):
        """"继续规划"仍是拒绝，但写给模型的反馈不同。"""
        payload = _decode(monkeypatch, value)
        assert payload["approved"] is False
        assert payload["feedback"] == "用户希望继续规划"

    @staticmethod
    def test_plan_execute_still_matches_exactly(monkeypatch):
        """计划审批取值走独立分支，不受词表放宽影响。"""
        payload = _decode(monkeypatch, "plan_execute", source="confirm_interrupt")
        assert payload["approved"] is True
        assert payload["plan_execute"] is True

    @staticmethod
    def test_plan_skip_still_matches_exactly(monkeypatch):
        payload = _decode(monkeypatch, "plan_skip", source="confirm_interrupt")
        assert payload["approved"] is False
        assert payload["plan_skip"] is True

    @staticmethod
    def test_unknown_option_still_falls_closed(monkeypatch):
        """未知选项仍按拒绝处理：放宽识别范围不得吞掉这道兜底。"""
        payload = _decode(monkeypatch, "no-such-option")
        assert payload["approved"] is False
        assert "未知选项" in payload["feedback"]
