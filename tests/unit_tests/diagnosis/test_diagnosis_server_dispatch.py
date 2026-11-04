# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for how the AgentServer hands ``diagnosis.*`` requests to the diagnosis host."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.diagnosis.host import DiagnosisHost


class _Host:
    def __init__(self, response: AgentResponse | Exception) -> None:
        self._response = response
        self.requests: list[AgentRequest] = []

    async def handle(self, request: AgentRequest) -> AgentResponse:
        self.requests.append(request)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _server(host: Any) -> AgentWebSocketServer:
    server = object.__new__(AgentWebSocketServer)
    server._diagnosis_host = host
    return server


def _request(method: ReqMethod = ReqMethod.DIAGNOSIS_LIST) -> AgentRequest:
    return AgentRequest(
        request_id="r-1",
        channel_id="web",
        session_id="session-1",
        req_method=method,
        params={"session_id": "session-1"},
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    async def send_wire_payload(ws: Any, wire: dict[str, Any]) -> bool:
        frames.append(wire)
        return True

    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send_wire_payload)
    return frames


def _encoded(response: AgentResponse) -> dict[str, Any]:
    return agent_ws_server.encode_agent_response_for_wire(response, response_id="r-1")


def _shape(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the timestamps that differ between two encodings of the same response."""
    return [
        {key: value for key, value in frame.items() if key not in {"timestamp", "provenance"}}
        for frame in frames
    ]


def test_every_diagnosis_method_is_dispatched_to_the_host() -> None:
    declared = {method for method in ReqMethod if method.value.startswith("diagnosis.")}

    assert agent_ws_server._DIAGNOSIS_REQ_METHODS == declared


@pytest.mark.asyncio
async def test_host_response_is_written_to_the_socket(sent: list[dict[str, Any]]) -> None:
    response = AgentResponse(request_id="r-1", channel_id="web", ok=True, payload={"items": []})
    host = _Host(response)

    await _server(host)._handle_diagnosis_request(object(), _request(), asyncio.Lock())

    assert [item.req_method for item in host.requests] == [ReqMethod.DIAGNOSIS_LIST]
    assert _shape(sent) == _shape([_encoded(response)])


@pytest.mark.asyncio
async def test_unexpected_host_failure_answers_internal_error(sent: list[dict[str, Any]]) -> None:
    host = _Host(RuntimeError("boom"))

    await _server(host)._handle_diagnosis_request(object(), _request(), asyncio.Lock())

    expected = AgentResponse(
        request_id="r-1",
        channel_id="web",
        ok=False,
        payload={"error": "boom", "code": "INTERNAL_ERROR"},
    )
    assert _shape(sent) == _shape([_encoded(expected)])


def test_the_host_is_built_lazily_once_and_rebuilt_after_it_is_released() -> None:
    server = object.__new__(AgentWebSocketServer)
    server._diagnosis_host = None
    server._agent_manager = object()

    first = server._get_diagnosis_host()
    second = server._get_diagnosis_host()
    server._diagnosis_host = None
    third = server._get_diagnosis_host()

    assert isinstance(first, DiagnosisHost)
    assert first is second
    assert third is not first
