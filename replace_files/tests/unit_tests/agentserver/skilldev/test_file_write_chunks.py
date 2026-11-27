from __future__ import annotations

import json
from collections.abc import Iterable

import pytest

from jiuwenclaw.agentserver.skilldev.deps import SkillDevDeps
from jiuwenclaw.agentserver.skilldev.error_codes import ERR_FW_CONTENT_TOO_LARGE
from jiuwenclaw.agentserver.skilldev.file_write_chunks import (
    FILE_WRITE_UNARY_SAFE_BYTES,
    FileWriteStagingStore,
    encode_file_write_chunks,
    send_file_write_with_chunks,
)
from jiuwenclaw.agentserver.skilldev.service import SkillDevService
from jiuwenclaw.agentserver.skilldev.workspace import WorkspaceProvider
from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenclaw.schema.message import ReqMethod


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


def _write_target(tmp_path, content: str = "old"):
    skill_dir = tmp_path / "task-1" / "skill"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "notes.txt"
    target.write_text(content, encoding="utf-8")
    return target


async def _handle(service: SkillDevService, params: dict) -> AgentResponseChunk:
    request = AgentRequest(
        request_id="write-request",
        channel_id="web",
        session_id="task-1",
        req_method=ReqMethod.SKILLDEV_FILE_WRITE,
        params={"task_id": "task-1", "path": "notes.txt", **params},
    )
    chunks = [chunk async for chunk in service.handle(request)]
    assert len(chunks) == 1
    return chunks[0]


def _metadata(chunks: Iterable[dict]) -> dict:
    first = next(iter(chunks))
    return {
        "write_id": first["write_id"],
        "total": first["total"],
        "size_bytes": first["size_bytes"],
        "sha256": first["sha256"],
    }


def test_file_write_chunks_keep_wire_messages_below_limit():
    content = ('中文🙂\\"\n' * 15_000) + "tail"
    chunks = list(encode_file_write_chunks(content, write_id="write-wire-1234"))

    assert len(chunks) > 1
    for index, chunk in enumerate(chunks):
        env = e2a_from_agent_fields(
            request_id=f"write-{index}",
            channel_id="web",
            session_id="task-1",
            req_method=ReqMethod.SKILLDEV_FILE_WRITE,
            params={"task_id": "task-1", "path": "notes.txt", **chunk},
        )
        assert len(json.dumps(env.to_dict(), ensure_ascii=False).encode("utf-8")) < 60_000


@pytest.mark.asyncio
async def test_file_write_service_chunk_commit_roundtrip_and_idempotency(tmp_path):
    target = _write_target(tmp_path)
    service = _service(tmp_path)
    content = "中文🙂\n" * 20_000
    chunks = list(encode_file_write_chunks(content, write_id="write-roundtrip-1234"))

    for chunk in reversed(chunks):
        response = await _handle(service, chunk)
        assert response.payload["ok"] is True
        assert response.payload["accepted"] is True
    duplicate = await _handle(service, chunks[0])
    assert duplicate.payload["accepted"] is True

    commit = {**_metadata(chunks), "phase": "commit"}
    response = await _handle(service, commit)
    assert response.payload["ok"] is True
    assert set(response.payload) == {"ok", "path", "size", "repackaged"}
    assert target.read_text(encoding="utf-8") == content

    repeated = await _handle(service, commit)
    assert repeated.payload == response.payload


@pytest.mark.asyncio
async def test_file_write_service_rejects_missing_chunk_without_changing_file(tmp_path):
    target = _write_target(tmp_path)
    service = _service(tmp_path)
    chunks = list(encode_file_write_chunks("x" * 70_000, write_id="write-missing-1234"))

    await _handle(service, chunks[0])
    response = await _handle(service, {**_metadata(chunks), "phase": "commit"})

    assert response.payload["ok"] is False
    assert "missing" in response.payload["error"]
    assert target.read_text(encoding="utf-8") == "old"


def test_file_write_staging_rejects_invalid_base64_and_abort(tmp_path):
    store = FileWriteStagingStore(tmp_path)
    params = {
        "phase": "chunk",
        "write_id": "write-invalid-1234",
        "path": "notes.txt",
        "encoding": "base64",
        "index": 0,
        "total": 1,
        "size_bytes": 3,
        "sha256": "0" * 64,
        "data": "not-base64!",
    }

    with pytest.raises(ValueError, match="base64"):
        store.accept_chunk(params)
    assert store.abort(params)["status"] == "aborted"
    assert store.status(params)["status"] == "unknown"


