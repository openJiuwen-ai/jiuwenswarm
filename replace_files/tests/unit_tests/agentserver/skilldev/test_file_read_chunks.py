from __future__ import annotations

import json
from collections.abc import Iterable

import pytest

from jiuwenclaw.agentserver.skilldev.deps import SkillDevDeps
from jiuwenclaw.agentserver.skilldev.file_read_chunks import (
    FILE_READ_CHUNK_EVENT_TYPE,
    FILE_READ_RESPONSE_TOO_LARGE_CODE,
    FileReadChunkDecodeError,
    decode_file_read_payload_from_stream,
    encode_file_read_payload_chunks,
)
from jiuwenclaw.agentserver.skilldev.service import SkillDevService
from jiuwenclaw.agentserver.skilldev.workspace import WorkspaceProvider
from jiuwenclaw.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenclaw.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenclaw.schema.message import ReqMethod


async def _aiter(chunks: Iterable[AgentResponseChunk]):
    for chunk in chunks:
        yield chunk


def _file_read_payload(*, content: str = "hello") -> dict:
    return {
        "ok": True,
        "path": "SKILL.md",
        "content": content,
    }


@pytest.mark.asyncio
async def test_file_read_chunks_roundtrip_single_chunk():
    payload = _file_read_payload()
    chunks = list(
        encode_file_read_payload_chunks(
            payload,
            request_id="read-1",
            channel_id="web",
            task_id="task-1",
            path="SKILL.md",
        )
    )

    assert len(chunks) == 1
    assert chunks[0].payload["event_type"] == FILE_READ_CHUNK_EVENT_TYPE
    assert chunks[0].payload["read_id"] == "read-1"
    assert chunks[0].payload["path"] == "SKILL.md"

    decoded = await decode_file_read_payload_from_stream(
        _aiter([*chunks, AgentResponseChunk("read-1", "web", {"is_complete": True}, True)])
    )
    assert decoded == payload


@pytest.mark.asyncio
async def test_file_read_chunks_roundtrip_large_payload_and_wire_size():
    payload = _file_read_payload(content="x" * 90_000)
    chunks = list(
        encode_file_read_payload_chunks(
            payload,
            request_id="read-large",
            channel_id="web",
            task_id="task-1",
            path="SKILL.md",
        )
    )

    assert len(chunks) > 1
    for index, chunk in enumerate(chunks):
        assert chunk.payload["index"] == index
        wire = encode_agent_chunk_for_wire(
            chunk,
            response_id="read-large",
            sequence=index,
        )
        assert len(json.dumps(wire, ensure_ascii=False).encode("utf-8")) < 60_000

    decoded = await decode_file_read_payload_from_stream(
        _aiter([*chunks, AgentResponseChunk("read-large", "web", {"is_complete": True}, True)])
    )
    assert decoded == payload


@pytest.mark.asyncio
async def test_file_read_chunks_reject_missing_chunk():
    payload = _file_read_payload(content="x" * 50_000)
    chunks = list(
        encode_file_read_payload_chunks(
            payload,
            request_id="read-missing",
            channel_id="web",
            task_id="task-1",
            path="SKILL.md",
        )
    )

    with pytest.raises(FileReadChunkDecodeError, match="missing chunks"):
        await decode_file_read_payload_from_stream(
            _aiter([chunks[0], AgentResponseChunk("read-missing", "web", {"is_complete": True}, True)])
        )


@pytest.mark.asyncio
async def test_file_read_chunks_reject_duplicate_chunk():
    payload = _file_read_payload(content="x" * 50_000)
    chunks = list(
        encode_file_read_payload_chunks(
            payload,
            request_id="read-dup",
            channel_id="web",
            task_id="task-1",
            path="SKILL.md",
        )
    )

    with pytest.raises(FileReadChunkDecodeError, match="duplicate"):
        await decode_file_read_payload_from_stream(
            _aiter([chunks[0], chunks[0], AgentResponseChunk("read-dup", "web", {"is_complete": True}, True)])
        )


@pytest.mark.asyncio
async def test_file_read_chunks_reject_invalid_base64():
    bad_chunk = AgentResponseChunk(
        "read-bad",
        "web",
        {
            "event_type": FILE_READ_CHUNK_EVENT_TYPE,
            "read_id": "read-bad",
            "task_id": "task-1",
            "path": "SKILL.md",
            "encoding": "json+base64",
            "index": 0,
            "total": 1,
            "data": "not-base64!",
        },
        False,
    )

    with pytest.raises(FileReadChunkDecodeError, match="failed to decode"):
        await decode_file_read_payload_from_stream(
            _aiter([bad_chunk, AgentResponseChunk("read-bad", "web", {"is_complete": True}, True)])
        )


