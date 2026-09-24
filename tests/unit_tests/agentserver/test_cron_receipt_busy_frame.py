"""cron 回执不得冒充轮终止帧 + attach None 不得吞消息（0922 CRON-005 定位修复）。

两条防线各对应一个洞：
- 修 1：cron 轮内受理回执显式 is_final=False（编码）/ cron kind 解码 is_complete
  跟随 wire is_final —— 回执不再提前收轮；
- 修 2：普通消息 attach_output 为 None 时返回 chat.error code=busy 终止帧
  （消息未投递即不得谎报成功；帧形状经信封映射仍为合法 is_final 终止帧）。
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import (
    CronToolRoute,
    CronTools,
)
from jiuwenswarm.common.e2a.constants import E2A_RESPONSE_KIND_CRON
from jiuwenswarm.common.e2a.gateway_normalize import (
    e2a_response_from_agent_chunk,
    e2a_response_to_agent_chunk,
)
from jiuwenswarm.common.e2a.models import E2AResponse
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire


def _cron_wire(is_final: bool) -> E2AResponse:
    return E2AResponse(
        request_id="req-1",
        response_kind=E2A_RESPONSE_KIND_CRON,
        is_final=is_final,
        status="succeeded" if is_final else "in_progress",
        body={"action": "add", "status": "ok", "data": {"job_id": "j1"}, "message": ""},
    )


class TestCronReceiptIsNotTerminal:
    def test_send_split_payload_declares_non_final(self, monkeypatch):
        captured: dict = {}

        async def _fake_send_push(payload):
            captured.update(payload)

        tools = object.__new__(CronTools)
        tools._gateway_push = type("P", (), {"send_push": staticmethod(_fake_send_push)})()
        token = CronTools.push_cron_route(
            CronToolRoute(request_id="req-1", channel_id="desktop", session_id="s1")
        )
        try:
            result = asyncio.run(tools._send_split("add", {"job_id": "j1"}))
        finally:
            CronTools.reset_cron_route(token)
        assert result["status"] == "forwarded"
        assert captured["response_kind"] == E2A_RESPONSE_KIND_CRON
        assert captured["request_id"] == "req-1"
        assert captured.get("is_final") is False

    def test_push_wire_respects_explicit_is_final_false(self):
        wire = build_server_push_wire(
            {
                "request_id": "req-1",
                "channel_id": "desktop",
                "response_kind": E2A_RESPONSE_KIND_CRON,
                "is_final": False,
                "body": {"action": "add", "status": "ok", "data": {}, "message": ""},
            }
        )
        assert wire["is_final"] is False

    def test_in_round_receipt_decodes_to_non_terminal_chunk(self):
        chunk = e2a_response_to_agent_chunk(_cron_wire(is_final=False))
        assert chunk.payload["event_type"] == "cron.response"
        assert chunk.is_complete is False

    def test_background_cron_completion_remains_terminal(self):
        chunk = e2a_response_to_agent_chunk(_cron_wire(is_final=True))
        assert chunk.is_complete is True


class TestBusyTerminalFrameShape:
    def _busy_chunk(self) -> AgentResponseChunk:
        return AgentResponseChunk(
            request_id="req-2",
            channel_id="desktop",
            payload={
                "event_type": "chat.error",
                "code": "busy",
                "error": "上一轮仍在执行，请稍后重试",
            },
            is_complete=True,
        )

    def test_busy_yields_envelope_maps_to_final_failed_error(self):
        wire = e2a_response_from_agent_chunk(
            self._busy_chunk(), response_id="resp-2", sequence=7, is_stream=True
        )
        assert wire.is_final is True
        assert wire.status == "failed"
        assert wire.response_kind == "e2a.error"

    def test_busy_code_survives_decode_roundtrip(self):
        wire = e2a_response_from_agent_chunk(
            self._busy_chunk(), response_id="resp-2", sequence=7, is_stream=True
        )
        assert wire.body["details"]["code"] == "busy"
