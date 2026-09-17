import asyncio
import json

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def fake_encode_agent_response_for_wire(resp, response_id):
    return {
        "response_id": response_id,
        "payload": resp.payload,
        "ok": resp.ok,
    }


def make_large_tool_result_records(count: int = 20) -> list[dict]:
    large_result = "x" * 20_000
    return [
        {
            "id": f"tool-result-{idx}",
            "role": "teammate",
            "member_name": "agent-1",
            "event_type": "chat.tool_result",
            "mode": "team",
            "timestamp": float(idx),
            "content": large_result,
            "tool_result": {
                "tool_name": "edit_file",
                "result": large_result,
            },
        }
        for idx in range(count)
    ]


@pytest.fixture(autouse=True)
def patch_wire_encoder(monkeypatch):
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )


@pytest.mark.asyncio
async def test_team_history_get_paginates_and_bounds_large_records(monkeypatch):
    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    ws = FakeWebSocket()
    records = make_large_tool_result_records()

    monkeypatch.setattr(
        agent_ws_server_module,
        "read_team_history_records",
        lambda session_id: records,
    )

    request = AgentRequest(
        request_id="req-team-history",
        channel_id="web",
        req_method=ReqMethod.TEAM_HISTORY_GET,
        params={"session_id": "sess-large", "limit": 20, "max_bytes": 4096},
    )

    await getattr(server, "_handle_team_history_get")(ws, request, asyncio.Lock())

    assert len(ws.sent) == 1
    frame = ws.sent[0]
    payload = frame["payload"]
    encoded_size = len(json.dumps(frame, ensure_ascii=False).encode("utf-8"))
    assert encoded_size <= 4096
    assert payload["session_id"] == "sess-large"
    assert payload["records"]
    assert len(payload["records"]) < len(records)
    assert payload["has_more"] is True
    assert payload["next_cursor"] == len(payload["records"])
    assert payload["records"][0]["content"].endswith("[truncated]")


@pytest.mark.asyncio
async def test_team_history_get_cursor_continues_next_page(monkeypatch):
    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    records = make_large_tool_result_records()

    monkeypatch.setattr(
        agent_ws_server_module,
        "read_team_history_records",
        lambda session_id: records,
    )

    first_ws = FakeWebSocket()
    first_request = AgentRequest(
        request_id="req-team-history-first",
        channel_id="web",
        req_method=ReqMethod.TEAM_HISTORY_GET,
        params={"session_id": "sess-large", "limit": 20, "max_bytes": 4096},
    )
    await getattr(server, "_handle_team_history_get")(first_ws, first_request, asyncio.Lock())
    first_payload = first_ws.sent[0]["payload"]

    second_ws = FakeWebSocket()
    second_request = AgentRequest(
        request_id="req-team-history-second",
        channel_id="web",
        req_method=ReqMethod.TEAM_HISTORY_GET,
        params={
            "session_id": "sess-large",
            "cursor": first_payload["next_cursor"],
            "limit": 20,
            "max_bytes": 4096,
        },
    )
    await getattr(server, "_handle_team_history_get")(second_ws, second_request, asyncio.Lock())
    second_payload = second_ws.sent[0]["payload"]

    assert first_payload["has_more"] is True
    assert second_payload["cursor"] == first_payload["next_cursor"]
    assert second_payload["records"]
    assert second_payload["records"][0]["id"] == records[first_payload["next_cursor"]]["id"]
    assert second_payload["next_cursor"] > second_payload["cursor"]
    assert len(json.dumps(second_ws.sent[0], ensure_ascii=False).encode("utf-8")) <= 4096


def test_history_get_sanitizes_large_restorable_records(monkeypatch):
    """chat.tool_result 不在切片白名单里：仍要走 string-truncate 才发到 wire。
    新逻辑下 sanitize 不在 get_conversation_history 里做（移到 split_history_record_for_stream
    里），所以这里直接调 split_history_record_for_stream 验最终 wire record 有界、
    content 被截断。"""
    large_record = {
        "id": "tool-result-large",
        "role": "assistant",
        "event_type": "chat.tool_result",
        "content": "x" * 100_000,
        "tool_result": {
            "tool_name": "edit_file",
            "result": "x" * 100_000,
        },
    }

    monkeypatch.setattr(agent_ws_server_module, "history_exists", lambda session_id: True)
    monkeypatch.setattr(
        agent_ws_server_module,
        "load_history_records",
        lambda session_id: [large_record],
    )

    # get_conversation_history 现在返回 raw record（不在内部 sanitize）——
    # sanitize 移到 _handle_history_get_stream 里调 split 时做。这里先验 raw 透传。
    result = agent_ws_server_module.AgentWebSocketServer.get_conversation_history(
        "sess-large",
        1,
    )

    assert result is not None
    raw_message = result["messages"][0]
    # raw 状态——还没被 string-truncate
    assert raw_message["content"] == "x" * 100_000

    # 真正的 sanitize 在 split_history_record_for_stream 里：chat.tool_result 仍单帧，
    # content 字符串被截断到 16KB + [truncated]。
    from jiuwenswarm.server.wire_truncate import split_history_record_for_stream

    chunks = split_history_record_for_stream(raw_message)
    assert len(chunks) == 1
    message = chunks[0]
    assert message["content"].endswith("[truncated]")
    assert message["tool_result"]["result"].endswith("[truncated]")
    from jiuwenswarm.server import wire_truncate as wire_truncate_module

    assert (
        len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        <= getattr(wire_truncate_module, "_HISTORY_WIRE_RECORD_MAX_BYTES")
    )


@pytest.mark.asyncio
async def test_team_history_get_preserves_too_large_first_record_as_placeholder(monkeypatch):
    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    ws = FakeWebSocket()
    huge_id = "tool-result-too-large-" + ("x" * 10_000)
    records = [
        {
            "id": huge_id,
            "role": "teammate",
            "member_name": "agent-1",
            "event_type": "chat.tool_result",
            "mode": "team",
            "timestamp": 1.0,
            "content": "x" * 100_000,
            "tool_result": {
                "tool_name": "edit_file",
                "result": "x" * 100_000,
            },
        }
    ]

    monkeypatch.setattr(
        agent_ws_server_module,
        "read_team_history_records",
        lambda session_id: records,
    )

    request = AgentRequest(
        request_id="req-team-history-placeholder",
        channel_id="web",
        req_method=ReqMethod.TEAM_HISTORY_GET,
        params={"session_id": "sess-large", "limit": 20, "max_bytes": 2048},
    )

    await getattr(server, "_handle_team_history_get")(ws, request, asyncio.Lock())

    payload = ws.sent[0]["payload"]
    encoded_size = len(json.dumps(ws.sent[0], ensure_ascii=False).encode("utf-8"))
    assert encoded_size <= 2048
    assert len(payload["records"]) == 1
    assert payload["next_cursor"] == 1
    assert payload["has_more"] is False
    assert payload["records"][0]["truncated"] is True
    assert payload["records"][0]["id"].startswith("tool-result-too-large-")
    assert payload["records"][0]["event_type"] == "chat.tool_result"
