# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""安全护栏检测模块单元测试。

验证 SecurityRailAnalyzer 处理安全检测事件、跳过非安全检测事件。
"""

from __future__ import annotations

import pytest

from agent_ssas.core.detection_modules.security_rail_detection.security_rail_analyzer import (
    SecurityRailAnalyzer,
)


class TestSecurityRailDetection:
    """安全护栏检测模块。"""

    @staticmethod
    def _make_security_event_desc(
        risk_type: str = "tool_permission_denied",
        risk_level: str = "high",
        risk_source: str = "PermissionInterruptRail",
        evidence: dict | None = None,
    ) -> dict:
        """构造安全检测事件的事件描述 json。

        event_node 中包含 event_class="security" 及风险字段。
        """
        if evidence is None:
            evidence = {"reason": "denied"}
        return {
            "event_node": {
                "event_type": "permission_interrupt_tool",
                "event_class": "security",
                "action_name": "bash",
                "risk_source": risk_source,
                "risk_type": risk_type,
                "risk_level": risk_level,
                "risk_assessment": {
                    "decision": "reject",
                    "evidence": evidence,
                },
            },
            "aux_ids": {
                "trace_id": "t1",
                "session_id": "s1",
                "interaction_seq": 0,
            },
        }

    @staticmethod
    def _make_lifecycle_event_desc() -> dict:
        """构造生命周期事件的事件描述 json。"""
        return {
            "event_node": {
                "event_type": "tool_input",
                "event_class": "lifecycle",
                "action_name": "bash",
            },
            "aux_ids": {"trace_id": "t1"},
        }

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_analyzer_handles_security_event() -> None:
        """验证 SecurityRailAnalyzer 处理安全检测事件,输出风险报告。"""
        analyzer = SecurityRailAnalyzer()
        event_desc = TestSecurityRailDetection._make_security_event_desc()
        report = await analyzer.analyze(event_desc)
        assert report["has_risk"] is True
        assert report["risk_level"] == "high"
        assert report["risk_type"] == "tool_permission_denied"
        assert report["risk_score"] == 75.0  # high -> 75
        assert report["detected_threats"] == ["tool_permission_denied"]
        assert report["module_name"] == "security_rail_detection"
        assert report["analytic_name"] == "Tool Permission Denied"
        assert "PermissionInterruptRail" in report["description"]
        # evidence 为自定义字段,包含 risk_source 和 decision(见 9.4.2 节)
        assert report["evidence"]["risk_source"] == "PermissionInterruptRail"
        assert report["evidence"]["decision"] == "reject"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_analyzer_skips_lifecycle_event() -> None:
        """验证 SecurityRailAnalyzer 跳过非安全检测事件(lifecycle)。"""
        analyzer = SecurityRailAnalyzer()
        event_desc = TestSecurityRailDetection._make_lifecycle_event_desc()
        report = await analyzer.analyze(event_desc)
        assert report["has_risk"] is False
        assert report["risk_level"] == "safe"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_analyzer_handles_non_dict_input() -> None:
        """验证传入非 dict 时返回无风险报告。"""
        analyzer = SecurityRailAnalyzer()
        report = await analyzer.analyze("not a dict")  # type: ignore[arg-type]
        assert report["has_risk"] is False

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_analyzer_evidence_from_risk_assessment() -> None:
        """验证 evidence 包含 risk_source 和 decision(来自 risk_assessment)。"""
        analyzer = SecurityRailAnalyzer()
        event_desc = TestSecurityRailDetection._make_security_event_desc(
            evidence={"detail": "tool not allowed"}
        )
        report = await analyzer.analyze(event_desc)
        # evidence 为自定义字段,包含 risk_source 和 decision(见 9.4.2 节)
        # decision 优先从 risk_assessment 中获取
        assert report["evidence"]["risk_source"] == "PermissionInterruptRail"
        assert report["evidence"]["decision"] == "reject"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_level_to_score_mapping() -> None:
        """验证 _level_to_score 风险等级到分数的映射。"""
        assert SecurityRailAnalyzer._level_to_score("safe") == 0.0
        assert SecurityRailAnalyzer._level_to_score("low") == 25.0
        assert SecurityRailAnalyzer._level_to_score("medium") == 50.0
        assert SecurityRailAnalyzer._level_to_score("high") == 75.0
        assert SecurityRailAnalyzer._level_to_score("critical") == 100.0
        # 未知等级回退 50.0
        assert SecurityRailAnalyzer._level_to_score("unknown") == 50.0
