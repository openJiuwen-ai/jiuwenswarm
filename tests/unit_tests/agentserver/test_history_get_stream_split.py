# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""_handle_history_get_stream + split_history_record_for_stream 端到端集成测试（精简版）。

构造一条混合 history：200KB chat.final + chat.reasoning + chat.tool_call，
跑 web 通道的 history.get 流，断言：
- chat.final 切成 N>=2 片，_part 元数据正确，拼回原文
- chat.reasoning / chat.tool_call 单帧、不切片（走旧 sanitize）
- 每片 ≤ 单条 record wire 预算（64KB）

"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.wire_truncate import _HISTORY_WIRE_RECORD_MAX_BYTES


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def fake_encode_agent_chunk_for_wire(chunk: Any, *, response_id: str, sequence: int) -> dict:
    return {
        "request_id": chunk.request_id,
        "channel_id": chunk.channel_id,
        "response_id": response_id,
        "sequence": sequence,
        "payload": chunk.payload,
        "is_complete": chunk.is_complete,
    }


@pytest.fixture(autouse=True)
def patch_wire_encoder(monkeypatch):
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_chunk_for_wire",
        fake_encode_agent_chunk_for_wire,
    )


def _make_history_records() -> list[dict]:
    """混合 history：超大 chat.final + reasoning + tool_call"""
    return [
        {
            "id": "rsn-1",
            "role": "assistant",
            "event_type": "chat.reasoning",
            "timestamp": 1.0,
            "content": "thinking " * 20_000,  # ~180KB → string-truncated，不切片
        },
        {
            "id": "tc-1",
            "role": "assistant",
            "event_type": "chat.tool_call",
            "timestamp": 2.0,
            "content": "tool_call_payload",
            "tool_call": {"id": "tc-1", "name": "edit_file", "arguments": {"path": "/x/y"}},
        },
        {
            "id": "msg-final-large",
            "role": "assistant",
            "event_type": "chat.final",
            "timestamp": 3.0,
            "content": "α" * 200_000,  # 400KB → 必须切片
        },
    ]


def _patch_history(monkeypatch, records: list[dict]) -> None:
    monkeypatch.setattr(agent_ws_server_module, "history_exists", lambda session_id: True)
    monkeypatch.setattr(
        agent_ws_server_module,
        "load_history_records",
        lambda session_id: records,
    )


@pytest.mark.asyncio
async def test_web_channel_splits_chat_final_and_leaves_others_unsplit(monkeypatch):
    """web 通道：chat.final 切片，reasoning / tool_call 单帧不切"""
    _patch_history(monkeypatch, _make_history_records())
    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    ws = FakeWebSocket()
    request = AgentRequest(
        request_id="req-web-split",
        channel_id="web",
        req_method=ReqMethod.HISTORY_GET,
        params={"session_id": "sess-web", "page_idx": 1},
    )

    await server._handle_history_get_stream(ws, request, asyncio.Lock())

    record_frames = [
        f for f in ws.sent
        if f["payload"].get("event_type") == "history.message"
        and isinstance(f["payload"].get("message"), dict)
    ]
    by_event_type: dict[str, list[dict]] = {}
    for f in record_frames:
        msg = f["payload"]["message"]
        by_event_type.setdefault(msg.get("event_type", ""), []).append(f)

    # chat.reasoning：单帧、无 _part、content 字符串截断
    rsn = by_event_type["chat.reasoning"]
    assert len(rsn) == 1
    assert "_part" not in rsn[0]["payload"]["message"]
    assert rsn[0]["payload"]["message"]["content"].endswith("[truncated]")

    # chat.tool_call：单帧、无 _part
    tc = by_event_type["chat.tool_call"]
    assert len(tc) == 1
    assert "_part" not in tc[0]["payload"]["message"]

    # chat.final：多片、有 _part、每片 ≤64KB、拼回原文
    final = by_event_type["chat.final"]
    assert len(final) > 1
    for i, f in enumerate(final):
        msg = f["payload"]["message"]
        part = msg["_part"]
        assert part["record_id"] == "msg-final-large"
        assert part["total_parts"] == len(final)
        assert part["part_idx"] == i
        assert len(json.dumps(msg, ensure_ascii=False).encode("utf-8")) <= _HISTORY_WIRE_RECORD_MAX_BYTES

    final.sort(key=lambda f: f["payload"]["message"]["_part"]["part_idx"])
    reassembled = "".join(f["payload"]["message"]["content"] for f in final)
    assert reassembled.encode("utf-8") == ("α" * 200_000).encode("utf-8")