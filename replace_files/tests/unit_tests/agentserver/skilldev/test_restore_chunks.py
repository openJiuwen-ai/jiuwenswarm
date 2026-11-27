from __future__ import annotations

import json
from collections.abc import Iterable

import pytest

from jiuwenclaw.agentserver.skilldev.deps import SkillDevDeps
from jiuwenclaw.agentserver.skilldev.service import SkillDevService
from jiuwenclaw.agentserver.skilldev.session_history.restore_chunks import (
    RESTORE_CHUNK_EVENT_TYPE,
    RESTORE_RESPONSE_TOO_LARGE_CODE,
    RestoreChunkDecodeError,
    decode_restore_payload_from_stream,
    decode_restore_payload_from_stream_with_retry,
    encode_restore_payload_chunks,
    is_retriable_restore_decode_error,
)
from jiuwenclaw.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenclaw.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenclaw.schema.message import ReqMethod


async def _aiter(chunks: Iterable[AgentResponseChunk]):
    for chunk in chunks:
        yield chunk


def _restore_payload(*, text: str = "hello") -> dict:
    return {
        "ok": True,
        "task_id": "task-1",
        "snapshot": {
            "task_id": "task-1",
            "stage": "idle",
            "is_suspended": False,
            "is_processing": False,
            "todos": [],
            "artifacts": [],
            "query": "q1",
        },
        "timeline_items": [
            {
                "seq": 1,
                "timestamp": "2026-06-07T00:00:00Z",
                "source": "assistant",
                "event_type": "skilldev.agent_output",
                "payload": {"delta": text},
            }
        ],
        "version": "2",
        "runner": "agent",
    }


@pytest.mark.asyncio
async def test_restore_chunks_roundtrip_single_chunk():
    payload = _restore_payload()
    chunks = list(
        encode_restore_payload_chunks(
            payload,
            request_id="restore-1",
            channel_id="web",
            task_id="task-1",
        )
    )

    assert len(chunks) == 1
    assert chunks[0].payload["event_type"] == RESTORE_CHUNK_EVENT_TYPE
    restored = await decode_restore_payload_from_stream(
        _aiter([*chunks, AgentResponseChunk("restore-1", "web", {"is_complete": True}, True)])
    )
    assert restored == payload


@pytest.mark.asyncio
async def test_restore_chunks_roundtrip_large_single_timeline_item_and_wire_size():
    payload = _restore_payload(text="x" * 90_000)
    chunks = list(
        encode_restore_payload_chunks(
            payload,
            request_id="restore-large",
            channel_id="web",
            task_id="task-1",
        )
    )

    assert len(chunks) > 1
    for index, chunk in enumerate(chunks):
        assert chunk.payload["index"] == index
        wire = encode_agent_chunk_for_wire(
            chunk,
            response_id="restore-large",
            sequence=index,
        )
        assert len(json.dumps(wire, ensure_ascii=False).encode("utf-8")) < 60_000

    restored = await decode_restore_payload_from_stream(
        _aiter([*chunks, AgentResponseChunk("restore-large", "web", {"is_complete": True}, True)])
    )
    assert restored == payload


@pytest.mark.asyncio
async def test_restore_chunks_tolerate_terminal_before_last_data_chunk():
    payload = _restore_payload(text="x" * 50_000)
    chunks = list(
        encode_restore_payload_chunks(
            payload,
            request_id="restore-reorder",
            channel_id="web",
            task_id="task-1",
        )
    )
    terminal = AgentResponseChunk(
        "restore-reorder",
        "web",
        {"is_complete": True},
        True,
    )
    reordered = [*chunks[:-1], terminal, chunks[-1]]

    restored = await decode_restore_payload_from_stream(_aiter(reordered))
    assert restored == payload


@pytest.mark.asyncio
async def test_restore_chunks_reject_missing_chunk():
    payload = _restore_payload(text="x" * 50_000)
    chunks = list(
        encode_restore_payload_chunks(
            payload,
            request_id="restore-missing",
            channel_id="web",
            task_id="task-1",
        )
    )

    with pytest.raises(RestoreChunkDecodeError, match="missing chunks"):
        await decode_restore_payload_from_stream(
            _aiter([chunks[0], AgentResponseChunk("restore-missing", "web", {"is_complete": True}, True)])
        )


@pytest.mark.asyncio
async def test_restore_chunks_reject_duplicate_chunk():
    payload = _restore_payload(text="x" * 50_000)
    chunks = list(
        encode_restore_payload_chunks(
            payload,
            request_id="restore-dup",
            channel_id="web",
            task_id="task-1",
        )
    )

    with pytest.raises(RestoreChunkDecodeError, match="duplicate"):
        await decode_restore_payload_from_stream(
            _aiter([chunks[0], chunks[0], AgentResponseChunk("restore-dup", "web", {"is_complete": True}, True)])
        )