def test_file_write_staging_rejects_digest_mismatch(tmp_path):
    store = FileWriteStagingStore(tmp_path)
    chunks = list(encode_file_write_chunks("hello", write_id="write-digest-1234"))
    chunks[0]["sha256"] = "0" * 64
    store.accept_chunk({"path": "notes.txt", **chunks[0]})

    with pytest.raises(ValueError, match="sha256 mismatch"):
        store.assemble({"path": "notes.txt", **_metadata(chunks)})


@pytest.mark.asyncio
async def test_file_write_service_rejects_invalid_utf8_without_changing_file(tmp_path):
    import base64
    import hashlib

    target = _write_target(tmp_path)
    service = _service(tmp_path)
    raw = b"\xff\xfe"
    metadata = {
        "write_id": "write-utf8-1234",
        "total": 1,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    response = await _handle(
        service,
        {
            **metadata,
            "phase": "chunk",
            "encoding": "base64",
            "index": 0,
            "data": base64.b64encode(raw).decode(),
        },
    )
    assert response.payload["ok"] is True

    response = await _handle(service, {**metadata, "phase": "commit"})
    assert response.payload["ok"] is False
    assert "UTF-8" in response.payload["error"]
    assert target.read_text(encoding="utf-8") == "old"


@pytest.mark.asyncio
async def test_file_write_service_keeps_existing_character_limit(tmp_path):
    target = _write_target(tmp_path)
    service = _service(tmp_path)

    response = await _handle(service, {"content": "x" * (1_048_576 + 1)})

    assert response.payload["ok"] is False
    assert response.payload["error"] == ERR_FW_CONTENT_TOO_LARGE
    assert target.read_text(encoding="utf-8") == "old"


@pytest.mark.asyncio
async def test_file_write_sender_keeps_small_write_legacy():
    calls: list[tuple[dict, str, int]] = []

    async def send(params, phase, attempt):
        calls.append((params, phase, attempt))
        return AgentResponse("r", "web", True, {"ok": True, "path": "notes.txt"})

    await send_file_write_with_chunks(
        base_params={"task_id": "task-1", "path": "notes.txt"},
        content="small",
        write_id="write-legacy-1234",
        send_request=send,
    )

    assert len(calls) == 1
    assert calls[0][1:] == ("legacy", 0)
    assert calls[0][0]["content"] == "small"


@pytest.mark.asyncio
async def test_file_write_sender_retries_each_chunk_at_most_twice(monkeypatch):
    calls: list[tuple[str, int, int | None]] = []

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.skilldev.file_write_chunks.asyncio.sleep", no_sleep
    )

    async def send(params, phase, attempt):
        calls.append((phase, attempt, params.get("index")))
        if phase == "chunk" and params["index"] == 0 and attempt == 0:
            raise ConnectionError("temporary")
        if phase == "chunk" and params["index"] == 0 and attempt == 1:
            return AgentResponse(
                "r", "web", False, {"ok": False, "code": "SERVICE_UNAVAILABLE"}
            )
        if phase == "commit":
            return AgentResponse("r", "web", True, {"ok": True, "path": "notes.txt"})
        return AgentResponse("r", "web", True, {"ok": True, "accepted": True})

    await send_file_write_with_chunks(
        base_params={"task_id": "task-1", "path": "notes.txt"},
        content="x" * (FILE_WRITE_UNARY_SAFE_BYTES + 1),
        write_id="write-retry-1234",
        send_request=send,
    )

    first_chunk_calls = [call for call in calls if call[0] == "chunk" and call[2] == 0]
    assert [call[1] for call in first_chunk_calls] == [0, 1, 2]


@pytest.mark.asyncio
async def test_file_write_sender_does_not_retry_business_error():
    phases: list[tuple[str, int]] = []

    async def send(_params, phase, attempt):
        phases.append((phase, attempt))
        if phase == "chunk":
            return AgentResponse("r", "web", True, {"ok": False, "error": "invalid path"})
        return AgentResponse("r", "web", True, {"ok": True})

    response = await send_file_write_with_chunks(
        base_params={"task_id": "task-1", "path": "notes.txt"},
        content="x" * (FILE_WRITE_UNARY_SAFE_BYTES + 1),
        write_id="write-business-1234",
        send_request=send,
    )

    assert response.payload["ok"] is False
    assert phases == [("chunk", 0), ("abort", 0)]


