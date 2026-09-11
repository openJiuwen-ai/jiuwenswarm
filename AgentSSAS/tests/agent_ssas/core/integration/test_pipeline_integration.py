# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""流水线集成测试。

验证完整流水线:raw_event -> parse -> pipeline -> RiskAssessment,
生命周期事件处理,安全检测事件处理。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel

# 确保能导入测试 fixtures
from tests.fixtures.event_factory import (
    create_raw_event,
    generate_permission_interrupt_event,
)


class TestPipelineIntegration:
    """流水线集成测试:raw_event -> parse -> pipeline -> RiskAssessment。"""

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_full_pipeline_lifecycle_event(ssas_home: Path) -> None:
        """测试完整流水线处理生命周期事件。

        raw_event(tool_input) -> DataPreprocessor.parse ->
        ThreatAnalysisPipeline.run -> RiskAssessment(无风险)。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()
        raw_event = create_raw_event(
            "tool_input",
            session_id="int-session",
            agent_id="int-agent",
            trace_id="int-trace",
            interaction_seq=0,
            tool_call_seq=0,
            tool_call_id="call-001",
        )
        assessment = await backend.report_event(raw_event)
        assert isinstance(assessment, RiskAssessment)
        # tool_input 事件:test_detection 通配订阅返回无风险
        assert assessment.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_full_pipeline_lifecycle_sequence(ssas_home: Path) -> None:
        """测试生命周期事件序列处理。

        依次上报 invoke_start -> llm_input -> tool_input -> tool_output ->
        llm_output -> invoke_end,验证每个事件都返回 RiskAssessment。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()
        events = [
            create_raw_event(
                "invoke_start",
                session_id="seq-session",
                agent_id="seq-agent",
                trace_id="seq-trace",
                interaction_seq=0,
            ),
            create_raw_event(
                "llm_input",
                session_id="seq-session",
                agent_id="seq-agent",
                trace_id="seq-trace",
                interaction_seq=0,
                llm_call_seq=0,
            ),
            create_raw_event(
                "tool_input",
                session_id="seq-session",
                agent_id="seq-agent",
                trace_id="seq-trace",
                interaction_seq=0,
                tool_call_seq=0,
                tool_call_id="call-001",
            ),
            create_raw_event(
                "tool_output",
                session_id="seq-session",
                agent_id="seq-agent",
                trace_id="seq-trace",
                interaction_seq=0,
                tool_call_seq=0,
                tool_call_id="call-001",
            ),
            create_raw_event(
                "llm_output",
                session_id="seq-session",
                agent_id="seq-agent",
                trace_id="seq-trace",
                interaction_seq=0,
                llm_call_seq=0,
            ),
            create_raw_event(
                "invoke_end",
                session_id="seq-session",
                agent_id="seq-agent",
                trace_id="seq-trace",
                interaction_seq=0,
            ),
        ]
        for raw_event in events:
            assessment = await backend.report_event(raw_event)
            assert isinstance(assessment, RiskAssessment)
        # 全部为生命周期事件,最终聚合应为无风险
        # (BlankThreatAnalyzer 和 AgentMossAnalyzer 都返回 safe)

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_full_pipeline_security_event(ssas_home: Path) -> None:
        """测试安全检测事件处理。

        permission_interrupt_tool 事件经流水线后,
        security_rail_detection 为 notify 模式,report_event 不等待后台
        检测,直接返回无风险 RiskAssessment。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()
        raw_event = generate_permission_interrupt_event(
            session_id="sec-session",
            agent_id="sec-agent",
            trace_id="sec-trace",
            interaction_seq=0,
            tool_call_seq=0,
            risk_level="high",
            risk_type="tool_permission_denied",
        )
        assessment = await backend.report_event(raw_event)
        assert isinstance(assessment, RiskAssessment)
        # security_rail_detection 模块订阅 permission_interrupt_tool,
        # notify 模式:report_event 不等待后台检测,直接返回无风险
        assert assessment.has_risk is False
        assert assessment.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_pipeline_persists_raw_events(ssas_home: Path) -> None:
        """验证 raw_event 被持久化到 SQLite 数据库。"""
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()
        raw_event = create_raw_event(
            "tool_input",
            session_id="persist-session",
            trace_id="persist-trace",
        )
        await backend.report_event(raw_event)
        # 从存储查询 raw_events
        events = await backend._storage.get_events_by_trace_id(
            "persist-trace"
        )
        # 至少应有 raw_event 记录
        assert len(events) >= 1

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level0
    async def test_agent_moss_analyzes_tool_event_in_real_pipeline(
        ssas_home: Path,
    ) -> None:
        """AgentMoss notify pipeline stores a real dangerous-command report."""
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()
        raw_event = create_raw_event(
            "tool_input",
            session_id="moss-session",
            agent_id="moss-agent",
            trace_id="moss-trace",
            interaction_seq=0,
            tool_call_seq=0,
            tool_call_id="moss-call-1",
            payload={
                "tool_name": "bash",
                "content": {"tool_args": {"command": "rm -rf /tmp/demo"}},
            },
        )

        # AgentMoss is advisory notify-mode analysis, so report_event itself
        # remains fail-open while the module result is stored asynchronously.
        assessment = await backend.report_event(raw_event)
        assert assessment.risk_level == RiskLevel.SAFE
        await asyncio.sleep(0.05)

        module = backend._module_manager.get_module("agent_moss")
        assert module is not None
        reports = await module.storage.result_store.get_events(limit=20)
        moss_reports = [
            report
            for report in reports
            if report.get("module_name") == "agent_moss"
            and report.get("risk_score", 0) >= 80
        ]
        assert moss_reports
        report = moss_reports[0]
        assert report["risk_level"] == "high"
        assert report["evidence"]["decision_mode"] == "advisory_notify"
        assert report["evidence"]["correlation"]["tool_call_id"] == "moss-call-1"
