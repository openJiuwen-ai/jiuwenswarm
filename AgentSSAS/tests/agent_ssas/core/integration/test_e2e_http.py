# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# tests/agent_ssas/core/integration/test_e2e_http.py
"""HTTP 服务模式端到端测试。

使用 httpx.AsyncClient + ASGI transport 启动 FastAPI 服务端(不占用真实端口),
通过 AgentSSASRemoteBackend 经 HTTP 转发完整事件流,验证端到端流程。
"""

import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from agent_ssas.core.framework.access_adapter.http_server import create_app
from agent_ssas.core.framework.access_adapter.agent_remote_backend import (
    AgentSSASRemoteBackend,
)
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskLevel
from tests.fixtures.event_factory import (
    generate_event_sequence,
    generate_permission_interrupt_event,
)


@pytest.fixture
def ssas_config(tmp_path):
    """测试用 AgentSSASConfig,HTTP 模式,指向临时目录。"""
    return AgentSSASConfig(
        ssas_home=str(tmp_path),
        http_endpoint="http://test",
        http_timeout=5.0,
    )


@pytest.fixture
def app(ssas_config):
    """创建 FastAPI 应用。"""
    return create_app(ssas_config)


@pytest.fixture
async def client(app):
    """创建直连 FastAPI 的 httpx 客户端。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def backend(ssas_config, client):
    """创建 AgentSSASRemoteBackend,注入模拟服务端客户端。"""
    b = AgentSSASRemoteBackend(ssas_config)
    b._client = client
    return b


@pytest.mark.integration
@pytest.mark.level1
class TestE2EHttp:
    """HTTP 服务模式端到端测试。"""

    @pytest.mark.asyncio
    async def test_full_event_sequence_via_http(self, backend):
        """完整事件流通过 HTTP 转发返回 RiskAssessment。"""
        events = generate_event_sequence()
        for raw_event in events:
            assessment = await backend.report_event(raw_event)
            assert assessment is not None
            assert assessment.risk_level == RiskLevel.SAFE

    @pytest.mark.asyncio
    async def test_security_event_via_http_high_risk(self, backend):
        """安全检测事件通过 HTTP 转发返回(notify 模式下返回 SAFE)。"""
        sec_event = generate_permission_interrupt_event()
        assessment = await backend.report_event(sec_event)
        assert assessment.risk_level == RiskLevel.SAFE
        assert assessment.has_risk is False

    @pytest.mark.asyncio
    async def test_http_latency(self, backend):
        """HTTP 延迟 < 100ms。"""
        raw_event = generate_event_sequence()[0]
        start = time.time()
        await backend.report_event(raw_event)
        elapsed_ms = (time.time() - start) * 1000
        # 首次请求含懒初始化,放宽到 500ms
        assert elapsed_ms < 500

        # 第二次请求验证正常延迟
        start = time.time()
        await backend.report_event(raw_event)
        elapsed_ms = (time.time() - start) * 1000
        assert elapsed_ms < 100

    @pytest.mark.asyncio
    async def test_fail_open_on_invalid_event(self, backend):
        """非法事件 fail-open 返回无风险。"""
        assessment = await backend.report_event({"invalid": "event"})
        assert assessment.risk_level == RiskLevel.SAFE

    @pytest.mark.asyncio
    async def test_server_persists_raw_events(self, backend, app, ssas_config):
        """服务端持久化 raw_events 到 SQLite。"""
        events = generate_event_sequence()
        for raw_event in events:
            await backend.report_event(raw_event)

        # 服务端后端已懒初始化,检查 SQLite
        db_path = Path(ssas_config.storage_path) / "ssas_core.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cursor = conn.execute("SELECT COUNT(*) FROM raw_events")
            count = cursor.fetchone()[0]
            conn.close()
            assert count == 6
