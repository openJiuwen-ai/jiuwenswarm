# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""数据模型单元测试。

验证 RiskLevel 枚举、RiskAssessment.to_dict、EventNode 默认值(seq=-1)、
UnifiedEvent.to_event_desc 和 _build_aux_ids。
"""

from __future__ import annotations

import pytest

from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel
from agent_ssas.core.framework.core_types.event import (
    CURRENT_EVENT_VERSION,
    EventNode,
    Trace,
    UnifiedEvent,
)


class TestRiskLevel:
    """RiskLevel 枚举。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_enum_values() -> None:
        """验证五级风险枚举值。"""
        assert RiskLevel.SAFE.value == "safe"
        assert RiskLevel.LOW.value == "low"
        assert RiskLevel.MEDIUM.value == "medium"
        assert RiskLevel.HIGH.value == "high"
        assert RiskLevel.CRITICAL.value == "critical"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_comparison() -> None:
        """验证风险等级比较:SAFE < LOW < MEDIUM < HIGH < CRITICAL。"""
        assert RiskLevel.SAFE < RiskLevel.LOW
        assert RiskLevel.LOW < RiskLevel.MEDIUM
        assert RiskLevel.MEDIUM < RiskLevel.HIGH
        assert RiskLevel.HIGH < RiskLevel.CRITICAL
        assert RiskLevel.CRITICAL > RiskLevel.SAFE
        assert RiskLevel.HIGH >= RiskLevel.HIGH
        assert RiskLevel.SAFE <= RiskLevel.LOW


class TestRiskAssessment:
    """RiskAssessment 数据模型。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_to_dict() -> None:
        """验证 to_dict 序列化,risk_level 转为字符串值。"""
        assessment = RiskAssessment(
            has_risk=True,
            risk_level=RiskLevel.HIGH,
            risk_type="tool_misuse",
            risk_score=75.0,
            confidence=0.9,
            detected_threats=["tool_misuse"],
            recommended_actions=["log", "alert"],
            details={"module": "test"},
            evidence={"reason": "denied"},
        )
        d = assessment.to_dict()
        assert d["has_risk"] is True
        assert d["risk_level"] == "high"
        assert d["risk_type"] == "tool_misuse"
        assert d["risk_score"] == 75.0
        assert d["confidence"] == 0.9
        assert d["detected_threats"] == ["tool_misuse"]
        assert d["recommended_actions"] == ["log", "alert"]
        assert d["details"] == {"module": "test"}
        assert d["evidence"] == {"reason": "denied"}

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_defaults() -> None:
        """验证默认值为无风险。"""
        assessment = RiskAssessment()
        assert assessment.has_risk is False
        assert assessment.risk_level == RiskLevel.SAFE
        assert assessment.risk_score == 0.0
        assert assessment.detected_threats == []
        assert assessment.recommended_actions == ["log"]


class TestEventNode:
    """EventNode 数据模型。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_default_seq_values() -> None:
        """验证 seq 字段默认值为 -1。"""
        node = EventNode(
            node_id="n1",
            node_type="tool_call",
            parent_node_id="p1",
            next_node_id="",
            session_id="s1",
            interaction_seq=0,
        )
        # 通用字段默认值
        assert node.agent_id == ""
        assert node.input_content == ""
        assert node.output_content == ""
        assert node.action_name == ""
        assert node.event_type == ""
        assert node.event_class == ""
        assert node.timestamp == 0.0
        # seq 字段默认 -1
        assert node.llm_call_seq == -1
        assert node.tool_call_seq == -1
        # 安全检测字段默认值
        assert node.is_risk_event is False
        assert node.risk_source == ""
        assert node.risk_type == ""
        assert node.risk_level == ""
        assert node.risk_assessment is None

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_custom_values() -> None:
        """验证自定义字段正确赋值。"""
        node = EventNode(
            node_id="n2",
            node_type="tool_call",
            parent_node_id="p1",
            next_node_id="n3",
            session_id="s1",
            interaction_seq=1,
            agent_id="agent-1",
            action_name="bash",
            event_type="tool_input",
            event_class="lifecycle",
            source="AgentSSASSecurityRail",
            timestamp=1715000000.0,
            llm_call_seq=0,
            tool_call_seq=0,
            tool_call_id="call-001",
            is_risk_event=False,
        )
        assert node.node_id == "n2"
        assert node.node_type == "tool_call"
        assert node.parent_node_id == "p1"
        assert node.next_node_id == "n3"
        assert node.session_id == "s1"
        assert node.interaction_seq == 1
        assert node.agent_id == "agent-1"
        assert node.action_name == "bash"
        assert node.llm_call_seq == 0
        assert node.tool_call_seq == 0
        assert node.tool_call_id == "call-001"


class TestUnifiedEvent:
    """UnifiedEvent 数据模型。"""

    @staticmethod
    def _make_unified() -> UnifiedEvent:
        """构造测试用 UnifiedEvent。"""
        node = EventNode(
            node_id="n1",
            node_type="tool_call",
            parent_node_id="p1",
            next_node_id="",
            session_id="s1",
            interaction_seq=0,
            agent_id="a1",
            action_name="bash",
            event_type="tool_input",
            event_class="lifecycle",
            source="AgentSSASSecurityRail",
            timestamp=1715000000.0,
            llm_call_seq=0,
            tool_call_seq=0,
            tool_call_id="call-001",
        )
        trace = Trace(trace_id="t1", source_info={})
        return UnifiedEvent(event_node=node, trace=trace, event_id="e1")

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_to_event_desc() -> None:
        """验证 to_event_desc 输出事件描述 json。"""
        unified = TestUnifiedEvent._make_unified()
        desc = unified.to_event_desc()
        assert desc["event_id"] == "e1"
        assert desc["event_version"] == CURRENT_EVENT_VERSION
        assert "event_node" in desc
        assert "aux_ids" in desc
        assert "trace" in desc
        assert desc["event_node"]["node_id"] == "n1"
        assert desc["event_node"]["event_type"] == "tool_input"
        assert desc["event_node"]["tool_call_id"] == "call-001"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_build_aux_ids() -> None:
        """验证 _build_aux_ids 提取关联 ID 字段。"""
        unified = TestUnifiedEvent._make_unified()
        aux_ids = UnifiedEvent._build_aux_ids(
            unified.event_node, unified.trace
        )
        assert aux_ids["interaction_seq"] == 0
        assert aux_ids["session_id"] == "s1"
        assert aux_ids["agent_id"] == "a1"
        assert aux_ids["trace_id"] == "t1"
        assert aux_ids["llm_call_seq"] == 0
        assert aux_ids["tool_call_seq"] == 0
        assert aux_ids["tool_call_id"] == "call-001"
