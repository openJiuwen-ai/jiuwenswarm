# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ExtendedSecurityCheckContext 扩展验证 (level0)。"""

import pytest

from agent_ssas.backend_client.openjiuwen.extended_context import ExtendedSecurityCheckContext


class TestExtendedSecurityCheckContext:
    """验证 ExtendedSecurityCheckContext 继承 SecurityCheckContext 后新增的 ID 字段有缺省值,向后兼容。"""

    @staticmethod
    @pytest.mark.level0
    def test_default_values():
        ctx = ExtendedSecurityCheckContext(callback_ctx=None, event=None)
        # 原有字段(继承自 SecurityCheckContext,agent-core 未修改)
        assert ctx.user_input is None
        assert ctx.auto_confirm_config is None
        assert ctx.subject_id == ""
        # 新增 ID 字段(int 类型字段缺省值为 -1,str 类型字段缺省值为空字符串)
        # 缺省值为 -1,表示当前事件不具备此序号字段。首个有效值为 0,从 0 开始计数。
        assert ctx.interaction_seq == -1
        assert ctx.session_id == ""
        assert ctx.agent_id == ""
        assert ctx.trace_id == ""
        assert ctx.context_id == ""
        assert ctx.conversation_id == ""
        assert ctx.tool_call_id == ""
        assert ctx.tool_name == ""
        assert ctx.llm_call_seq == -1
        assert ctx.tool_call_seq == -1
        assert ctx.subsession_id == ""

    @staticmethod
    @pytest.mark.level0
    def test_custom_values():
        """验证可以自定义设置所有新增字段。"""
        ctx = ExtendedSecurityCheckContext(
            callback_ctx=None,
            event=None,
            interaction_seq=5,
            session_id="session-001",
            agent_id="agent-001",
            trace_id="trace-001",
            context_id="ctx-001",
            conversation_id="session-001",
            tool_call_id="call-001",
            tool_name="bash",
            llm_call_seq=2,
            tool_call_seq=3,
            subsession_id="parent-session",
        )
        assert ctx.interaction_seq == 5
        assert ctx.session_id == "session-001"
        assert ctx.agent_id == "agent-001"
        assert ctx.trace_id == "trace-001"
        assert ctx.context_id == "ctx-001"
        assert ctx.conversation_id == "session-001"
        assert ctx.tool_call_id == "call-001"
        assert ctx.tool_name == "bash"
        assert ctx.llm_call_seq == 2
        assert ctx.tool_call_seq == 3
        assert ctx.subsession_id == "parent-session"

    @staticmethod
    @pytest.mark.level0
    def test_int_fields_are_int_type():
        """验证 int 类型字段确实是 int 类型。"""
        ctx = ExtendedSecurityCheckContext(callback_ctx=None, event=None)
        assert isinstance(ctx.interaction_seq, int)
        assert isinstance(ctx.llm_call_seq, int)
        assert isinstance(ctx.tool_call_seq, int)