@pytest.mark.asyncio
async def test_file_read_chunks_reject_mixed_read_id():
    payload = _file_read_payload(content="x" * 50_000)
    chunks = list(
        encode_file_read_payload_chunks(
            payload,
            request_id="read-a",
            channel_id="web",
            task_id="task-1",
            path="SKILL.md",
        )
    )
    chunks[1].payload["read_id"] = "read-b"

    with pytest.raises(FileReadChunkDecodeError, match="mixed read_id"):
        await decode_file_read_payload_from_stream(
            _aiter([*chunks, AgentResponseChunk("read-a", "web", {"is_complete": True}, True)])
        )


@pytest.mark.asyncio
async def test_file_read_chunks_reject_unexpected_event_type():
    unexpected = AgentResponseChunk(
        "read-event",
        "web",
        {"event_type": "skilldev.restore.chunk"},
        False,
    )

    with pytest.raises(FileReadChunkDecodeError, match="unexpected file read chunk event_type"):
        await decode_file_read_payload_from_stream(_aiter([unexpected]))


def _service(tmp_path) -> SkillDevService:
    deps = SkillDevDeps(
        model_name="",
        model_client_config={},
        model_config_obj={},
        sysop_config=None,
        state_store=None,
        workspace_provider=WorkspaceProvider(tmp_path),
        session_history=None,
    )
    return SkillDevService(deps)


def _request(*, is_stream: bool, path: str = "SKILL.md") -> AgentRequest:
    return AgentRequest(
        request_id="file-read-service",
        channel_id="web",
        session_id="task-1",
        req_method=ReqMethod.SKILLDEV_FILE_READ,
        params={"task_id": "task-1", "path": path},
        is_stream=is_stream,
    )


def _write_skill_file(tmp_path, content: str) -> None:
    skill_dir = tmp_path / "task-1" / "skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_file_read_service_unary_small_payload_keeps_legacy_shape(tmp_path):
    _write_skill_file(tmp_path, "hello")
    service = _service(tmp_path)

    chunks = [chunk async for chunk in service.handle(_request(is_stream=False))]

    assert len(chunks) == 1
    assert chunks[0].is_complete is True
    assert chunks[0].payload == {
        "ok": True,
        "path": "SKILL.md",
        "content": "hello",
        "editable": True,
    }


@pytest.mark.asyncio
async def test_file_read_service_unary_large_payload_returns_small_error(tmp_path):
    _write_skill_file(tmp_path, "x" * 90_000)
    service = _service(tmp_path)

    chunks = [chunk async for chunk in service.handle(_request(is_stream=False))]

    assert len(chunks) == 1
    assert chunks[0].is_complete is True
    assert chunks[0].payload["event_type"] == "skilldev.error"
    assert chunks[0].payload["code"] == FILE_READ_RESPONSE_TOO_LARGE_CODE
    assert len(json.dumps(chunks[0].payload).encode("utf-8")) < 60_000


@pytest.mark.asyncio
async def test_file_read_service_stream_large_payload_chunks_and_decodes(tmp_path):
    content = "x" * 90_000
    _write_skill_file(tmp_path, content)
    service = _service(tmp_path)

    chunks = [chunk async for chunk in service.handle(_request(is_stream=True))]

    assert chunks[-1].is_complete is True
    assert chunks[-1].payload == {"is_complete": True}
    assert len(chunks) > 2
    decoded = await decode_file_read_payload_from_stream(_aiter(chunks))
    assert decoded == {
        "ok": True,
        "path": "SKILL.md",
        "content": content,
        "editable": True,
    }


@pytest.mark.asyncio
async def test_file_read_service_rejects_path_traversal(tmp_path):
    _write_skill_file(tmp_path, "hello")
    (tmp_path / "task-1" / "secret.txt").write_text("secret", encoding="utf-8")
    service = _service(tmp_path)

    chunks = [chunk async for chunk in service.handle(_request(is_stream=True, path="../secret.txt"))]

    assert len(chunks) == 1
    assert chunks[0].payload["event_type"] == "skilldev.error"
    assert "路径非法" in chunks[0].payload["error"]


@pytest.mark.asyncio
async def test_file_read_service_reports_missing_file(tmp_path):
    (tmp_path / "task-1" / "skill").mkdir(parents=True)
    service = _service(tmp_path)

    chunks = [chunk async for chunk in service.handle(_request(is_stream=True, path="missing.md"))]

    assert len(chunks) == 1
    assert chunks[0].payload["event_type"] == "skilldev.error"
    assert "文件不存在" in chunks[0].payload["error"]
