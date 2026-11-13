"""tool_result events must fit the AgentServer WebSocket send budget (issue #9834)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_gateway.common.ws_limits import AGENT_WS_SEND_BUDGET_BYTES
from e2a.wire_codec import encode_agent_chunk_for_wire

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.server.ws_send import send_wire_payload

# An MCP tool reply larger than the 6 MiB send budget; MCPTool.invoke wraps a
# plain reply as {"result": ...}, which the rail streams as raw_output.
_LARGE_TEXT = "row " * (2 * 2**20)
_MCP_TOOL = SimpleNamespace(name="mcp__db__query", id="tc-mcp")
# Same cut as the event's result / rendered_result text fields.
_TEXT_LIMIT = 60000


class _StreamSession:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def write_stream(self, event: object) -> None:
        self.events.append(event)

    def get_session_id(self) -> str:
        return "wire-budget-artifact-test"


class _WebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


async def _emit(result: object, *, rendered_result: str | None = None) -> dict:
    session = _StreamSession()
    projected = await JiuSwarmStreamEventRail()._emit_tool_result(
        session,
        _MCP_TOOL,
        result,
        rendered_result=rendered_result,
    )
    assert projected is True
    return session.events[0].payload


def _wire_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@pytest.mark.asyncio
async def test_large_mcp_result_is_sent_as_tool_result_not_error() -> None:
    payload = await _emit({"result": _LARGE_TEXT}, rendered_result=_LARGE_TEXT)
    wire = encode_agent_chunk_for_wire(
        AgentResponseChunk(
            request_id="req-1",
            channel_id="web",
            payload={"event_type": "chat.tool_result", **payload},
        ),
        response_id="req-1",
        sequence=1,
    )
    ws = _WebSocket()

    sent_as_is = await send_wire_payload(ws, wire)

    assert sent_as_is is True
    frame = ws.sent[0]
    assert "response_too_large" not in frame
    assert '"tool_call_id": "tc-mcp"' in frame


@pytest.mark.asyncio
async def test_large_raw_output_is_cut_like_result_and_flagged() -> None:
    payload = (await _emit({"result": _LARGE_TEXT}, rendered_result=_LARGE_TEXT))["tool_result"]

    assert _wire_bytes(payload) < AGENT_WS_SEND_BUDGET_BYTES
    assert payload["raw_output"] == {}
    assert payload["raw_output_truncated"] is True
    refs = payload["payload_refs"]
    assert json.loads(Path(refs["raw_output"]["path"]).read_text(encoding="utf-8")) == {"result": _LARGE_TEXT}
    assert Path(refs["rendered_result"]["path"]).read_text(encoding="utf-8") == _LARGE_TEXT
    assert len(payload["rendered_result"]) < _TEXT_LIMIT


@pytest.mark.asyncio
async def test_mcp_app_block_is_kept_when_raw_output_is_trimmed() -> None:
    mcp_app = {
        "server": "db",
        "tool": "query",
        "resourceUri": "ui://db/table",
        "arguments": {"sql": "select 1"},
        "toolResult": {"structuredContent": {"rows": [[1, "a"], [2, "b"]]}},
    }

    payload = (await _emit({"result": _LARGE_TEXT, "mcp_app": mcp_app}))["tool_result"]

    assert payload["raw_output"]["mcp_app"] == mcp_app
    ref = payload["payload_refs"]["raw_output"]
    assert json.loads(Path(ref["path"]).read_text(encoding="utf-8"))["result"] == _LARGE_TEXT
    assert payload["raw_output_truncated"] is True


@pytest.mark.asyncio
async def test_raw_output_of_many_small_items_falls_back_to_empty() -> None:
    rows = [{"id": index, "name": "item"} for index in range(200_000)]

    payload = (await _emit({"result": rows}))["tool_result"]

    assert _wire_bytes(rows) > 2**20
    assert payload["raw_output"] == {}
    assert payload["raw_output_truncated"] is True
    assert _wire_bytes(payload) < AGENT_WS_SEND_BUDGET_BYTES
    ref = payload["payload_refs"]["raw_output"]
    assert len(json.loads(Path(ref["path"]).read_text(encoding="utf-8"))["result"]) == len(rows)


@pytest.mark.asyncio
async def test_small_raw_output_is_streamed_unchanged() -> None:
    result = {"result": "3 rows", "status": "success", "skill_tree": {"path": ["a", "b"]}}

    payload = (await _emit(result))["tool_result"]

    assert payload["raw_output"] == result
    assert "raw_output_truncated" not in payload
