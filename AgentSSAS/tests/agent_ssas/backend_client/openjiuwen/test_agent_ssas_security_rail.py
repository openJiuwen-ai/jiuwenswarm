# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSASSecurityRail 采集逻辑单元测试 (level1)。"""

from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.harness.rails.security.base_security_rail import SecurityAllow

from agent_ssas.backend_client.openjiuwen.agent_ssas_security_rail import (
    _MODEL_EVENTS,
    _PIPELINE_EVENTS,
    AgentSSASSecurityRail,
)
from agent_ssas.core.framework.core_types.assessment import (
    RiskAssessment,
    RiskLevel,
)


class TestAgentSSASSecurityRailCollection:
    """验证 AgentSSASSecurityRail 的事件采集逻辑。"""

    @staticmethod
    @pytest.mark.level1
    def test_priority_is_80():
        """验证 priority=80。"""
        assert AgentSSASSecurityRail.priority == 80

    @staticmethod
    @pytest.mark.level1
    def test_supported_events_declared():
        """验证 supported_events 声明了 6 个管线事件。"""
        assert AgentSSASSecurityRail.supported_events == _PIPELINE_EVENTS
        assert len(_PIPELINE_EVENTS) == 6
        assert len(_MODEL_EVENTS) == 2

    @staticmethod
    @pytest.mark.level1
    def test_pipeline_events_contains_correct_events():
        """验证 _PIPELINE_EVENTS 包含正确的 6 个事件。"""
        expected = {
            AgentCallbackEvent.BEFORE_INVOKE,
            AgentCallbackEvent.AFTER_INVOKE,
            AgentCallbackEvent.BEFORE_MODEL_CALL,
            AgentCallbackEvent.AFTER_MODEL_CALL,
            AgentCallbackEvent.BEFORE_TOOL_CALL,
            AgentCallbackEvent.AFTER_TOOL_CALL,
        }
        assert _PIPELINE_EVENTS == expected

    @staticmethod
    @pytest.mark.level1
    def test_model_events_contains_correct_events():
        """验证 _MODEL_EVENTS 包含 BEFORE/AFTER_MODEL_CALL。"""
        expected = {
            AgentCallbackEvent.BEFORE_MODEL_CALL,
            AgentCallbackEvent.AFTER_MODEL_CALL,
        }
        assert _MODEL_EVENTS == expected

    @staticmethod
    @pytest.mark.level1
    def test_init_creates_modules():
        """验证 __init__ 创建了各模块实例。"""
        backend = MagicMock()
        rail = AgentSSASSecurityRail(backend=backend)

        assert rail._backend is backend
        assert rail._id_manager is not None
        assert rail._event_builder is not None
        assert rail._event_filter is not None
        assert rail._event_reporter is not None

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_run_security_check_returns_allow_on_filter_none():
        """过滤返回 None 时返回 SecurityAllow。"""
        backend = MagicMock()
        rail = AgentSSASSecurityRail(backend=backend)

        # 构造 mock security_ctx,让 event_filter 返回 None
        sec_ctx = MagicMock()
        sec_ctx.event = AgentCallbackEvent.BEFORE_INVOKE
        sec_ctx.callback_ctx = MagicMock()
        sec_ctx.callback_ctx.extra = {}

        # Mock event_builder 返回一个 dict
        rail._event_builder.build_event_dict = MagicMock(
            return_value={"common": {}, "payload": {}, "metadata": {}}
        )
        # Mock event_filter 返回 None
        rail._event_filter.filter_event = MagicMock(return_value=None)

        result = await rail.run_security_check(sec_ctx)
        assert isinstance(result, SecurityAllow)

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_run_security_check_reports_filtered_event():
        """验证 run_security_check 正确串联 builder→filter→reporter。"""
        backend = MagicMock()

        async def _report(raw_event):
            return RiskAssessment(risk_level=RiskLevel.SAFE)

        backend.report_event = _report
        rail = AgentSSASSecurityRail(backend=backend)

        # 构造 mock security_ctx
        sec_ctx = MagicMock()
        sec_ctx.event = AgentCallbackEvent.BEFORE_INVOKE
        sec_ctx.callback_ctx = MagicMock()
        sec_ctx.callback_ctx.extra = {}
        sec_ctx.callback_ctx.inputs = MagicMock()
        sec_ctx.callback_ctx.exception = None

        # 不 mock event_builder/filter,用真实实现
        result = await rail.run_security_check(sec_ctx)
        # 应返回 SecurityAllow(因为 backend 返回 SAFE)
        assert isinstance(result, SecurityAllow)
