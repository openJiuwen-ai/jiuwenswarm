# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""端到端验证测试。

使用 event_factory 产生完整事件序列,驱动 AgentSSASBackend 完整流水线,
验证威胁日志文件生成和 SQLite 数据库中的事件记录。
不依赖 jiuwenswarm 运行时。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel
from tests.fixtures.event_factory import (
    create_raw_event,
    generate_event_sequence,
    generate_permission_interrupt_event,
)


class TestEndToEnd:
    """端到端验证:虚拟事件驱动完整流水线。"""

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_full_event_sequence_drives_pipeline(
        ssas_home: Path,
    ) -> None:
        """使用 event_factory 产生完整事件序列,驱动 AgentSSASBackend。

        验证:
        1. 每个事件都返回 RiskAssessment
        2. 威胁日志文件已生成
        3. SQLite 数据库中记录了事件
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        # 产生完整事件序列
        events = generate_event_sequence(
            session_id="e2e-session",
            agent_id="e2e-agent",
            trace_id="e2e-trace",
            interaction_seq=0,
        )

        # 依次上报所有事件
        assessments: list[RiskAssessment] = []
        for raw_event in events:
            assessment = await backend.report_event(raw_event)
            assert isinstance(assessment, RiskAssessment)
            assessments.append(assessment)

        # 生命周期事件应全部返回无风险(BlankThreatAnalyzer 和 AgentMossAnalyzer)
        for a in assessments:
            assert a.risk_level == RiskLevel.SAFE

        # 验证威胁日志文件已生成(test_detection 通配订阅,会触发呈现)
        # 等待 notify 模式异步任务完成写入
        await asyncio.sleep(0.2)
        threat_log_dir = Path(config.storage_path) / "reports" / "threat_log"
        threat_files = list(threat_log_dir.glob("threat_e2e-trace_*.json"))
        assert len(threat_files) >= 1, "应生成至少一个威胁日志文件"

        # 验证威胁日志内容为 OCSF Detection Finding 格式
        # 等待文件写入完成(notify 异步任务可能正在写入)
        ocsf = None
        for _ in range(10):
            try:
                with open(threat_files[0], "r", encoding="utf-8") as f:
                    ocsf = json.load(f)
                break
            except (json.JSONDecodeError, OSError):
                await asyncio.sleep(0.1)
        assert ocsf["activity_id"] == 1
        assert ocsf["category_uid"] == 2
        assert ocsf["class_uid"] == 2004
        assert ocsf["trace_id"] == "e2e-trace"

        # 验证 SQLite 数据库中记录了事件
        db_events = await backend._storage.get_events_by_trace_id(
            "e2e-trace"
        )
        # 至少应有 raw_event 记录
        assert len(db_events) >= 1
        # 验证 raw_event 来源标记
        raw_sources = [e for e in db_events if e.get("_source") == "raw_event"]
        assert len(raw_sources) >= 1

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_security_event_generates_risk_report(
        ssas_home: Path,
    ) -> None:
        """验证安全检测事件产生威胁日志。

        permission_interrupt_tool 事件经流水线后,
        security_rail_detection 为 notify 模式,report_event 不等待后台
        检测,直接返回无风险 RiskAssessment。后台 SecurityRailAnalyzer
        输出 HIGH 风险报告,威胁日志文件以 OCSF 格式落盘。

        注意:多个检测模块处理同一事件时,由于文件名以
        trace_id + 时间戳命名,同一秒内的报告可能互相覆盖。
        此处验证聚合后的 RiskAssessment 为无风险(notify 模式),
        并验证威胁日志文件以 OCSF 格式落盘。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        raw_event = generate_permission_interrupt_event(
            session_id="e2e-sec-session",
            agent_id="e2e-sec-agent",
            trace_id="e2e-sec-trace",
            interaction_seq=0,
            tool_call_seq=0,
            risk_level="high",
        )
        assessment = await backend.report_event(raw_event)
        # notify 模式:report_event 不等待后台检测,直接返回无风险
        assert assessment.has_risk is False
        assert assessment.risk_level == RiskLevel.SAFE

        # 等待 notify 模式后台异步任务完成写入威胁日志
        await asyncio.sleep(0.2)
        # 验证威胁日志文件已以 OCSF 格式落盘
        threat_log_dir = Path(config.storage_path) / "reports" / "threat_log"
        threat_files = list(
            threat_log_dir.glob("threat_e2e-sec-trace_*.json")
        )
        assert len(threat_files) >= 1, "应生成至少一个威胁日志文件"
        # 验证文件内容为 OCSF Detection Finding 格式
        with open(threat_files[0], "r", encoding="utf-8") as f:
            ocsf = json.load(f)
        assert ocsf["activity_id"] == 1
        assert ocsf["category_uid"] == 2
        assert ocsf["class_uid"] == 2004
        assert ocsf["trace_id"] == "e2e-sec-trace"
        # SecurityRailAnalyzer 的报告风险等级为 high,
        # 后台异步落盘的威胁日志证实有风险
        # 验证 ai_operation 中 tool_call 数据正确填充
        interactions = ocsf["ai_operation"]["interactions"]
        assert len(interactions) == 1
        llm_calls = interactions[0]["llm_calls"]
        assert len(llm_calls) == 1
        tool_calls = llm_calls[0]["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "bash"
        assert tool_calls[0]["input"] == '{"command": "rm -rf /"}'

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_mixed_event_sequence(ssas_home: Path) -> None:
        """验证生命周期事件序列后接安全检测事件。

        先上报完整生命周期事件序列,再上报安全检测事件,
        验证两类事件都能正确处理。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        # 生命周期事件序列
        lifecycle_events = generate_event_sequence(
            session_id="mix-session",
            agent_id="mix-agent",
            trace_id="mix-trace",
            interaction_seq=0,
        )
        for raw_event in lifecycle_events:
            assessment = await backend.report_event(raw_event)
            assert isinstance(assessment, RiskAssessment)
            assert assessment.risk_level == RiskLevel.SAFE

        # 安全检测事件
        security_event = generate_permission_interrupt_event(
            session_id="mix-session",
            agent_id="mix-agent",
            trace_id="mix-trace-sec",
            interaction_seq=1,
            tool_call_seq=0,
            risk_level="critical",
            risk_type="tool_permission_denied",
        )
        assessment = await backend.report_event(security_event)
        # notify 模式:report_event 不等待后台检测,直接返回无风险
        assert assessment.has_risk is False
        assert assessment.risk_level == RiskLevel.SAFE

        # 等待 notify 模式后台异步任务完成写入威胁日志
        await asyncio.sleep(0.2)
        # 验证两类威胁日志都已生成
        threat_log_dir = Path(config.storage_path) / "reports" / "threat_log"
        lifecycle_files = list(
            threat_log_dir.glob("threat_mix-trace_*.json")
        )
        security_files = list(
            threat_log_dir.glob("threat_mix-trace-sec_*.json")
        )
        assert len(lifecycle_files) >= 1
        assert len(security_files) >= 1

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_module_result_db_records(ssas_home: Path) -> None:
        """验证检测模块的 result.db 记录了威胁分析报告。

        test_detection 模块默认 disabled,通过 config 覆盖启用后,
        其 result.db 应记录每个事件的分析报告。
        """
        # test_detection 默认 disabled,通过 config 覆盖启用
        config = AgentSSASConfig(
            ssas_home=str(ssas_home),
            modules={"test_detection": {"enabled": True}},
        )
        backend = AgentSSASBackend(config)
        await backend.initialize()

        raw_event = create_raw_event(
            "tool_input",
            session_id="mod-session",
            agent_id="mod-agent",
            trace_id="mod-trace",
        )
        await backend.report_event(raw_event)
        # 等待 notify 模式异步任务完成写入
        await asyncio.sleep(0.1)

        # 查询 test_detection 模块的 result_store
        module = backend._module_manager.get_module("test_detection")
        assert module is not None
        reports = await module.storage.result_store.get_events(limit=10)
        assert len(reports) >= 1
        # 验证报告内容
        report = reports[0]
        assert "has_risk" in report
        assert "risk_level" in report
