# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for examples/extensions/outbound_stream_mapper."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_EXTENSION_PATH = (
    Path(__file__).resolve().parents[3]
    / "examples"
    / "extensions"
    / "outbound_stream_mapper"
    / "extension.py"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "outbound_stream_mapper_under_test", _EXTENSION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ext = _load()


def _ctx(request_id: str = "r1", session_id: str = "s1"):
    return ext.StreamMapContext(
        transport="ws",
        channel_id="my-agui-client",
        request_id=request_id,
        session_id=session_id,
        connection_id="c1",
        request=None,
    )


def _wire(
    event_type: str,
    *,
    is_final: bool = False,
    body: dict | None = None,
    status: str = "in_progress",
) -> dict:
    payload = {"event_type": event_type} if event_type else {}
    payload.update(body or {})
    return {
        "protocol_version": "1.0",
        "request_id": "r1",
        "sequence": 0,
        "is_final": is_final,
        "status": status,
        "response_kind": "e2a.complete" if is_final else "e2a.chunk",
        "body": payload,
    }


def _types(frames) -> list[str]:
    return [frame["data"]["type"] for frame in frames]


def _new():
    extension = ext.AgUiStreamExtension()
    extension.on_open(_ctx())
    return extension


def test_delta_opens_run_and_streams_text() -> None:
    extension = _new()
    first = extension.map_frame(_wire("chat.delta", body={"delta": "你"}), _ctx())
    assert _types(first) == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
    ]
    assert first[0]["data"]["runId"] == "r1"
    assert first[-1]["data"]["delta"] == "你"

    second = extension.map_frame(_wire("chat.delta", body={"delta": "好"}), _ctx())
    assert _types(second) == ["TEXT_MESSAGE_CONTENT"]


def test_final_closes_text_and_run() -> None:
    extension = _new()
    extension.map_frame(_wire("chat.delta", body={"delta": "hi"}), _ctx())
    frames = extension.map_frame(_wire("chat.final", is_final=True), _ctx())
    assert _types(frames) == ["TEXT_MESSAGE_END", "RUN_FINISHED"]
    assert frames[-1]["data"]["runId"] == "r1"


def test_tool_call_and_result() -> None:
    extension = _new()
    frames = extension.map_frame(
        _wire(
            "chat.tool_call",
            body={
                "tool_call": {
                    "id": "call-1",
                    "name": "web_search",
                    "arguments": {"q": "jiuwen"},
                }
            },
        ),
        _ctx(),
    )
    assert _types(frames) == ["RUN_STARTED", "TOOL_CALL_START", "TOOL_CALL_ARGS"]
    assert frames[1]["data"]["toolCallId"] == "call-1"
    assert frames[1]["data"]["toolCallName"] == "web_search"
    assert json.loads(frames[2]["data"]["delta"]) == {"q": "jiuwen"}

    result = extension.map_frame(
        _wire(
            "chat.tool_result",
            body={"tool_result": {"tool_call_id": "call-1", "result": {"ok": True}}},
        ),
        _ctx(),
    )
    assert _types(result) == ["TOOL_CALL_END", "TOOL_CALL_RESULT"]
    assert result[0]["data"]["toolCallId"] == "call-1"
    assert json.loads(result[1]["data"]["content"]) == {"ok": True}


def test_error_frame_maps_to_run_error() -> None:
    extension = _new()
    frames = extension.map_frame(
        _wire("chat.error", is_final=True, status="failed", body={"error": "boom"}),
        _ctx(),
    )
    assert frames[-1]["data"]["type"] == "RUN_ERROR"
    assert frames[-1]["data"]["message"] == "boom"


def test_unmapped_event_is_dropped_without_starting_run() -> None:
    extension = _new()
    frames = extension.map_frame(_wire("chat.processing_status"), _ctx())
    assert frames == []


def test_on_error_returns_run_error_and_cleans_state() -> None:
    extension = _new()
    frames = extension.on_error(_ctx(), RuntimeError("mapper failed"))
    assert _types(frames) == ["RUN_ERROR"]
    assert frames[0]["data"]["message"] == "mapper failed"
    assert "r1" not in extension._runs


def test_on_close_cleans_state() -> None:
    extension = _new()
    assert "r1" in extension._runs
    extension.on_close(_ctx())
    assert "r1" not in extension._runs
