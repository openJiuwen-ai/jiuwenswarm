# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""数据预处理模块单元测试。

验证 parse tool_input 事件、parse permission_interrupt_tool 安全检测事件、
解析后 UnifiedEvent 字段正确性、自动生成 session_start 节点。
"""

from __future__ import annotations

import pytest

from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.data_preprocessor import DataPreprocessor


class TestDataPreprocessor:
    """数据预处理模块。"""

    @staticmethod
    def _make_tool_input_raw_event() -> dict:
        """构造 tool_input 生命周期事件 raw_event。"""
        return {
            "common": {
                "source": "AgentSSASSecurityRail",
                "event_type": "tool_input",
                "event_class": "lifecycle",
                "timestamp": 1715000000.0,
                "interaction_seq": 0,
                "session_id": "test-session",
                "conversation_id": "test-session",
                "agent_id": "test-agent",
                "trace_id": "test-trace",
                "context_id": "test-ctx",
                "llm_call_seq": -1,
                "tool_call_seq": 0,
                "subsession_id": "",
                "tool_call_id": "call-001",
            },
            "payload": {
                "tool_name": "bash",
                "tool_call_id": "call-001",
                "content": {"tool_args": {"command": "ls"}},
            },
            "metadata": {},
        }

    @staticmethod
    def _make_permission_interrupt_raw_event() -> dict:
        """构造 permission_interrupt_tool 安全检测事件 raw_event。"""
        return {
            "common": {
                "source": "AgentSSASSecurityRail",
                "event_type": "permission_interrupt_tool",
                "event_class": "security",
                "timestamp": 1715000000.0,
                "interaction_seq": 0,
                "session_id": "test-session",
                "conversation_id": "test-session",
                "agent_id": "test-agent",
                "trace_id": "test-trace",
                "context_id": "test-ctx",
                "llm_call_seq": -1,
                "tool_call_seq": 0,
                "subsession_id": "",
                "tool_call_id": "call-001",
            },
            "payload": {
                "tool_name": "bash",
                "tool_call_id": "call-001",
                "risk_source": "PermissionInterruptRail",
                "risk_type": "tool_permission_denied",
                "risk_level": "high",
                "decision": "reject",
                "evidence": {"reason": "denied"},
            },
            "metadata": {},
        }

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_parse_tool_input_event() -> None:
        """验证 tool_input 事件解析为 UnifiedEvent,字段正确。"""
        preprocessor = DataPreprocessor(AgentSSASConfig())
        raw_event = TestDataPreprocessor._make_tool_input_raw_event()
        events = await preprocessor.parse(raw_event)
        # 最后一个是基础事件,前面是派生事件(session_start, user_input)
        unified = events[-1]
        node = unified.event_node
        assert node.event_type == "tool_input"
        assert node.event_class == "lifecycle"
        assert node.source == "AgentSSASSecurityRail"
        assert node.session_id == "test-session"
        assert node.interaction_seq == 0
        assert node.agent_id == "test-agent"
        assert node.action_name == "bash"
        assert node.is_risk_event is False
        assert node.node_type == "tool_call"
        assert node.tool_call_seq == 0
        assert node.tool_call_id == "call-001"
        assert unified.to_event_desc()["aux_ids"]["tool_call_id"] == "call-001"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_parse_permission_interrupt_event() -> None:
        """验证安全检测事件被正确标记为 is_risk_event=True。"""
        preprocessor = DataPreprocessor(AgentSSASConfig())
        raw_event = TestDataPreprocessor._make_permission_interrupt_raw_event()
        events = await preprocessor.parse(raw_event)
        unified = events[-1]
        node = unified.event_node
        assert node.is_risk_event is True
        assert node.event_class == "security"
        assert node.risk_source == "PermissionInterruptRail"
        assert node.risk_type == "tool_permission_denied"
        assert node.risk_level == "high"
        # risk_assessment 包含 decision 和 evidence
        assert node.risk_assessment is not None
        assert node.risk_assessment["decision"] == "reject"
        assert node.risk_assessment["evidence"] == {"reason": "denied"}

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_parse_fields_correctness() -> None:
        """验证解析后 UnifiedEvent 各字段正确性。"""
        preprocessor = DataPreprocessor(AgentSSASConfig())
        raw_event = TestDataPreprocessor._make_tool_input_raw_event()
        events = await preprocessor.parse(raw_event)
        unified = events[-1]
        # event_id 已生成
        assert unified.event_id
        # trace 已构建
        assert unified.trace.trace_id == "test-trace"
        # event_node 已注册到 trace.all_nodes
        assert unified.event_node.node_id in unified.trace.all_nodes
        # input_content 来自 tool_args
        assert "command" in unified.event_node.input_content

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_auto_session_start_node() -> None:
        """验证无显式 session 事件时自动生成 session 节点。"""
        preprocessor = DataPreprocessor(AgentSSASConfig())
        raw_event = TestDataPreprocessor._make_tool_input_raw_event()
        events = await preprocessor.parse(raw_event)
        unified = events[0]
        # 自动生成的 session 节点应存在于 trace.all_nodes
        session_node_id = "test-session_session"
        assert session_node_id in unified.trace.all_nodes
        start_node = unified.trace.all_nodes[session_node_id]
        assert start_node.node_type == "session"
        assert start_node.action_name == "session_start"
        assert start_node.interaction_seq == -1

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_parse_invalid_raw_event_type() -> None:
        """验证传入非 dict 时抛出 ValueError。"""
        preprocessor = DataPreprocessor(AgentSSASConfig())
        with pytest.raises(ValueError):
            await preprocessor.parse("not a dict")  # type: ignore[arg-type]

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_parse_missing_common_layer() -> None:
        """验证缺少 common 层时抛出 ValueError。"""
        preprocessor = DataPreprocessor(AgentSSASConfig())
        with pytest.raises(ValueError):
            await preprocessor.parse({"payload": {}, "metadata": {}})
