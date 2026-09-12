# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""事件过滤模块单元测试 (level1)。"""

from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent

from agent_ssas.backend_client.openjiuwen.event_filter import EventFilter


def _make_security_ctx(event, extra=None, tool_name="", tool_call_id=""):
    """构造测试用 mock security_ctx。"""
    security_ctx = MagicMock()
    security_ctx.callback_ctx = MagicMock()
    security_ctx.callback_ctx.extra = extra or {}
    security_ctx.event = event
    security_ctx.tool_name = tool_name
    security_ctx.tool_call_id = tool_call_id
    return security_ctx


class TestEventFilter:
    """验证事件过滤模块的过滤逻辑。"""

    @staticmethod
    @pytest.mark.level1
    def test_base_event_passes_through():
        """生命周期事件全通过(返回原始 dict,不修改)。"""
        security_ctx = _make_security_ctx(AgentCallbackEvent.BEFORE_INVOKE)
        event_filter = EventFilter()

        original_dict = {
            "common": {"event_type": "invoke_start", "event_class": "lifecycle"},
            "payload": {},
            "metadata": {},
        }
        result = event_filter.filter_event(original_dict, security_ctx)

        assert result is not None
        assert result["common"]["event_type"] == "invoke_start"
        assert result["common"]["event_class"] == "lifecycle"

    @staticmethod
    @pytest.mark.level1
    def test_no_risk_when_skip_tool_false():
        """ctx.extra['_skip_tool'] 为 False 时,BEFORE_TOOL_CALL 作为生命周期事件通过。"""
        security_ctx = _make_security_ctx(
            AgentCallbackEvent.BEFORE_TOOL_CALL,
            extra={"_skip_tool": False},
        )
        event_filter = EventFilter()

        original_dict = {
            "common": {"event_type": "tool_input", "event_class": "lifecycle"},
            "payload": {},
            "metadata": {},
        }
        result = event_filter.filter_event(original_dict, security_ctx)

        assert result is not None
        assert result["common"]["event_type"] == "tool_input"
        assert result["common"]["event_class"] == "lifecycle"

    @staticmethod
    @pytest.mark.level1
    def test_risk_detected_when_skip_tool_true():
        """ctx.extra['_skip_tool'] 为 True 时,生成安全检测事件字段并修改 event_type 和 event_class。"""
        security_ctx = _make_security_ctx(
            AgentCallbackEvent.BEFORE_TOOL_CALL,
            extra={"_skip_tool": True},
            tool_name="bash",
            tool_call_id="call-001",
        )
        event_filter = EventFilter()

        original_dict = {
            "common": {"event_type": "tool_input", "event_class": "lifecycle"},
            "payload": {"tool_name": "bash"},
            "metadata": {},
        }
        result = event_filter.filter_event(original_dict, security_ctx)

        assert result is not None
        assert result["common"]["event_type"] == "permission_interrupt_tool"
        assert result["common"]["event_class"] == "security"
        assert result["payload"]["risk_source"] == "PermissionInterruptRail"
        assert result["payload"]["risk_type"] == "tool_permission_denied"
        assert result["payload"]["risk_level"] == "high"
        assert result["payload"]["decision"] == "reject"
        assert "evidence" in result["payload"]
        assert result["payload"]["evidence"]["tool_name"] == "bash"

    @staticmethod
    @pytest.mark.level1
    def test_no_observation_for_non_tool_events():
        """非 BEFORE_TOOL_CALL 事件不检查 _skip_tool,直接通过。"""
        security_ctx = _make_security_ctx(
            AgentCallbackEvent.BEFORE_MODEL_CALL,
            extra={"_skip_tool": True},
        )
        event_filter = EventFilter()

        original_dict = {
            "common": {"event_type": "llm_input", "event_class": "lifecycle"},
            "payload": {},
            "metadata": {},
        }
        result = event_filter.filter_event(original_dict, security_ctx)

        assert result is not None
        assert result["common"]["event_type"] == "llm_input"
        assert result["common"]["event_class"] == "lifecycle"

    @staticmethod
    @pytest.mark.level1
    def test_no_skip_tool_key_defaults_false():
        """ctx.extra 中没有 _skip_tool 键时,默认为 False。"""
        security_ctx = _make_security_ctx(
            AgentCallbackEvent.BEFORE_TOOL_CALL,
            extra={},
        )
        event_filter = EventFilter()

        original_dict = {
            "common": {"event_type": "tool_input", "event_class": "lifecycle"},
            "payload": {},
            "metadata": {},
        }
        result = event_filter.filter_event(original_dict, security_ctx)

        assert result is not None
        assert result["common"]["event_type"] == "tool_input"
        assert result["common"]["event_class"] == "lifecycle"
