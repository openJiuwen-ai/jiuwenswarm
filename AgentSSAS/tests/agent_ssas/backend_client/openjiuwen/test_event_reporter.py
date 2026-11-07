# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""上报模块单元测试 (level1)。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.harness.rails.security.base_security_rail import (
    SecurityAlert,
    SecurityAlertLevel,
    SecurityAllow,
    SecurityReject,
)

from agent_ssas.backend_client.openjiuwen.event_reporter import EventReporter
from agent_ssas.core.framework.core_types.assessment import (
    RiskAssessment,
    RiskLevel,
)


class TestEventReporter:
    """验证 fail-open 逻辑和 RiskAssessment 到 SecurityDecision 的映射。"""

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_fail_open_on_backend_exception():
        """后端异常时返回 SecurityAllow。"""
        backend = MagicMock()
        backend.report_event = AsyncMock(side_effect=RuntimeError("connection failed"))
        reporter = EventReporter(backend)

        decision = await reporter.report({"common": {}, "payload": {}, "metadata": {}})

        assert isinstance(decision, SecurityAllow)

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_safe_report_swallows_exception():
        """safe_report 异常时不抛出。"""
        backend = MagicMock()
        backend.report_event = AsyncMock(side_effect=RuntimeError("connection failed"))
        reporter = EventReporter(backend)

        # 不应抛出异常
        await reporter.safe_report({"common": {"event_type": "test"}, "payload": {}, "metadata": {}})

    @staticmethod
    @pytest.mark.level1
    def test_critical_maps_to_reject():
        """risk_level=critical 映射为 SecurityReject。"""
        backend = MagicMock()
        reporter = EventReporter(backend, policy_name="active_protection")

        assessment = RiskAssessment(
            has_risk=True,
            risk_level=RiskLevel.CRITICAL,
            risk_type="test_threat",
        )
        decision = reporter.assessment_to_decision(assessment)

        assert isinstance(decision, SecurityReject)
        assert "[AgentSSAS] 阻断" in decision.message

    @staticmethod
    @pytest.mark.level1
    def test_high_maps_to_alert_error():
        """risk_level=high 映射为 SecurityAlert(ERROR)。"""
        backend = MagicMock()
        reporter = EventReporter(backend, policy_name="active_protection")

        assessment = RiskAssessment(
            has_risk=True,
            risk_level=RiskLevel.HIGH,
            risk_type="test_threat",
        )
        decision = reporter.assessment_to_decision(assessment)

        assert isinstance(decision, SecurityAlert)
        assert decision.level == SecurityAlertLevel.ERROR

    @staticmethod
    @pytest.mark.level1
    def test_medium_maps_to_alert_warning():
        """risk_level=medium 映射为 SecurityAlert(WARNING)。"""
        backend = MagicMock()
        reporter = EventReporter(backend, policy_name="active_protection")

        assessment = RiskAssessment(
            has_risk=True,
            risk_level=RiskLevel.MEDIUM,
        )
        decision = reporter.assessment_to_decision(assessment)

        assert isinstance(decision, SecurityAlert)
        assert decision.level == SecurityAlertLevel.WARNING

    @staticmethod
    @pytest.mark.level1
    def test_low_maps_to_alert_info():
        """risk_level=low 映射为 SecurityAlert(INFO)。"""
        backend = MagicMock()
        reporter = EventReporter(backend, policy_name="active_protection")

        assessment = RiskAssessment(
            has_risk=True,
            risk_level=RiskLevel.LOW,
        )
        decision = reporter.assessment_to_decision(assessment)

        assert isinstance(decision, SecurityAlert)
        assert decision.level == SecurityAlertLevel.INFO

    @staticmethod
    @pytest.mark.level1
    def test_safe_maps_to_allow():
        """risk_level=safe 映射为 SecurityAllow。"""
        backend = MagicMock()
        reporter = EventReporter(backend)

        assessment = RiskAssessment(risk_level=RiskLevel.SAFE)
        decision = reporter.assessment_to_decision(assessment)

        assert isinstance(decision, SecurityAllow)

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_report_returns_decision_from_assessment():
        """report 正常流程返回 assessment_to_decision 的结果。"""
        backend = MagicMock()

        async def _report(raw_event):
            return RiskAssessment(
                has_risk=True,
                risk_level=RiskLevel.HIGH,
                risk_type="test",
            )

        backend.report_event = _report
        reporter = EventReporter(backend, policy_name="active_protection")

        decision = await reporter.report({"common": {}, "payload": {}, "metadata": {}})
        assert isinstance(decision, SecurityAlert)
        assert decision.level == SecurityAlertLevel.ERROR