@pytest.mark.asyncio
async def test_file_write_sender_recovers_lost_commit_response_via_status(monkeypatch):
    phases: list[tuple[str, int]] = []

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.skilldev.file_write_chunks.asyncio.sleep", no_sleep
    )

    async def send(_params, phase, attempt):
        phases.append((phase, attempt))
        if phase == "commit":
            raise TimeoutError("lost response")
        if phase == "status":
            return AgentResponse(
                "r",
                "web",
                True,
                {"ok": True, "status": "committed", "path": "notes.txt"},
            )
        return AgentResponse("r", "web", True, {"ok": True})

    response = await send_file_write_with_chunks(
        base_params={"task_id": "task-1", "path": "notes.txt"},
        content="x" * (FILE_WRITE_UNARY_SAFE_BYTES + 1),
        write_id="write-status-1234",
        send_request=send,
    )

    assert response.payload["status"] == "committed"
    assert [attempt for phase, attempt in phases if phase == "commit"] == [0, 1, 2]
    assert sum(phase == "status" for phase, _ in phases) == 1


@pytest.mark.asyncio
async def test_vibeskill_file_write_handler_uses_chunk_sender():
    from jiuwenclaw.channel.vibeskill_channel import VibeSkillChannel

    class FakeChannel:
        def __init__(self):
            self.envelopes = []

        @staticmethod
        def _apply_tenant_service_id(params, _session_id):
            params["service_id"] = "tenant-1"

        @staticmethod
        def _session_user_id(_session_id):
            return "user-1"

        @staticmethod
        def _json_response(status, payload):
            return status, {}, json.dumps(payload).encode()

        async def _send_agent_request(self, env):
            self.envelopes.append(env)
            if env.params.get("phase") == "commit":
                payload = {
                    "ok": True,
                    "path": "notes.txt",
                    "size": 70_000,
                    "repackaged": False,
                }
            else:
                payload = {"ok": True, "accepted": True}
            return AgentResponse(env.request_id, env.channel, True, payload)

    channel = FakeChannel()
    status, _, body = await VibeSkillChannel._handle_http_file_content_write(
        channel,
        "task-1",
        {},
        "/api/v1/session/task-1/file/content?path=notes.txt",
        json.dumps({"content": "x" * 70_000}).encode(),
    )

    assert status == 200
    assert json.loads(body)["ok"] is True
    phases = [env.params.get("phase", "legacy") for env in channel.envelopes]
    assert phases[-1] == "commit"
    assert phases.count("chunk") > 1
    assert all(
        len(json.dumps(env.to_dict(), ensure_ascii=False).encode("utf-8")) < 60_000
        for env in channel.envelopes
    )


@pytest.mark.asyncio
async def test_websocket_file_write_handler_uses_chunk_sender():
    from jiuwenclaw.app_web_handlers import (
        WebHandlersBindParams,
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
        _FORWARD_REQ_METHODS,
        _register_web_handlers,
    )

    class FakeAgentClient:
        server_ready = True

        def __init__(self):
            self.envelopes = []

        async def send_request(self, env):
            self.envelopes.append(env)
            payload = (
                {"ok": True, "path": "notes.txt", "size": 70_000, "repackaged": False}
                if env.params.get("phase") == "commit"
                else {"ok": True, "accepted": True}
            )
            return AgentResponse(env.request_id, env.channel, True, payload)

    class FakeWebChannel:
        channel_id = "web"

        def __init__(self):
            self.handlers = {}
            self.responses = []

        def on_connect(self, handler):
            self.on_connect_handler = handler

        def register_method(self, name, handler):
            self.handlers[name] = handler

        async def send_response(self, _ws, request_id, **kwargs):
            self.responses.append((request_id, kwargs))

    channel = FakeWebChannel()
    client = FakeAgentClient()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))

    assert "skilldev.file.write" not in _FORWARD_REQ_METHODS
    assert "skilldev.file.write" not in _FORWARD_NO_LOCAL_HANDLER_METHODS
    await channel.handlers["skilldev.file.write"](
        object(),
        "browser-request",
        {"task_id": "task-1", "path": "notes.txt", "content": "x" * 70_000},
        "task-1",
    )

    assert channel.responses[-1][1]["ok"] is True
    phases = [env.params.get("phase", "legacy") for env in client.envelopes]
    assert phases[-1] == "commit"
    assert phases.count("chunk") > 1
