# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import json
from typing import Any

import pytest

from jiuwenswarm.common.e2a.constants import E2A_RESPONSE_STATUS_SUCCEEDED
from jiuwenswarm.server.ws_send import send_wire_payload

#: 路径参数占位值：路由矩阵用它填充 ``{session_id}`` 等段。
PATH_PARAM_SAMPLES: dict[str, str] = {
    "session_id": "sess_probe",
    "name": "probe_name",
    "team_name": "probe_team",
    "task_id": "task_probe",
    "issue_id": "issue_probe",
    "rule_id": "rule_probe",
    "override_id": "ovr_probe",
    "method": "session.list",
    "id": "hosting_probe",
}


class StubWSServer:
    def __init__(self) -> None:
        self.envelopes: list[dict[str, Any]] = []

    async def _reply(self, ws: Any, request_id: Any) -> None:
        await send_wire_payload(
            ws,
            {
                "request_id": request_id,
                "status": E2A_RESPONSE_STATUS_SUCCEEDED,
                "body": {"probe": True},
                "metadata": {},
            },
        )

    async def _handle_message(self, ws: Any, raw: str | bytes, send_lock: Any) -> None:
        envelope = json.loads(raw)
        self.envelopes.append(envelope)
        await self._reply(ws, envelope.get("request_id"))

    async def record_request(self, ctx: Any, request: Any) -> None:
        method = getattr(request, "req_method", None)
        self.envelopes.append(
            {
                "request_id": getattr(request, "request_id", None),
                "session_id": getattr(request, "session_id", None),
                "channel": getattr(request, "channel_id", None),
                "method": getattr(method, "value", method),
                "params": getattr(request, "params", None) or {},
                "metadata": getattr(request, "metadata", None) or {},
                "is_stream": bool(getattr(request, "is_stream", False)),
            }
        )
        await ctx.sink.send_wire(
            {
                "request_id": getattr(request, "request_id", None),
                "status": E2A_RESPONSE_STATUS_SUCCEEDED,
                "body": {"probe": True},
                "metadata": {},
            }
        )

    @property
    def last(self) -> dict[str, Any]:
        assert self.envelopes, "桩未收到任何请求"
        return self.envelopes[-1]


@pytest.fixture()
def stub_server() -> StubWSServer:
    return StubWSServer()


@pytest.fixture(autouse=True)
def stub_pipeline(monkeypatch, stub_server: StubWSServer):
    from jiuwenswarm.server import pipeline

    async def _stub(ctx: Any, request: Any, *, peer: Any = None) -> None:
        await stub_server.record_request(ctx, request)

    monkeypatch.setattr(pipeline, "dispatch_parsed_request", _stub)
    return stub_server


@pytest.fixture()
def http_server(stub_server: StubWSServer):
    from jiuwenswarm.server.agent_http_server import AgentHTTPServer

    return AgentHTTPServer(stub_server)


@pytest.fixture()
def client(http_server):
    from fastapi.testclient import TestClient

    return TestClient(http_server.build_app())


def fill_path(path: str) -> str:
    out = path
    for key, value in PATH_PARAM_SAMPLES.items():
        out = out.replace("{" + key + "}", value)
    assert "{" not in out, f"路径含未覆盖的参数占位符: {path}（请补 PATH_PARAM_SAMPLES）"
    return out
