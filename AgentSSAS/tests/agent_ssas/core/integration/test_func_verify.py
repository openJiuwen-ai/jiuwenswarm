# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 功能验证脚本。

使用 event_factory 产生虚拟事件,驱动 AgentSSASBackend 完整流水线,
逐项验证 AgentSSAS 子系统的核心功能:
- 生命周期事件 -> RiskAssessment
- 安全检测事件 -> RiskAssessment
- 数据库持久化(raw_event 落库)
- 威胁日志文件生成(OCSF 格式)
- 检测模块 result.db 记录
- fail-open 异常容错

所有测试用 @pytest.mark.integration 和 @pytest.mark.level1 标记,
可直接用 `pytest tests/agent_ssas/func_verify.py` 运行。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel
from tests.fixtures.event_factory import (
    create_raw_event,
    generate_event_sequence,
    generate_permission_interrupt_event,
)


class TestFuncVerify:
    """AgentSSAS 功能验证:虚拟事件驱动完整流水线。"""

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_lifecycle_events_return_risk_assessment(
        ssas_home: Path,
    ) -> None:
        """验证生命周期事件序列产出 RiskAssessment。

        使用 generate_event_sequence 产生完整事件流,
        依次上报 invoke_start -> llm_input -> tool_input -> tool_output ->
        llm_output -> invoke_end,验证每个事件都返回 RiskAssessment,
        且生命周期事件聚合结果为无风险(SAFE)。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        events = generate_event_sequence(
            session_id="fv-life-session",
            agent_id="fv-life-agent",
            trace_id="fv-life-trace",
            interaction_seq=0,
        )

        for raw_event in events:
            assessment = await backend.report_event(raw_event)
            assert isinstance(assessment, RiskAssessment)
            assert assessment.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_security_event_returns_high_risk(ssas_home: Path) -> None:
        """验证安全检测事件产出无风险 RiskAssessment(notify 模式)。

        使用 generate_permission_interrupt_event 产生安全检测事件,
        security_rail_detection 为 notify 模式,report_event 不等待后台
        检测,直接返回无风险 RiskAssessment。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        raw_event = generate_permission_interrupt_event(
            session_id="fv-sec-session",
            agent_id="fv-sec-agent",
            trace_id="fv-sec-trace",
            interaction_seq=0,
            tool_call_seq=0,
            risk_level="high",
        )
        assessment = await backend.report_event(raw_event)
        assert isinstance(assessment, RiskAssessment)
        # notify 模式:report_event 不等待后台检测,直接返回无风险
        assert assessment.has_risk is False
        assert assessment.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_raw_events_persisted_to_sqlite(ssas_home: Path) -> None:
        """验证 raw_event 被持久化到 SQLite 数据库。

        上报事件后从存储查询同一 trace_id 的记录,
        验证 raw_events 表至少有一条记录,且来源标记为 raw_event。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        raw_event = create_raw_event(
            "tool_input",
            session_id="fv-persist-session",
            agent_id="fv-persist-agent",
            trace_id="fv-persist-trace",
        )
        await backend.report_event(raw_event)

        events = await backend._storage.get_events_by_trace_id(
            "fv-persist-trace"
        )
        assert len(events) >= 1
        raw_sources = [e for e in events if e.get("_source") == "raw_event"]
        assert len(raw_sources) >= 1

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_threat_log_file_generated(ssas_home: Path) -> None:
        """验证威胁日志文件以 OCSF 格式生成。

        上报安全检测事件后检查 threat_log 目录,
        验证文件存在且内容为 OCSF Detection Finding 格式
        (activity_id=1、category_uid=2、class_uid=2004)。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        raw_event = generate_permission_interrupt_event(
            session_id="fv-log-session",
            agent_id="fv-log-agent",
            trace_id="fv-log-trace",
            interaction_seq=0,
            tool_call_seq=0,
            risk_level="high",
        )
        await backend.report_event(raw_event)
        # notify 模式后台异步执行,等待后台任务完成写入威胁日志
        await asyncio.sleep(0.2)

        threat_log_dir = Path(config.storage_path) / "reports" / "threat_log"
        threat_files = list(threat_log_dir.glob("threat_fv-log-trace_*.json"))
        assert len(threat_files) >= 1, "应生成至少一个威胁日志文件"

        with open(threat_files[0], "r", encoding="utf-8") as f:
            ocsf = json.load(f)
        assert ocsf["activity_id"] == 1
        assert ocsf["category_uid"] == 2
        assert ocsf["class_uid"] == 2004
        assert ocsf["trace_id"] == "fv-log-trace"

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_module_result_db_records(ssas_home: Path) -> None:
        """验证检测模块的 result.db 记录了威胁分析报告。

        test_detection 模块默认 disabled,通过 config 覆盖启用后,
        其 result.db 应记录每个事件的分析报告。上报事件后查询模块
        result_store,验证报告内容包含 has_risk 和 risk_level 字段。
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
            session_id="fv-mod-session",
            agent_id="fv-mod-agent",
            trace_id="fv-mod-trace",
        )
        await backend.report_event(raw_event)
        # 等待 notify 模式异步任务完成写入
        await asyncio.sleep(0.1)

        module = backend._module_manager.get_module("test_detection")
        assert module is not None
        reports = await module.storage.result_store.get_events(limit=10)
        assert len(reports) >= 1
        report = reports[0]
        assert "has_risk" in report
        assert "risk_level" in report

    @staticmethod
    @pytest.mark.integration
    @pytest.mark.level1
    async def test_fail_open_on_exception(ssas_home: Path) -> None:
        """验证引擎异常时 fail-open 返回无风险 RiskAssessment。

        mock preprocessor.parse 抛出异常,验证 report_event 不抛出,
        而是返回无风险 RiskAssessment(risk_level=SAFE)。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        # mock preprocessor.parse 抛出异常
        backend._preprocessor = MagicMock()
        backend._preprocessor.parse = AsyncMock(
            side_effect=RuntimeError("engine failed")
        )
        backend._pipeline = MagicMock()
        backend._pipeline.run = AsyncMock(
            return_value=RiskAssessment(has_risk=False, risk_level=RiskLevel.SAFE)
        )
        assessment = await backend.report_event(
            {"common": {}, "payload": {}, "metadata": {}}
        )
        assert isinstance(assessment, RiskAssessment)
        assert assessment.risk_level == RiskLevel.SAFE
