# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""流水线模块单元测试。

验证 _aggregate_reports 聚合策略、空报告列表返回无风险、
多报告聚合取最高风险等级、run 方法端到端(用 mock module_manager)。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_ssas.core.framework.analysis_pipeline.pipeline import ThreatAnalysisPipeline
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel
from agent_ssas.core.framework.core_types.event import EventNode, Trace, UnifiedEvent


def _make_unified_event(event_type: str = "tool_input") -> UnifiedEvent:
    """构造测试用 UnifiedEvent。"""
    node = EventNode(
        node_id="n1",
        node_type="tool_call",
        parent_node_id="",
        next_node_id="",
        session_id="s1",
        interaction_seq=0,
        agent_id="a1",
        event_type=event_type,
        event_class="lifecycle",
    )
    trace = Trace(trace_id="t1")
    return UnifiedEvent(event_node=node, trace=trace, event_id="e1")


class TestAggregateReports:
    """_aggregate_reports 聚合策略。"""

    @staticmethod
    def _make_pipeline(ssas_config: AgentSSASConfig) -> ThreatAnalysisPipeline:
        """构造 ThreatAnalysisPipeline,module_manager 用 mock。"""
        mock_mm = MagicMock()
        return ThreatAnalysisPipeline(ssas_config, mock_mm)

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_empty_reports_returns_safe(ssas_config: AgentSSASConfig) -> None:
        """验证空报告列表返回无风险 RiskAssessment。"""
        pipeline = TestAggregateReports._make_pipeline(ssas_config)
        result = pipeline._aggregate_reports([])
        assert result.has_risk is False
        assert result.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_aggregate_takes_highest_risk_level(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证多报告聚合取最高风险等级。"""
        pipeline = TestAggregateReports._make_pipeline(ssas_config)
        reports = [
            {
                "has_risk": False,
                "risk_level": "safe",
                "risk_type": "",
                "risk_score": 0.0,
                "confidence": 1.0,
                "detected_threats": [],
                "evidence": {},
                "module_name": "module_a",
            },
            {
                "has_risk": True,
                "risk_level": "high",
                "risk_type": "tool_misuse",
                "risk_score": 75.0,
                "confidence": 0.9,
                "detected_threats": ["tool_misuse"],
                "evidence": {"reason": "denied"},
                "module_name": "module_b",
            },
        ]
        result = pipeline._aggregate_reports(reports)
        assert result.has_risk is True
        assert result.risk_level == RiskLevel.HIGH
        assert result.risk_type == "tool_misuse"
        assert result.risk_score == 75.0
        assert result.confidence == 0.9
        assert "tool_misuse" in result.detected_threats

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_aggregate_merges_detected_threats(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证 detected_threats 去重保序合并。"""
        pipeline = TestAggregateReports._make_pipeline(ssas_config)
        reports = [
            {
                "has_risk": True,
                "risk_level": "medium",
                "risk_type": "type_a",
                "detected_threats": ["threat_a", "threat_b"],
                "evidence": {},
                "module_name": "m1",
            },
            {
                "has_risk": True,
                "risk_level": "high",
                "risk_type": "type_b",
                "detected_threats": ["threat_b", "threat_c"],
                "evidence": {},
                "module_name": "m2",
            },
        ]
        result = pipeline._aggregate_reports(reports)
        assert result.detected_threats == ["threat_a", "threat_b", "threat_c"]

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_aggregate_merges_recommended_actions(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证 recommended_actions 去重合并,空时默认 ["log"]。"""
        pipeline = TestAggregateReports._make_pipeline(ssas_config)
        # 空列表 -> 默认 ["log"]
        result = pipeline._aggregate_reports(
            [
                {
                    "has_risk": False,
                    "risk_level": "safe",
                    "detected_threats": [],
                    "evidence": {},
                    "module_name": "m1",
                }
            ]
        )
        assert result.recommended_actions == ["log"]

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_aggregate_unknown_risk_level_falls_back_safe(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证未知风险等级字符串回退为 SAFE。"""
        pipeline = TestAggregateReports._make_pipeline(ssas_config)
        reports = [
            {
                "has_risk": False,
                "risk_level": "unknown_level",
                "detected_threats": [],
                "evidence": {},
                "module_name": "m1",
            }
        ]
        result = pipeline._aggregate_reports(reports)
        assert result.risk_level == RiskLevel.SAFE


class TestPipelineRun:
    """run 方法端到端(mock module_manager)。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_run_no_subscribers_returns_safe(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证无订阅模块时直接返回无风险结果。"""
        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(return_value=[])
        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm)
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        assert result.has_risk is False
        assert result.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_run_with_auth_subscriber_returns_report(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证 auth 模式订阅者时同步等待结果并返回聚合结果。"""
        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("test_module", "auth")]
        )
        # mock DetectionModule:modeler 和 analyzer
        mock_module = MagicMock()
        mock_module.modeler.build_model = AsyncMock(
            return_value={"event_type": "tool_input"}
        )
        mock_module.analyzer.analyze = AsyncMock(
            return_value={
                "has_risk": False,
                "risk_level": "safe",
                "risk_type": "",
                "risk_score": 0.0,
                "confidence": 1.0,
                "detected_threats": [],
                "evidence": {},
                "module_name": "test_module",
            }
        )
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)
        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm)
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        assert isinstance(result, RiskAssessment)
        assert result.has_risk is False

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_run_all_notify_returns_safe(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证全 notify 模式订阅者时返回无风险结果(检测后台异步进行)。"""
        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("test_module", "notify")]
        )
        mock_module = MagicMock()
        mock_module.modeler.build_model = AsyncMock(
            return_value={"event_type": "tool_input"}
        )
        mock_module.analyzer.analyze = AsyncMock(
            return_value={
                "has_risk": False,
                "risk_level": "safe",
                "module_name": "test_module",
            }
        )
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)
        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm)
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        # 全 notify 返回无风险
        assert result.has_risk is False
        assert result.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_run_modeler_failure_skips_module(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证建模失败时跳过该模块,不影响聚合。"""
        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("bad_module", "auth")]
        )
        mock_module = MagicMock()
        mock_module.modeler.build_model = AsyncMock(
            side_effect=RuntimeError("modeler failed")
        )
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)
        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm)
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        # 建模失败 -> 无报告 -> 无风险
        assert result.has_risk is False
        assert result.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_run_analyzer_failure_skips_module(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证分析失败时跳过该模块。"""
        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("bad_module", "auth")]
        )
        mock_module = MagicMock()
        mock_module.modeler.build_model = AsyncMock(return_value={})
        mock_module.analyzer.analyze = AsyncMock(
            side_effect=RuntimeError("analyzer failed")
        )
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)
        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm)
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        assert result.has_risk is False


