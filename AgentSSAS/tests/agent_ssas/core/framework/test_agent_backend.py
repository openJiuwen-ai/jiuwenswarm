# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""接入适配模块单元测试。

验证 report_event 返回 RiskAssessment、fail-open 异常时返回无风险、
initialize 加载检测模块。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel


def _make_raw_event() -> dict:
    """构造测试用 raw_event。"""
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


class TestAgentSSASBackend:
    """AgentSSASBackend 进程内模式。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_report_event_returns_assessment(ssas_home: Path) -> None:
        """验证 report_event 返回 RiskAssessment。"""
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()
        assessment = await backend.report_event(_make_raw_event())
        assert assessment is not None
        assert isinstance(assessment, RiskAssessment)

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_report_event_returns_assessment_with_mock(
        ssas_home: Path,
    ) -> None:
        """验证 report_event 返回 RiskAssessment(mock 内部模块)。

        使用 mock 的 preprocessor 和 pipeline,隔离检测模块依赖。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        # mock preprocessor.parse 返回事件列表
        backend._preprocessor = MagicMock()
        backend._preprocessor.parse = AsyncMock(return_value=[MagicMock()])
        # mock pipeline.run 返回无风险 RiskAssessment
        backend._pipeline = MagicMock()
        backend._pipeline.run = AsyncMock(
            return_value=RiskAssessment(has_risk=False, risk_level=RiskLevel.SAFE)
        )
        assessment = await backend.report_event(
            {"common": {}, "payload": {}, "metadata": {}}
        )
        assert assessment is not None
        assert isinstance(assessment, RiskAssessment)

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_fail_open_on_exception(ssas_home: Path) -> None:
        """验证引擎异常时 fail-open 返回无风险 RiskAssessment。"""
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

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_initialize_loads_detection_modules(
        ssas_home: Path,
    ) -> None:
        """验证 initialize 加载检测模块。"""
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()
        # initialize 后检测模块管理器应已加载内置模块
        # test_detection 默认 disabled,security_rail_detection 默认 enabled
        module = backend._module_manager.get_module("security_rail_detection")
        assert module is not None

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_initialize_is_idempotent(ssas_home: Path) -> None:
        """验证 initialize 幂等:连续两次调用无异常,模块只加载一次。"""
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()
        await backend.initialize()  # 重复调用应安全返回
        assert backend._initialized is True
        module = backend._module_manager.get_module("security_rail_detection")
        assert module is not None

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_report_event_auto_waits_for_initialization(
        ssas_home: Path,
    ) -> None:
        """验证未先 initialize 时 report_event 自动等待初始化(fire-and-forget 竞态修复)。"""
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        # 模拟 patch 的 fire-and-forget:不 await initialize 直接 report_event
        assessment = await backend.report_event(_make_raw_event())
        assert isinstance(assessment, RiskAssessment)
        # 初始化已被自动触发并完成,检测模块可用
        assert backend._initialized is True
        module = backend._module_manager.get_module("security_rail_detection")
        assert module is not None

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_report_event_waits_inflight_init_task(
        ssas_home: Path,
    ) -> None:
        """验证在途 fire-and-forget 初始化任务被等待完成而非重复执行。"""
        import asyncio

        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        # 模拟 patch 的 ensure_future(backend.initialize())
        init_task = asyncio.ensure_future(backend.initialize())
        backend._init_task = init_task
        # 未等待初始化完成直接上报事件
        assessment = await backend.report_event(_make_raw_event())
        assert isinstance(assessment, RiskAssessment)
        # 在途任务完成后初始化标志置位
        await init_task
        assert backend._initialized is True

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_security_event_writes_alert_via_notify(
        ssas_home: Path,
    ) -> None:
        """验证 permission_interrupt_tool 事件经 notify 路径写入 alerts 表。

        该用例复现并验证告警丢失根因修复:全 notify 订阅下同步返回无风险,
        但有风险的报告由流水线后台任务写入主库 alerts 表。
        """
        import asyncio

        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        raw_event = _make_raw_event()
        # 改写为安全检测事件(PermissionInterruptRail deny 场景)
        raw_event["common"]["event_type"] = "permission_interrupt_tool"
        raw_event["common"]["event_class"] = "security"
        raw_event["payload"]["risk_source"] = "PermissionInterruptRail"
        raw_event["payload"]["risk_type"] = "tool_permission_denied"
        raw_event["payload"]["risk_level"] = "high"
        raw_event["payload"]["decision"] = "reject"

        assessment = await backend.report_event(raw_event)
        # observe 默认策略(全 notify)同步返回无风险
        assert assessment.risk_level == RiskLevel.SAFE
        # 等待 notify 后台任务完成(含告警落库)
        await asyncio.gather(
            *backend._pipeline._background_tasks, return_exceptions=True
        )
        alerts = await backend._storage.get_alerts()
        assert len(alerts) == 1
        alert = alerts[0]
        # alert_id 含 module_name,可追溯产生告警的检测模块
        assert alert["alert_id"].endswith("_security_rail_detection")
        assert alert["module_name"] == "security_rail_detection"
        assert alert["risk_level"] == "high"
        assert alert["risk_type"] == "tool_permission_denied"
        # timestamp_text 列为本地时区可读格式(直接查表列验证)
        row = backend._storage._conn.execute(
            "SELECT timestamp_text FROM alerts WHERE alert_id = ?",
            (alert["alert_id"],),
        ).fetchone()
        assert row is not None and len(row[0]) > 0

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_initialize_triggers_ttl_cleanup(ssas_home: Path) -> None:
        """验证 initialize 启动时执行 TTL 清理(预置过期数据被清除)。"""
        import time

        from agent_ssas.core.framework.storage.sqlite_store import SQLiteStore

        config = AgentSSASConfig(ssas_home=str(ssas_home))
        # 预置过期数据到主库(先以同一路径打开写入;AgentSSASBackend 构造时
        # 才会创建 ssas 目录,此处需先建目录)
        db_path = ssas_home / "ssas" / "ssas_core.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        pre_store = SQLiteStore(db_path)
        old_ts = time.time() - 31 * 86400  # 31 天前,超过 event_ttl=30
        await pre_store.record_event(
            {
                "event_id": "stale_event",
                "event_type": "invoke_start",
                "timestamp": old_ts,
            }
        )
        await pre_store.record_raw_event(
            {
                "common": {
                    "event_type": "invoke_start",
                    "event_class": "lifecycle",
                    "timestamp": old_ts,
                    "session_id": "s1",
                }
            }
        )
        pre_store._conn.close()

        backend = AgentSSASBackend(config)
        await backend.initialize()  # 启动清理应删除过期事件

        events = await backend._storage.get_events()
        raw_events = [
            r
            for r in backend._storage._conn.execute(
                "SELECT data FROM raw_events"
            ).fetchall()
        ]
        assert all(e["event_id"] != "stale_event" for e in events)
        assert len(raw_events) == 0