@pytest.mark.asyncio
async def test_restore_chunks_reject_invalid_base64():
    bad_chunk = AgentResponseChunk(
        "restore-bad",
        "web",
        {
            "event_type": RESTORE_CHUNK_EVENT_TYPE,
            "restore_id": "restore-bad",
            "task_id": "task-1",
            "encoding": "json+base64",
            "index": 0,
            "total": 1,
            "data": "not-base64!",
        },
        False,
    )

    with pytest.raises(RestoreChunkDecodeError, match="failed to decode"):
        await decode_restore_payload_from_stream(
            _aiter([bad_chunk, AgentResponseChunk("restore-bad", "web", {"is_complete": True}, True)])
        )


class _FakeHistory:
    def __init__(self, payload: dict | None) -> None:
        self.payload = payload

    def restore_session(self, task_id: str) -> dict | None:
        assert task_id == "task-1"
        return self.payload


def _service(payload: dict | None) -> SkillDevService:
    deps = SkillDevDeps(
        model_name="",
        model_client_config={},
        model_config_obj={},
        sysop_config=None,
        state_store=None,
        workspace_provider=None,
        session_history=_FakeHistory(payload),
    )
    return SkillDevService(deps)


def _request(*, is_stream: bool) -> AgentRequest:
    return AgentRequest(
        request_id="restore-service",
        channel_id="web",
        session_id="task-1",
        req_method=ReqMethod.SKILLDEV_RESTORE,
        params={"task_id": "task-1"},
        is_stream=is_stream,
    )


@pytest.mark.asyncio
async def test_restore_service_unary_small_payload_keeps_legacy_shape():
    service = _service({k: v for k, v in _restore_payload().items() if k != "ok"})
    chunks = [chunk async for chunk in service.handle(_request(is_stream=False))]

    assert len(chunks) == 1
    assert chunks[0].is_complete is True
    assert chunks[0].payload["ok"] is True
    assert chunks[0].payload["task_id"] == "task-1"
    assert chunks[0].payload["timeline_items"][0]["payload"]["delta"] == "hello"


@pytest.mark.asyncio
async def test_restore_service_unary_large_payload_returns_small_error():
    service = _service({k: v for k, v in _restore_payload(text="x" * 90_000).items() if k != "ok"})
    chunks = [chunk async for chunk in service.handle(_request(is_stream=False))]

    assert len(chunks) == 1
    assert chunks[0].is_complete is True
    assert chunks[0].payload["event_type"] == "skilldev.error"
    assert chunks[0].payload["code"] == RESTORE_RESPONSE_TOO_LARGE_CODE
    assert len(json.dumps(chunks[0].payload).encode("utf-8")) < 60_000


@pytest.mark.asyncio
async def test_restore_decode_retry_recovers_on_second_attempt():
    payload = _restore_payload(text="x" * 50_000)
    chunks = list(
        encode_restore_payload_chunks(
            payload,
            request_id="restore-retry",
            channel_id="web",
            task_id="task-1",
        )
    )
    terminal = AgentResponseChunk("restore-retry", "web", {"is_complete": True}, True)
    broken = [chunks[0], terminal]
    good = [*chunks, terminal]
    calls = {"count": 0}

    def open_stream():
        calls["count"] += 1
        if calls["count"] == 1:
            return _aiter(broken)
        return _aiter(good)

    restored = await decode_restore_payload_from_stream_with_retry(open_stream, max_attempts=2)
    assert restored == payload
    assert calls["count"] == 2


def test_is_retriable_restore_decode_error():
    missing = RestoreChunkDecodeError("restore stream missing chunks: expected 7, got 6")
    assert is_retriable_restore_decode_error(missing) is True

    duplicate = RestoreChunkDecodeError("duplicate restore chunk index")
    assert is_retriable_restore_decode_error(duplicate) is False

    skilldev = RestoreChunkDecodeError("任务 task-1 不存在", code="SKILLDEV_RESTORE_ERROR")
    assert is_retriable_restore_decode_error(skilldev) is False


@pytest.mark.asyncio
async def test_restore_service_stream_large_payload_chunks_and_decodes():
    restored_payload = {k: v for k, v in _restore_payload(text="x" * 90_000).items() if k != "ok"}
    service = _service(restored_payload)
    chunks = [chunk async for chunk in service.handle(_request(is_stream=True))]

    assert chunks[-1].is_complete is True
    assert chunks[-1].payload == {"is_complete": True}
    assert len(chunks) > 2
    decoded = await decode_restore_payload_from_stream(_aiter(chunks))
    assert decoded == {"ok": True, **restored_payload}