class TestPipelineAuthTimeout:
    """auth 模式超时控制测试。"""

    @staticmethod
    def _make_pipeline_with_timeout(
        ssas_config: AgentSSASConfig, mock_mm: MagicMock, auth_timeout: float = 0.01
    ) -> ThreatAnalysisPipeline:
        """构造带超时配置的 ThreatAnalysisPipeline。"""
        ssas_config.auth_timeout = auth_timeout
        return ThreatAnalysisPipeline(ssas_config, mock_mm)

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_auth_timeout_returns_allow_policy(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证 auth 超时后按默认 allow 策略返回无风险报告。"""
        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("slow_module", "auth")]
        )
        mock_module = MagicMock()
        mock_module.config = {"auth_timeout_policy": "allow", "analytic_type_id": 1}

        async def _slow_build_model(event_desc):
            await asyncio.sleep(10)  # 远超 timeout

        mock_module.modeler.build_model = _slow_build_model
        mock_module.analyzer.analyze = AsyncMock(return_value={})
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)

        pipeline = TestPipelineAuthTimeout._make_pipeline_with_timeout(
            ssas_config, mock_mm, auth_timeout=0.01
        )
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        # 超时后按 allow 策略返回无风险
        assert isinstance(result, RiskAssessment)
        assert result.has_risk is False
        assert result.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_auth_timeout_returns_reject_policy(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证 auth 超时后按 reject 策略返回高风险报告。"""
        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("slow_module", "auth")]
        )
        mock_module = MagicMock()
        mock_module.config = {
            "auth_timeout_policy": "reject",
            "analytic_type_id": 1,
        }

        async def _slow_build_model(event_desc):
            await asyncio.sleep(10)

        mock_module.modeler.build_model = _slow_build_model
        mock_module.analyzer.analyze = AsyncMock(return_value={})
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)

        pipeline = TestPipelineAuthTimeout._make_pipeline_with_timeout(
            ssas_config, mock_mm, auth_timeout=0.01
        )
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        # 超时后按 reject 策略返回高风险
        assert result.has_risk is True
        assert result.risk_level == RiskLevel.HIGH

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_auth_no_timeout_returns_normal_result(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证 auth 模块在超时前正常返回时使用实际结果。"""
        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("fast_module", "auth")]
        )
        mock_module = MagicMock()
        mock_module.config = {"auth_timeout_policy": "allow", "analytic_type_id": 1}
        mock_module.modeler.build_model = AsyncMock(
            return_value={"event_type": "tool_input"}
        )
        mock_module.analyzer.analyze = AsyncMock(
            return_value={
                "has_risk": True,
                "risk_level": "critical",
                "risk_type": "tool_misuse",
                "risk_score": 90.0,
                "confidence": 0.95,
                "detected_threats": ["tool_misuse"],
                "evidence": {"reason": "detected"},
                "module_name": "fast_module",
            }
        )
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)

        pipeline = TestPipelineAuthTimeout._make_pipeline_with_timeout(
            ssas_config, mock_mm, auth_timeout=5.0
        )
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        # 正常返回,不触发超时
        assert result.has_risk is True
        assert result.risk_level == RiskLevel.CRITICAL

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_auth_timeout_1s_actually_waits(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证配置 auth_timeout=1.0 时实际等待约 1 秒后超时。"""
        import time

        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("slow_module", "auth")]
        )
        mock_module = MagicMock()
        mock_module.config = {"auth_timeout_policy": "allow", "analytic_type_id": 1}

        async def _slow_build_model(event_desc):
            await asyncio.sleep(10)  # 远超 timeout

        mock_module.modeler.build_model = _slow_build_model
        mock_module.analyzer.analyze = AsyncMock(return_value={})
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)

        pipeline = TestPipelineAuthTimeout._make_pipeline_with_timeout(
            ssas_config, mock_mm, auth_timeout=1.0
        )
        unified = _make_unified_event()
        start = time.monotonic()
        result = await pipeline.run(unified)
        elapsed = time.monotonic() - start
        # 实际等待约 1 秒(允许一定误差)
        assert elapsed >= 0.9, f"期望等待至少0.9s,实际 {elapsed:.3f}s"
        assert elapsed < 2.0, f"期望等待不超过2s,实际 {elapsed:.3f}s"
        # 超时后按 allow 策略返回无风险
        assert result.has_risk is False
        assert result.risk_level == RiskLevel.SAFE

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_auth_timeout_0_3s_actually_waits(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证配置 auth_timeout=0.3 时实际等待约 0.3 秒后超时。"""
        import time

        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("slow_module", "auth")]
        )
        mock_module = MagicMock()
        mock_module.config = {"auth_timeout_policy": "allow", "analytic_type_id": 1}

        async def _slow_build_model(event_desc):
            await asyncio.sleep(10)

        mock_module.modeler.build_model = _slow_build_model
        mock_module.analyzer.analyze = AsyncMock(return_value={})
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)

        pipeline = TestPipelineAuthTimeout._make_pipeline_with_timeout(
            ssas_config, mock_mm, auth_timeout=0.3
        )
        unified = _make_unified_event()
        start = time.monotonic()
        result = await pipeline.run(unified)
        elapsed = time.monotonic() - start
        # 实际等待约 0.3 秒
        assert elapsed >= 0.25, f"期望等待至少0.25s,实际 {elapsed:.3f}s"
        assert elapsed < 1.0, f"期望等待不超过1s,实际 {elapsed:.3f}s"
        assert result.has_risk is False


class TestPipelineAlertPersistence:
    """告警统一落库测试(notify/auth 模式均写主库 alerts 表)。"""

    @staticmethod
    def _make_mock_mm(
        risk_report: dict | None,
    ) -> MagicMock:
        """构造 mock module_manager,analyze 返回指定报告。"""
        mock_mm = MagicMock()
        mock_module = MagicMock()
        mock_module.config = {"auth_timeout_policy": "allow", "analytic_type_id": 1}
        mock_module.modeler.build_model = AsyncMock(
            return_value={"event_type": "tool_input"}
        )
        mock_module.analyzer.analyze = AsyncMock(return_value=risk_report)
        mock_mm.get_module = MagicMock(return_value=mock_module)
        mock_mm.get_storage = MagicMock(return_value=None)
        return mock_mm

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_notify_risk_report_writes_alert(
        ssas_config: AgentSSASConfig,
        sqlite_store,
    ) -> None:
        """验证全 notify 模式下有风险报告仍写入 alerts 表。"""
        risk_report = {
            "has_risk": True,
            "risk_level": "high",
            "risk_type": "tool_permission_denied",
            "risk_score": 75.0,
            "confidence": 0.9,
            "detected_threats": ["tool_permission_denied"],
            "recommended_actions": ["log"],
            "evidence": {"risk_source": "PermissionInterruptRail"},
            "module_name": "security_rail_detection",
        }
        mock_mm = TestPipelineAlertPersistence._make_mock_mm(risk_report)
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("security_rail_detection", "notify")]
        )
        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm, storage=sqlite_store)
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        # 同步返回仍为无风险(notify 不阻塞)
        assert result.has_risk is False
        # 等待后台任务完成(含告警落库)
        await asyncio.gather(*pipeline._background_tasks, return_exceptions=True)
        alerts = await sqlite_store.get_alerts()
        assert len(alerts) == 1
        alert = alerts[0]
        # alert_id 格式为 {event_id}_{module_name}
        assert alert["alert_id"] == f"{unified.event_id}_security_rail_detection"
        assert alert["module_name"] == "security_rail_detection"
        assert alert["risk_level"] == "high"
        assert alert["risk_type"] == "tool_permission_denied"
        assert alert["session_id"] == "s1"
        assert alert["trace_id"] == "t1"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_notify_safe_report_no_alert(
        ssas_config: AgentSSASConfig,
        sqlite_store,
    ) -> None:
        """验证 notify 模式下无风险报告不写 alerts 表。"""
        safe_report = {
            "has_risk": False,
            "risk_level": "safe",
            "risk_type": "",
            "module_name": "security_rail_detection",
        }
        mock_mm = TestPipelineAlertPersistence._make_mock_mm(safe_report)
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("security_rail_detection", "notify")]
        )
        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm, storage=sqlite_store)
        unified = _make_unified_event()
        await pipeline.run(unified)
        await asyncio.gather(*pipeline._background_tasks, return_exceptions=True)
        alerts = await sqlite_store.get_alerts()
        assert alerts == []

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_auth_risk_report_writes_alert(
        ssas_config: AgentSSASConfig,
        sqlite_store,
    ) -> None:
        """验证 auth 模式下有风险报告同步写入 alerts 表。"""
        risk_report = {
            "has_risk": True,
            "risk_level": "critical",
            "risk_type": "dangerous_tool_operation",
            "risk_score": 90.0,
            "confidence": 0.95,
            "detected_threats": ["dangerous_tool_operation"],
            "evidence": {},
            "module_name": "agent_moss",
        }
        mock_mm = TestPipelineAlertPersistence._make_mock_mm(risk_report)
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("agent_moss", "auth")]
        )
        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm, storage=sqlite_store)
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        # auth 模式同步返回有风险结果
        assert result.has_risk is True
        assert result.risk_level == RiskLevel.CRITICAL
        # 告警同步写入完成
        alerts = await sqlite_store.get_alerts()
        assert len(alerts) == 1
        assert alerts[0]["alert_id"] == f"{unified.event_id}_agent_moss"
        assert alerts[0]["risk_level"] == "critical"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_storage_none_no_alert_no_error(
        ssas_config: AgentSSASConfig,
    ) -> None:
        """验证 storage=None 时不写告警且不报错(兼容独立测试场景)。"""
        risk_report = {
            "has_risk": True,
            "risk_level": "high",
            "risk_type": "tool_misuse",
            "module_name": "test_module",
        }
        mock_mm = TestPipelineAlertPersistence._make_mock_mm(risk_report)
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("test_module", "auth")]
        )
        # storage 缺省为 None
        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm)
        unified = _make_unified_event()
        result = await pipeline.run(unified)
        assert result.has_risk is True

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_multi_module_alerts_not_overwritten(
        ssas_config: AgentSSASConfig,
        sqlite_store,
    ) -> None:
        """验证同一事件多模块告警互不覆盖(alert_id 含 module_name)。"""
        # 两个 auth 模块,各自返回有风险报告
        reports = [
            {
                "has_risk": True,
                "risk_level": "high",
                "risk_type": "type_a",
                "module_name": "module_a",
            },
            {
                "has_risk": True,
                "risk_level": "medium",
                "risk_type": "type_b",
                "module_name": "module_b",
            },
        ]
        mock_mm = MagicMock()
        mock_mm.get_subscribers_with_mode = MagicMock(
            return_value=[("module_a", "auth"), ("module_b", "auth")]
        )
        modules = {}
        for name, report in zip(("module_a", "module_b"), reports):
            mock_module = MagicMock()
            mock_module.config = {
                "auth_timeout_policy": "allow",
                "analytic_type_id": 1,
            }
            mock_module.modeler.build_model = AsyncMock(return_value={})
            mock_module.analyzer.analyze = AsyncMock(return_value=report)
            modules[name] = mock_module
        mock_mm.get_module = MagicMock(side_effect=lambda n: modules[n])
        mock_mm.get_storage = MagicMock(return_value=None)

        pipeline = ThreatAnalysisPipeline(ssas_config, mock_mm, storage=sqlite_store)
        unified = _make_unified_event()
        await pipeline.run(unified)
        # 两个模块各写一条告警,互不覆盖
        alerts = await sqlite_store.get_alerts()
        assert len(alerts) == 2
        alert_ids = {a["alert_id"] for a in alerts}
        assert alert_ids == {
            f"{unified.event_id}_module_a",
            f"{unified.event_id}_module_b",
        }
