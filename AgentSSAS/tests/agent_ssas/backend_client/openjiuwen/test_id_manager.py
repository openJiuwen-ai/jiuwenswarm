# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ID 管理模块单元测试 (level1)。"""

from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent

from agent_ssas.backend_client.openjiuwen.id_manager import IDManager


class TestIDManager:
    """验证 interaction_seq、llm_call_seq、tool_call_seq 整数自增序号、
    tool_call 事件的 llm_call_seq 取值、subsession_id。
    三个计数器均存储当前序号,初始值为 -1,BEFORE_xxx 时先自增再使用。
    """

    @staticmethod
    @pytest.mark.level1
    def test_interaction_seq_incremental_sequence():
        """BEFORE_INVOKE 生成整数自增序号,从 0 开始递增。"""
        ctx = MagicMock()
        ctx.extra = {}
        ctx.session = MagicMock()
        ctx.session.get_session_id.return_value = "test-session"
        idm = IDManager()

        # 第一次 BEFORE_INVOKE:-1 自增为 0
        seq0 = idm._ensure_interaction_seq(ctx, AgentCallbackEvent.BEFORE_INVOKE)
        assert seq0 == 0
        assert isinstance(seq0, int)

        # 第二次 BEFORE_INVOKE:0 自增为 1
        seq1 = idm._ensure_interaction_seq(ctx, AgentCallbackEvent.BEFORE_INVOKE)
        assert seq1 == 1
        assert isinstance(seq1, int)

    @staticmethod
    @pytest.mark.level1
    def test_interaction_seq_reused_within_invoke():
        """同一 invoke 内后续事件复用 interaction_seq(从 LRU 池读取当前值)。"""
        ctx = MagicMock()
        ctx.extra = {}
        ctx.session = MagicMock()
        ctx.session.get_session_id.return_value = "test-session"
        idm = IDManager()

        # BEFORE_INVOKE 生成(-1 自增为 0)
        idm._ensure_interaction_seq(ctx, AgentCallbackEvent.BEFORE_INVOKE)
        # 后续事件复用(非 BEFORE_INVOKE,从 LRU 池读取当前值)
        seq = idm._ensure_interaction_seq(ctx, AgentCallbackEvent.BEFORE_MODEL_CALL)
        assert seq == 0

    @staticmethod
    @pytest.mark.level1
    def test_llm_call_seq_generated_on_before_model_call():
        """BEFORE_MODEL_CALL 生成整数自增序号(初始 -1,先自增再使用),AFTER_MODEL_CALL 复用。"""
        ctx = MagicMock()
        ctx.extra = {}
        idm = IDManager()

        # BEFORE_MODEL_CALL 生成(-1 自增为 0)
        llm_seq_1 = idm._ensure_llm_call_seq(ctx, AgentCallbackEvent.BEFORE_MODEL_CALL)
        assert llm_seq_1 == 0
        assert isinstance(llm_seq_1, int)
        assert ctx.extra["llm_call_seq"] == 0

        # AFTER_MODEL_CALL 复用(读取当前值)
        llm_seq_2 = idm._ensure_llm_call_seq(ctx, AgentCallbackEvent.AFTER_MODEL_CALL)
        assert llm_seq_2 == llm_seq_1 == 0

        # 第二次 BEFORE_MODEL_CALL(0 自增为 1)
        llm_seq_3 = idm._ensure_llm_call_seq(ctx, AgentCallbackEvent.BEFORE_MODEL_CALL)
        assert llm_seq_3 == 1
        assert ctx.extra["llm_call_seq"] == 1

    @staticmethod
    @pytest.mark.level1
    def test_tool_call_seq_generated_on_before_tool_call():
        """BEFORE_TOOL_CALL 生成整数自增序号(初始 -1,先自增再使用),AFTER_TOOL_CALL 复用。"""
        ctx = MagicMock()
        ctx.extra = {}
        idm = IDManager()

        # 第一次 BEFORE_TOOL_CALL(-1 自增为 0)
        seq0 = idm._ensure_tool_call_seq(ctx, AgentCallbackEvent.BEFORE_TOOL_CALL)
        assert seq0 == 0
        assert isinstance(seq0, int)
        assert ctx.extra["tool_call_seq"] == 0

        # AFTER_TOOL_CALL 复用(读取当前值)
        seq_after = idm._ensure_tool_call_seq(ctx, AgentCallbackEvent.AFTER_TOOL_CALL)
        assert seq_after == seq0 == 0

        # 第二次 BEFORE_TOOL_CALL(0 自增为 1)
        seq1 = idm._ensure_tool_call_seq(ctx, AgentCallbackEvent.BEFORE_TOOL_CALL)
        assert seq1 == 1
        assert ctx.extra["tool_call_seq"] == 1

    @staticmethod
    @pytest.mark.level1
    def test_llm_call_seq_for_tool_call_reads_from_extra():
        """tool_call 事件的 llm_call_seq 从 ctx.extra 读取当前值。"""
        ctx = MagicMock()
        ctx.extra = {"llm_call_seq": 3}
        idm = IDManager()

        # tool_call 事件(BEFORE_TOOL_CALL)的 llm_call_seq 直接读取当前值
        llm_seq = idm._ensure_llm_call_seq(ctx, AgentCallbackEvent.BEFORE_TOOL_CALL)
        assert llm_seq == 3
        assert isinstance(llm_seq, int)

    @staticmethod
    @pytest.mark.level1
    def test_llm_call_seq_default_when_not_set():
        """未设置 llm_call_seq 时返回 -1。"""
        ctx = MagicMock()
        ctx.extra = {}
        idm = IDManager()

        llm_seq = idm._ensure_llm_call_seq(ctx, AgentCallbackEvent.AFTER_MODEL_CALL)
        assert llm_seq == -1

    @staticmethod
    @pytest.mark.level1
    def test_subsession_id_empty_for_non_subagent():
        """非子 Agent 场景 subsession_id 为空字符串。"""
        ctx = MagicMock()
        ctx.inputs = MagicMock()
        ctx.inputs.parent_session_id = None
        idm = IDManager()

        assert idm._resolve_subsession_id(ctx) == ""

    @staticmethod
    @pytest.mark.level1
    def test_subsession_id_filled_for_subagent():
        """子 Agent 场景 subsession_id 为 parent_session_id。"""
        ctx = MagicMock()
        ctx.inputs = MagicMock()
        ctx.inputs.parent_session_id = "parent-session-001"
        idm = IDManager()

        assert idm._resolve_subsession_id(ctx) == "parent-session-001"

    @staticmethod
    @pytest.mark.level1
    def test_resolve_session_id_empty_when_no_session():
        """无 session 时返回空字符串。"""
        ctx = MagicMock()
        ctx.session = None
        idm = IDManager()
        assert idm._resolve_session_id(ctx) == ""

    @staticmethod
    @pytest.mark.level1
    def test_resolve_agent_id_empty_when_no_agent():
        """无 agent 时返回空字符串。"""
        ctx = MagicMock()
        ctx.agent = None
        idm = IDManager()
        assert idm._resolve_agent_id(ctx) == ""

    @staticmethod
    @pytest.mark.level1
    def test_resolve_trace_id_empty_when_no_session():
        """无 session 时 trace_id 返回空字符串。"""
        ctx = MagicMock()
        ctx.session = None
        idm = IDManager()
        assert idm._resolve_trace_id(ctx) == ""

    @staticmethod
    @pytest.mark.level1
    def test_resolve_context_id_empty_when_no_context():
        """无 context 时返回空字符串。"""
        ctx = MagicMock()
        ctx.context = None
        idm = IDManager()
        assert idm._resolve_context_id(ctx) == ""

    @staticmethod
    @pytest.mark.level1
    def test_resolve_tool_name_for_non_tool_event():
        """非 TOOL 事件 tool_name 为空字符串。"""
        ctx = MagicMock()
        idm = IDManager()
        assert idm._resolve_tool_name(ctx, AgentCallbackEvent.BEFORE_MODEL_CALL) == ""

    @staticmethod
    @pytest.mark.level1
    def test_resolve_tool_call_id_empty_for_none():
        """tool_call 为 None 时返回空字符串。"""
        idm = IDManager()
        assert idm._resolve_tool_call_id(None) == ""

    @staticmethod
    @pytest.mark.level1
    def test_event_class_for_lifecycle():
        """生命周期事件 event_class 为 lifecycle。"""
        idm = IDManager()
        assert idm._event_class_for(AgentCallbackEvent.BEFORE_INVOKE, "invoke_start") == "lifecycle"
        assert idm._event_class_for(AgentCallbackEvent.BEFORE_MODEL_CALL, "llm_input") == "lifecycle"

    @staticmethod
    @pytest.mark.level1
    def test_event_class_for_security():
        """安全检测事件 event_class 为 security。"""
        idm = IDManager()
        assert idm._event_class_for(
            AgentCallbackEvent.BEFORE_TOOL_CALL, "permission_interrupt_tool"
        ) == "security"
