"""cron 回执不得冒充本轮终止帧——解码端契约（0922 CRON-005 修复·PR !7425）。

编码端 is_final=False 已由 c07679586 落地；本文件锁定解码端：
kind==cron 的 GatewayChunk.is_complete 必须跟随 wire is_final——
否则按 is_complete 收轮的客户端（用例 GatewayClient/CLI）仍会被轮内
受理回执提前收轮（0924 cron 目录五连红的直接机制）。

attach_output 为 None 的收口已由上游 d4a11a7f0（reclaim + OUTPUT_LEASE_BUSY）
覆盖，不在本文件范围。
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
