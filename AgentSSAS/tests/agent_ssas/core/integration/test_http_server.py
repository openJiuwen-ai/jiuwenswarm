# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# tests/agent_ssas/core/integration/test_http_server.py
"""FastAPI HTTP 服务端端点测试。

使用 httpx.AsyncClient + ASGI transport 测试 FastAPI,不启动真实端口。
"""

import httpx
import pytest
from httpx import ASGITransport

from agent_ssas.core.framework.access_adapter.http_server import create_app
from agent_ssas.core.framework.config.settings import AgentSSASConfig


@pytest.fixture
def ssas_config(tmp_path):
    """测试用 AgentSSASConfig,指向临时目录。"""
    return AgentSSASConfig(ssas_home=str(tmp_path))


@pytest.fixture
def app(ssas_config):
    """创建 FastAPI 应用。"""
    return create_app(ssas_config)


@pytest.fixture
async def client(app):
    """创建 httpx 异步客户端,通过 ASGI transport 直连 FastAPI。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.integration
@pytest.mark.level1
class TestHttpServer:
    """FastAPI 端点测试。"""

    @pytest.mark.asyncio
    async def test_post_events_returns_assessment(self, client):
        """POST /api/v1/events 正常请求返回 RiskAssessment。"""
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
        resp = await client.post("/api/v1/events", json={"raw_event": raw_event})
        assert resp.status_code == 200
        data = resp.json()
        assert "assessment" in data
        assessment = data["assessment"]
        assert "risk_level" in assessment
        assert "has_risk" in assessment

    @pytest.mark.asyncio
    async def test_post_events_empty_raw_event(self, client):
        """空 raw_event 请求时 fail-open 返回无风险。"""
        resp = await client.post(
            "/api/v1/events", json={"raw_event": {}}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assessment"]["risk_level"] == "safe"
        assert data["assessment"]["has_risk"] is False

    @pytest.mark.asyncio
    async def test_post_events_security_event_high_risk(self, client):
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
        resp = await client.post("/api/v1/events", json={"raw_event": raw_event})
        assert resp.status_code == 200
        assessment = resp.json()["assessment"]
        assert assessment["risk_level"] == "safe"
        assert assessment["has_risk"] is False

    @pytest.mark.asyncio
    async def test_health_check(self, client):
        """健康检查端点。"""
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def _make_raw_event_payload() -> dict:
    """构造最小合法 raw_event 请求体。"""
    return {
        "raw_event": {
            "common": {
                "source": "AgentSSASSecurityRail",
                "event_type": "invoke_start",
                "event_class": "lifecycle",
                "timestamp": 1715000000.0,
                "interaction_seq": 0,
                "session_id": "session-auth",
                "conversation_id": "session-auth",
                "agent_id": "deep-agent-1",
                "trace_id": "trace-auth",
                "context_id": "ctx-auth",
                "llm_call_seq": -1,
                "tool_call_seq": -1,
                "subsession_id": "",
                "tool_call_id": "",
            },
            "payload": {},
            "metadata": {},
        }
    }


@pytest.mark.integration
@pytest.mark.level1
class TestTokenAuth:
    """Bearer token 认证中间件测试(TokenAuthMiddleware)。

    补齐 issue 测试承诺"覆盖维度-安全"中缺失的 token 认证 401 测试。
    """

    @pytest.mark.asyncio
    async def test_no_token_configured_allows_requests(self, tmp_path):
        """未配置 http_token 时请求正常放行(不启用认证)。"""
        config = AgentSSASConfig(ssas_home=str(tmp_path))
        assert config.http_token is None
        app = create_app(config)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/events", json=_make_raw_event_payload())
            assert resp.status_code == 200
            assert "assessment" in resp.json()

    @pytest.mark.asyncio
    async def test_token_configured_missing_header_returns_401(self, tmp_path):
        """配置 token 后无 Authorization 头返回 401。"""
        config = AgentSSASConfig(ssas_home=str(tmp_path), http_token="secret-token")
        app = create_app(config)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/events", json=_make_raw_event_payload())
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Unauthorized"}

    @pytest.mark.asyncio
    async def test_token_configured_wrong_token_returns_401(self, tmp_path):
        """配置 token 后错误 token 返回 401。"""
        config = AgentSSASConfig(ssas_home=str(tmp_path), http_token="secret-token")
        app = create_app(config)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/events",
                json=_make_raw_event_payload(),
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Unauthorized"}

    @pytest.mark.asyncio
    async def test_token_configured_correct_token_returns_200(self, tmp_path):
        """配置 token 后正确 token 请求返回 200。"""
        config = AgentSSASConfig(ssas_home=str(tmp_path), http_token="secret-token")
        app = create_app(config)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/events",
                json=_make_raw_event_payload(),
                headers={"Authorization": "Bearer secret-token"},
            )
            assert resp.status_code == 200
            assert "assessment" in resp.json()

    @pytest.mark.asyncio
    async def test_health_endpoint_exempt_from_auth(self, tmp_path):
        """/health 端点免认证(token 配置后仍可直接访问)。"""
        config = AgentSSASConfig(ssas_home=str(tmp_path), http_token="secret-token")
        app = create_app(config)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}
