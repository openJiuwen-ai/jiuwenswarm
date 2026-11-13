"""Keep rich result projections and cancelled-call audits without full-history reads."""
import asyncio
import json
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.session import history_artifacts as artifacts
from jiuwenswarm.server.runtime.session import lifecycle, session_history as history
from tests.unit_tests.test_context_snapshots import stored


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["awaiting_end", "yielding_end"])
async def test_cancelled_consumer_preserves_one_usage_record_without_yielding_after_close(
    stored, monkeypatch, boundary,
):
    from tests.unit_tests.agentserver.test_deep_adapter_stream_end_final import (
        _adapter_ready, _chat_request, _usage_chunk,
    )

    adapter = _adapter_ready(monkeypatch)
    reached = asyncio.Event()
    release = asyncio.Event()
    refs = {"prompt": artifacts.put_text(stored.sid, "ACTUAL FINAL PROMPT")}

    class Stream:
        def __aiter__(self):
            return self.generate()

        async def generate(self):
            yield SimpleNamespace(type="llm_call_start", payload={"prompt": "PREVIEW"})
            yield _usage_chunk()
            if boundary == "yielding_end":
                yield SimpleNamespace(type="llm_call_end", payload={"payload_refs": refs})
            reached.set()
            await release.wait()

        async def close(self, *, abort_active_round=False):
            return None

    adapter._instance.attach_output = AsyncMock(return_value=Stream())
    adapter._instance.send_input = AsyncMock()
    request = _chat_request()
    request.session_id = stored.sid
    events = []

    async def consume():
        async with aclosing(adapter.process_message_stream_impl(request, {"query": "QUESTION"})) as stream:
            async for chunk in stream:
                if isinstance(chunk.payload, dict):
                    events.append(chunk.payload)
                    if boundary == "yielding_end" and chunk.payload.get("event_type") == "chat.llm_call_end":
                        reached.set()
                        await release.wait()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(reached.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    history.flush_history_writes()
    rows = [row for row in history.load_history_records(stored.sid) if row.get("event_type") == "chat.usage_metadata"]
    assert len(rows) == 1
    assert rows[0]["request_id"] == request.request_id
    assert rows[0]["metadata"]["usage_metadata"]["total_tokens"] == 12
    if boundary == "yielding_end":
        assert rows[0]["metadata"]["usage_metadata"]["payload_refs"] == refs
    assert not any(event.get("event_type") == "chat.usage_metadata" for event in events)


@pytest.mark.asyncio
async def test_large_tool_result_keeps_small_rich_metadata_and_complete_original(stored):
    from tests.unit_tests.agentserver.test_stream_event_rail_wire_budget import _emit

    lifecycle.claim_runtime("wire-budget-artifact-test")
    original = {"result": "ORIGINAL " * 20000, "skill_tree": {"candidates": [{"label": "skill"}]},
                "planned_graph": {"graph": {"nodes": {"a": {"label": "A"}}, "edges": []}},
                "beam_search": {"status": "complete"}, "graph_status": "ready"}
    payload = (await _emit(original, rendered_result=original["result"]))["tool_result"]
    assert payload["raw_output"] == {key: value for key, value in original.items() if key != "result"}
    assert "ORIGINAL" in payload["rendered_result"] and len(payload["rendered_result"]) < 4096
    assert json.loads(Path(payload["payload_refs"]["raw_output"]["path"]).read_text(encoding="utf-8")) == original


def test_rich_metadata_projection_obeys_existing_raw_output_budget(stored):
    value = {"result": "x" * 2000000, "skill_tree": {"large": "x" * 2000000},
             "planned_graph": {"graph": {"nodes": {}, "edges": []}}, "graph_status": "ready"}
    projected = artifacts.externalize_payload(stored.sid, {"raw_output": value}, fields=("raw_output",))
    assert "skill_tree" not in projected["raw_output"]
    assert projected["raw_output"]["graph_status"] == "ready"
    assert len(json.dumps(projected["raw_output"]).encode()) <= artifacts.DISPLAY_METADATA_BYTES


@pytest.mark.parametrize("nested", [False, True])
def test_large_tool_result_keeps_pending_state_without_copying_data_text(stored, nested):
    states = {"status": "pending", "pending": True}
    value = {"data": {**states, "result": "x" * 100000}} if nested else {**states, "result": "x" * 100000}
    projected = artifacts.externalize_payload(stored.sid, {"raw_output": value}, fields=("raw_output",))
    assert projected["raw_output"] == ({"data": states} if nested else states)


@pytest.mark.parametrize("offset", [0, 8191])
def test_artifact_cursor_reads_exact_utf8_original_through_shared_history_path(stored, offset):
    original = "x" * 8191 + "中文🙂" * 3000
    reference = artifacts.put_text(stored.sid, original)
    cursor = f"{artifacts.ARTIFACT_CURSOR_PREFIX}{reference['sha256']}:{offset}"
    data = history.read_history_cursor_page(stored.sid, cursor=cursor, limit=1, is_restorable=lambda _row: False)
    chunk = data["messages"][0]
    assert chunk["content"] == original.encode()[offset:chunk["next_offset"]].decode()
    assert chunk["sha256"] == reference["sha256"] and len(chunk["content"].encode()) <= 8192
    from jiuwenswarm.server.control.history_service import stream_history_records

    assert stream_history_records(data["messages"], use_split=True) == data["messages"]


@pytest.mark.parametrize("cursor", ["history-artifact:v1:../bad:0", "history-artifact:v1:" + "a" * 64 + ":-1"])
def test_artifact_cursor_rejects_malformed_inputs_without_returning_history(stored, cursor):
    from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError

    with pytest.raises(HistoryReplayError) as error:
        history.read_history_cursor_page(stored.sid, cursor=cursor, limit=1, is_restorable=lambda _row: True)
    assert error.value.code == "HISTORY_ARTIFACT_FAILED"
