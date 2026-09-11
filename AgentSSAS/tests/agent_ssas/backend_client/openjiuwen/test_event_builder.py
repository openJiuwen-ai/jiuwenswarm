# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""构建模块单元测试 (level1)。"""

from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent

from agent_ssas.backend_client.openjiuwen.event_builder import EventBuilder
from agent_ssas.backend_client.openjiuwen.extended_context import ExtendedSecurityCheckContext
from agent_ssas.backend_client.openjiuwen.id_manager import IDManager


class TestEventBuilder:
    """验证三层结构事件消息构建。"""

    @staticmethod
    def _make_security_ctx(event, **kwargs):
        """构造测试用 ExtendedSecurityCheckContext。"""
        ctx = MagicMock()
        ctx.extra = {}
        ctx.inputs = MagicMock()
        ctx.exception = None
        defaults = dict(
            interaction_seq=0,
            session_id="session-001",
            agent_id="deep-agent-1",
            trace_id="trace-001",
            context_id="ctx-001",
            conversation_id="session-001",
            tool_call_id="",
            tool_name="",
            llm_call_seq=-1,
            tool_call_seq=-1,
            subsession_id="",
        )
        defaults.update(kwargs)
        return ExtendedSecurityCheckContext(
            callback_ctx=ctx, event=event, **defaults
        )

    @staticmethod
    @pytest.mark.level1
    def test_event_dict_three_layer_structure():
        """验证事件消息包含 common/payload/metadata 三层。"""
        idm = IDManager()
        builder = EventBuilder(idm)
        sec_ctx = _make_sec_ctx(AgentCallbackEvent.BEFORE_INVOKE)

        result = builder.build_event_dict(sec_ctx)

        assert "common" in result
        assert "payload" in result
        assert "metadata" in result
        assert result["metadata"] == {}

    @staticmethod
    @pytest.mark.level1
    def test_common_layer_has_14_fields():
        """验证 common 层包含 14 个通用字段。"""
        idm = IDManager()
        builder = EventBuilder(idm)
        sec_ctx = _make_sec_ctx(AgentCallbackEvent.BEFORE_INVOKE)

        result = builder.build_event_dict(sec_ctx)
        common = result["common"]

        expected_fields = {
            "source", "event_type", "event_class", "timestamp",
            "interaction_seq", "session_id", "conversation_id", "agent_id",
            "trace_id", "context_id", "llm_call_seq", "tool_call_seq",
            "subsession_id", "tool_call_id",
        }
        assert set(common.keys()) == expected_fields
        assert len(common) == 14

    @staticmethod
    @pytest.mark.level1
    def test_common_source_is_agent_ssas_security_rail():
        """验证 common.source == AgentSSASSecurityRail。"""
        idm = IDManager()
        builder = EventBuilder(idm)
        sec_ctx = _make_sec_ctx(AgentCallbackEvent.BEFORE_INVOKE)

        result = builder.build_event_dict(sec_ctx)
        assert result["common"]["source"] == "AgentSSASSecurityRail"

    @staticmethod
    @pytest.mark.level1
    def test_event_type_mapping():
        """验证 AgentCallbackEvent 到 event_type 的映射。"""
        idm = IDManager()
        builder = EventBuilder(idm)

        assert builder._event_type_for(AgentCallbackEvent.BEFORE_INVOKE) == "invoke_start"
        assert builder._event_type_for(AgentCallbackEvent.AFTER_INVOKE) == "invoke_end"
        assert builder._event_type_for(AgentCallbackEvent.BEFORE_MODEL_CALL) == "llm_input"
        assert builder._event_type_for(AgentCallbackEvent.AFTER_MODEL_CALL) == "llm_output"
        assert builder._event_type_for(AgentCallbackEvent.BEFORE_TOOL_CALL) == "tool_input"
        assert builder._event_type_for(AgentCallbackEvent.AFTER_TOOL_CALL) == "tool_output"

    @staticmethod
    @pytest.mark.level1
    def test_event_class_lifecycle():
        """验证生命周期事件 event_class 为 lifecycle。"""
        idm = IDManager()
        builder = EventBuilder(idm)
        sec_ctx = _make_sec_ctx(AgentCallbackEvent.BEFORE_INVOKE)

        result = builder.build_event_dict(sec_ctx)
        assert result["common"]["event_class"] == "lifecycle"

    @staticmethod
    @pytest.mark.level1
    def test_common_fields_values():
        """验证 common 层字段值正确传递。"""
        idm = IDManager()
        builder = EventBuilder(idm)
        sec_ctx = _make_sec_ctx(
            AgentCallbackEvent.BEFORE_TOOL_CALL,
            tool_call_id="call-001",
            tool_name="bash",
            llm_call_seq=0,
            tool_call_seq=0,
        )

        result = builder.build_event_dict(sec_ctx)
        common = result["common"]

        assert common["interaction_seq"] == 0
        assert common["session_id"] == "session-001"
        assert common["tool_call_id"] == "call-001"
        assert common["llm_call_seq"] == 0
        assert common["tool_call_seq"] == 0
        assert isinstance(common["interaction_seq"], int)
        assert isinstance(common["llm_call_seq"], int)
        assert isinstance(common["tool_call_seq"], int)

    @staticmethod
    @pytest.mark.level1
    def test_payload_tool_name_for_tool_event():
        """验证工具事件 payload 包含 tool_name。"""
        idm = IDManager()
        builder = EventBuilder(idm)
        sec_ctx = _make_sec_ctx(
            AgentCallbackEvent.BEFORE_TOOL_CALL,
            tool_name="bash",
        )

        result = builder.build_event_dict(sec_ctx)
        assert result["payload"]["tool_name"] == "bash"

    @staticmethod
    @pytest.mark.level1
    def test_payload_content_for_invoke_start():
        """验证 invoke_start 事件的 payload content 包含 query。"""
        idm = IDManager()
        builder = EventBuilder(idm)
        sec_ctx = _make_sec_ctx(AgentCallbackEvent.BEFORE_INVOKE)
        sec_ctx.callback_ctx.inputs.query = "hello"
        sec_ctx.callback_ctx.inputs.parent_session_id = ""
        sec_ctx.callback_ctx.inputs.run_kind = None

        result = builder.build_event_dict(sec_ctx)
        assert "content" in result["payload"]
        assert result["payload"]["content"]["query"] == "hello"


def _make_sec_ctx(event, **kwargs):
    """模块级辅助函数,构造测试用 ExtendedSecurityCheckContext。"""
    ctx = MagicMock()
    ctx.extra = {}
    ctx.inputs = MagicMock()
    ctx.exception = None
    defaults = dict(
        interaction_seq=0,
        session_id="session-001",
        agent_id="deep-agent-1",
        trace_id="trace-001",
        context_id="ctx-001",
        conversation_id="session-001",
        tool_call_id="",
        tool_name="",
        llm_call_seq=-1,
        tool_call_seq=-1,
        subsession_id="",
    )
    defaults.update(kwargs)
    return ExtendedSecurityCheckContext(
        callback_ctx=ctx, event=event, **defaults
    )
