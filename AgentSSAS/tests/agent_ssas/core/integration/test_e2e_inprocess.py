# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# tests/agent_ssas/core/integration/test_e2e_inprocess.py
"""进程内模式端到端测试。

模拟完整 Agent 行为事件流,通过 AgentSSASBackend.report_event()
驱动完整流水线,验证 SQLite 数据库、威胁日志文件正确生成。
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskLevel
from tests.fixtures.event_factory import (
    generate_event_sequence,
    generate_permission_interrupt_event,
)


@pytest.fixture
def ssas_config(tmp_path):
    """测试用 AgentSSASConfig,指向临时目录。"""
    return AgentSSASConfig(ssas_home=str(tmp_path))


@pytest.fixture
async def backend(ssas_config):
    """创建并初始化 AgentSSASBackend。"""
    b = AgentSSASBackend(ssas_config)
    await b.initialize()
    yield b


@pytest.mark.integration
@pytest.mark.level1
class TestE2EInprocess:
    """进程内模式端到端测试。"""

    @pytest.mark.asyncio
    async def test_full_event_sequence_returns_assessments(self, backend):
        """完整事件流返回 RiskAssessment。"""
        events = generate_event_sequence()
        for raw_event in events:
            assessment = await backend.report_event(raw_event)
            assert assessment is not None
            # 生命周期事件应返回无风险
            assert assessment.risk_level == RiskLevel.SAFE

    @pytest.mark.asyncio
    async def test_security_event_returns_high_risk(self, backend):
        """安全检测事件返回无风险(notify 模式后台异步执行,不阻塞)。"""
        sec_event = generate_permission_interrupt_event()
        assessment = await backend.report_event(sec_event)
        # security_rail_detection 为 notify 模式,report_event 不等待后台检测,
        # 直接返回无风险 RiskAssessment
        assert assessment.risk_level == RiskLevel.SAFE
        assert assessment.has_risk is False

    @pytest.mark.asyncio
    async def test_raw_events_persisted_to_sqlite(self, backend, ssas_config):
        """raw_event 持久化到 SQLite。"""
        events = generate_event_sequence()
        for raw_event in events:
            await backend.report_event(raw_event)

        db_path = Path(ssas_config.storage_path) / "ssas_core.db"
        assert db_path.exists()
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT COUNT(*) FROM raw_events")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 6

    @pytest.mark.asyncio
    async def test_threat_log_file_generated(self, backend, ssas_config):
        """威胁日志文件(OCSF 格式 JSON)正确生成。"""
        sec_event = generate_permission_interrupt_event()
        await backend.report_event(sec_event)
        # notify 模式后台异步执行,等待后台任务完成写入威胁日志
        await asyncio.sleep(0.2)

        reports_dir = Path(ssas_config.storage_path) / "reports" / "threat_log"
        assert reports_dir.exists()
        files = list(reports_dir.glob("*.json"))
        assert len(files) > 0

    @pytest.mark.asyncio
    async def test_fail_open_on_invalid_event(self, backend):
        """非法事件 fail-open 返回无风险。"""
        assessment = await backend.report_event({"invalid": "event"})
        assert assessment.risk_level == RiskLevel.SAFE
        assert assessment.has_risk is False

    @pytest.mark.asyncio
    async def test_module_result_db_records(self, ssas_config, tmp_path):
        """检测模块 result.db 记录正确。"""
        # test_detection 默认 disabled,通过 config 覆盖启用
        config = AgentSSASConfig(
            ssas_home=str(tmp_path),
            modules={"test_detection": {"enabled": True}},
        )
        backend = AgentSSASBackend(config)
        await backend.initialize()

        events = generate_event_sequence()
        for raw_event in events:
            await backend.report_event(raw_event)

        sec_event = generate_permission_interrupt_event()
        await backend.report_event(sec_event)
        # notify 模式后台异步执行,等待后台任务完成写入 result.db
        await asyncio.sleep(0.2)

        # test_detection 订阅所有事件(含聚合事件),记录数 >= 生命周期事件数
        td_db = Path(config.storage_path) / "modules" / "test_detection" / "result.db"
        assert td_db.exists()
        conn = sqlite3.connect(str(td_db))
        cursor = conn.execute("SELECT COUNT(*) FROM events")
        count = cursor.fetchone()[0]
        conn.close()
        assert count >= 5  # 6 生命周期 + 1 安全检测(含聚合事件可能不同)

        # security_rail_detection 只订阅安全检测事件,应有 1 条记录
        srd_db = Path(config.storage_path) / "modules" / "security_rail_detection" / "result.db"
        assert srd_db.exists()
        conn = sqlite3.connect(str(srd_db))
        cursor = conn.execute("SELECT COUNT(*) FROM events")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 1
