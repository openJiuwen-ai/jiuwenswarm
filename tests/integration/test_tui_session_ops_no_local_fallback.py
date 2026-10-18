# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""E2E: AgentServer 不可达时，TUI 会话操作不再本地兜底（PR-1R 回归）。

gateway 本地会话兜底（embedded session_ops）已删除。本文件在真实 handler
链路上验证新行为：
- agent_client 未配置 → 立即 SERVICE_UNAVAILABLE；
- agent_client 指向真实死端口（触发真实 WS 重连失败）→ SERVICE_UNAVAILABLE；
- 任何路径都不得触发本地执行——embedded session_ops 模块已删除，残留的
  本地兜底路径会抛 ModuleNotFoundError，测试通过即证明兜底不可达。
"""

from __future__ import annotations

import socket

import pytest

from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
    CliHandlersBindParams,
    register_cli_handlers,
)
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient

pytestmark = [pytest.mark.integration]


class FakeGatewayServer:
    """与 unit_tests/gateway/test_cli_channel_handlers 一致的假通道。"""

    def __init__(self) -> None:
        self.local_handlers: dict[str, dict] = {}
        self.responses: list[dict] = []

    def register_local_handler(self, path: str, method: str, handler) -> None:
        self.local_handlers.setdefault(path, {})[method] = handler

    async def send_response(
        self, ws, req_id, *, ok=True, payload=None, error=None, code=None
    ) -> None:
        self.responses.append(
            {
                "id": req_id,
                "ok": ok,
                "payload": payload or {},
                "error": error,
                "code": code,
            }
        )


def _dead_ws_port() -> int:
    """已释放的本地端口：连接必然被立即拒绝。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


SESSION_OPS = (
    ("session.rewind", {"session_id": "s-e2e", "turn_index": 2}),
    ("history.list_turns", {"session_id": "s-e2e"}),
    ("session.rewind_and_restore", {"session_id": "s-e2e", "turn_index": 2}),
    ("session.restore_files", {"session_id": "s-e2e", "turn_index": 2}),
    ("command.rewind_compact", {"session_id": "s-e2e", "turn_index": 2, "direction": "from"}),
)
_OP_IDS = [method for method, _ in SESSION_OPS]


def _build_server(agent_client) -> FakeGatewayServer:
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(channel=server, agent_client=agent_client, path="/tui")
    )
    return server


def _assert_single_unavailable_response(server: FakeGatewayServer, req_id: str) -> None:
    assert len(server.responses) == 1, f"expected exactly one response, got {server.responses}"
    resp = server.responses[0]
    assert resp["id"] == req_id
    assert resp["ok"] is False
    assert resp["code"] == "SERVICE_UNAVAILABLE", resp
    assert resp["error"], "error message must be present"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,params", SESSION_OPS, ids=_OP_IDS)
async def test_session_ops_without_agent_client_report_unavailable(method, params):
    """agent_client 未配置：所有会话操作立即返回 SERVICE_UNAVAILABLE。"""
    server = _build_server(agent_client=None)
    await server.local_handlers["/tui"][method](object(), "req-none", params, "s-e2e")
    _assert_single_unavailable_response(server, "req-none")


@pytest.mark.asyncio
@pytest.mark.parametrize("method,params", SESSION_OPS, ids=_OP_IDS)
async def test_session_ops_with_dead_agent_server_report_unavailable(method, params):
    """真实 WS 客户端指向死端口：重连失败后返回 SERVICE_UNAVAILABLE，不本地执行。"""
    client = WebSocketAgentServerClient()
    client._uri = f"ws://127.0.0.1:{_dead_ws_port()}"
    server = _build_server(agent_client=client)
    await server.local_handlers["/tui"][method](object(), "req-dead", params, "s-e2e")
    _assert_single_unavailable_response(server, "req-dead")
