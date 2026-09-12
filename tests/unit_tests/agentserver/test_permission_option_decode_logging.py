# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""认不出来的审批选项必须留下告警。

兜底成拒绝是对的，静默地兜底不是：用户点了同意、工具被拒，现场却只有一句用户
看不到的 feedback。这里断言"未识别"会以 WARNING 记下原始取值，而正常路径（包括
渲染端回传空串的取消）不得产生噪声。

不使用 caplog：本仓库的 logger 不向 root 传播，caplog 收不到记录。改为直接替换
发出日志的那个 logger。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest


class _RecordingLogger:
    """只记录调用的 logger 替身。"""

    def __init__(self) -> None:
        self.warnings: list[tuple] = []
        self.infos: list[tuple] = []

    def warning(self, msg, *args, **kwargs) -> None:
        self.warnings.append((msg, args))

    def info(self, msg, *args, **kwargs) -> None:
        self.infos.append((msg, args))

    def error(self, msg, *args, **kwargs) -> None:
        pass

    def debug(self, msg, *args, **kwargs) -> None:
        pass


def _decode(monkeypatch, selected_option, source="permission_interrupt", custom_input=""):
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    recorder = _RecordingLogger()
    monkeypatch.setattr(interface_module, "logger", recorder)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    selected_options = [] if selected_option is None else [selected_option]
    request = AgentRequest(
        request_id="req-answer",
        channel_id="web",
        session_id="web_session",
        params={
            "query": "",
            "request_id": "call_123",
            "answers": [
                {"selected_options": selected_options, "custom_input": custom_input}
            ],
            "source": source,
        },
    )
    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)
    return inputs["query"].user_inputs["call_123"], recorder


def _warned_values(recorder) -> list:
    """告警里携带的原始取值。"""
    return [args[3] for _msg, args in recorder.warnings]


class TestUnrecognisedOptionsWarn:

    @staticmethod
    def test_unknown_option_warns_and_still_refuses(monkeypatch):
        """未识别取值：先告警，再按拒绝兜底。"""
        payload, recorder = _decode(monkeypatch, "no-such-option")
        assert payload["approved"] is False
        assert len(recorder.warnings) == 1
        assert "no-such-option" in _warned_values(recorder)

    @staticmethod
    def test_warning_names_the_branch_and_the_outcome(monkeypatch):
        """告警要能定位到分支，否则日志里认不出是哪条路。"""
        _payload, recorder = _decode(monkeypatch, "no-such-option")
        msg, args = recorder.warnings[0]
        assert "unrecognised approval option" in msg
        assert args[0] == "PermissionRail"
        assert args[4] == "rejected_as_unknown_option"

    @staticmethod
    def test_unknown_option_with_custom_input_also_warns(monkeypatch):
        """带自由文本的拒绝同样是没解出取值，不能因为有 feedback 就不告警。"""
        payload, recorder = _decode(monkeypatch, "no-such-option", custom_input="别跑")
        assert payload["approved"] is False
        assert payload["feedback"] == "别跑"
        assert len(recorder.warnings) == 1
        assert recorder.warnings[0][1][4] == "rejected_with_custom_input"

    @staticmethod
    def test_evolution_branch_warns_on_unknown_action(monkeypatch):
        """技能演进分支同样会把未识别取值变成拒绝，同样要告警。"""
        payload, recorder = _decode(
            monkeypatch, "no-such-option", source="skill_evolution_approval"
        )
        assert payload["action"] == "reject"
        assert len(recorder.warnings) == 1
        assert recorder.warnings[0][1][0] == "SkillEvolutionApproval"


class TestQuietPaths:

    @staticmethod
    @pytest.mark.parametrize(
        "value", ["approve", "本次允许", "allow_once", "Session allow", "拒绝", "plan_execute"]
    )
    def test_recognised_values_do_not_warn(monkeypatch, value):
        """认识的取值不产生告警，否则告警本身没有信噪比。"""
        _payload, recorder = _decode(monkeypatch, value, source="confirm_interrupt")
        assert recorder.warnings == []

    @staticmethod
    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_cancel_does_not_warn(monkeypatch, value):
        """空取值是渲染端的取消（TUI 无拒绝项时按 Esc），属正常路径。"""
        payload, recorder = _decode(monkeypatch, value)
        assert payload["approved"] is False
        assert recorder.warnings == []
