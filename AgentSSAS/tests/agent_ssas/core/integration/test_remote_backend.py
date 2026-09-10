# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# tests/agent_ssas/core/integration/test_remote_backend.py
"""AgentSSASRemoteBackend 测试。

测试 HTTP 模式客户端的转发、fail-open 等逻辑。
"""

import httpx
import pytest
from httpx import ASGITransport

from agent_ssas.core.framework.access_adapter.http_server import create_app
from agent_ssas.core.framework.access_adapter.agent_remote_backend import (
    AgentSSASRemoteBackend,
)
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskLevel


@pytest.fixture
def ssas_config(tmp_path):
    """测试用 AgentSSASConfig,指向临时目录,HTTP 模式。"""
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
async def server_client(app):
    """创建直连 FastAPI 的 httpx 客户端(模拟服务端)。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def backend(ssas_config, server_client):
    """创建 AgentSSASRemoteBackend,注入模拟的 httpx 客户端。"""
    b = AgentSSASRemoteBackend(ssas_config)
    # 替换内部 client 为模拟服务端的 client
    b._client = server_client
    return b


@pytest.mark.integration
@pytest.mark.level1
class TestAgentSSASRemoteBackend:
    """AgentSSASRemoteBackend 测试。"""

    @pytest.mark.asyncio
    async def test_report_event_returns_assessment(self, backend):
        """正常转发事件返回 RiskAssessment。"""
        raw_event = {
            "common": {
                "source": "AgentSSASSecurityRail",
                "event_type": "tool_input",
                "event_class": "lifecycle",
                "timestamp": 1715000000.0,
                "interaction_seq": 0,
                "session_id": "session-001",
                "conversation_id": "session-001",
                "agent_id": "deep-agent-1",
                "trace_id": "trace-001",
                "context_id": "ctx-001",
                "llm_call_seq": 0,
                "tool_call_seq": 0,
                "subsession_id": "",
                "tool_call_id": "call_abc123",
            },
            "payload": {
                "content": {"tool_args": {"command": "ls -la"}},
                "tool_name": "bash",
                "tool_call_id": "call_abc123",
            },
            "metadata": {},
        }
        assessment = await backend.report_event(raw_event)
        assert assessment is not None
        assert assessment.risk_level == RiskLevel.SAFE

    @pytest.mark.asyncio
    async def test_report_event_security_event_high_risk(self, backend):
        """安全检测事件(notify 模式下返回 SAFE)。"""
        raw_event = {
            "common": {
                "source": "AgentSSASSecurityRail",
                "event_type": "permission_interrupt_tool",
                "event_class": "security",
                "timestamp": 1715000005.0,
                "interaction_seq": 0,
                "session_id": "session-001",
                "conversation_id": "session-001",
                "agent_id": "deep-agent-1",
                "trace_id": "trace-001",
                "context_id": "ctx-001",
                "llm_call_seq": 0,
                "tool_call_seq": 0,
                "subsession_id": "",
                "tool_call_id": "call_abc123",
            },
            "payload": {
                "content": {"tool_args": {"command": "rm -rf /"}},
                "tool_name": "bash",
                "tool_call_id": "call_abc123",
                "risk_source": "PermissionInterruptRail",
                "risk_type": "tool_permission_denied",
                "risk_level": "high",
                "decision": "reject",
                "evidence": {"reason": "PermissionInterruptRail denied"},
            },
            "metadata": {},
        }
        assessment = await backend.report_event(raw_event)
        assert assessment.risk_level == RiskLevel.SAFE
        assert assessment.has_risk is False

    @pytest.mark.asyncio
    async def test_fail_open_on_connection_error(self, tmp_path):
        """连接失败时 fail-open 返回无风险。"""
        config = AgentSSASConfig(
            ssas_home=str(tmp_path),
            http_endpoint="http://localhost:1",  # 不存在的端口
            http_timeout=1.0,
        )
        backend = AgentSSASRemoteBackend(config)
        assessment = await backend.report_event({"common": {}, "payload": {}, "metadata": {}})
        assert assessment.risk_level == RiskLevel.SAFE
        assert assessment.has_risk is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_fail_open_on_timeout(self, tmp_path):
        """超时时 fail-open 返回无风险。"""
        config = AgentSSASConfig(
            ssas_home=str(tmp_path),
            http_endpoint="http://localhost:1",
            http_timeout=0.01,  # 极短超时
        )
        backend = AgentSSASRemoteBackend(config)
        assessment = await backend.report_event({"common": {}, "payload": {}, "metadata": {}})
        assert assessment.risk_level == RiskLevel.SAFE
        await backend.close()

    @pytest.mark.asyncio
    async def test_initialize_is_noop(self, backend):
        """HTTP 模式客户端 initialize 为空实现。"""
        await backend.initialize()  # 不应抛异常

    @pytest.mark.asyncio
    async def test_assessment_serialization_roundtrip(self, backend):
        """RiskAssessment 序列化/反序列化往返。"""
        from agent_ssas.core.framework.core_types.assessment import RiskAssessment

        original = RiskAssessment(
            has_risk=True,
            risk_level=RiskLevel.HIGH,
            risk_type="test_threat",
            risk_score=0.85,
            confidence=0.9,
            detected_threats=["threat1"],
            recommended_actions=["log", "alert"],
        )
        d = original.to_dict()
        restored = RiskAssessment.from_dict(d)
        assert restored.has_risk == original.has_risk
        assert restored.risk_level == original.risk_level
        assert restored.risk_type == original.risk_type
        assert restored.risk_score == original.risk_score
        assert restored.confidence == original.confidence
        assert restored.detected_threats == original.detected_threats
