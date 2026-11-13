"""Artifact retrieval must work through the public Front control route."""
import asyncio
import datetime
import hashlib
import json
from uuid import uuid4

import pytest
import websockets
from e2a.wire_codec import parse_agent_server_wire_unary

from jiuwenswarm.server.front.server import AgentServerFront
from jiuwenswarm.server.runtime.session.history_artifacts import put_text
from tests.unit_tests.test_context_snapshots import stored, user
from tests.unit_tests.test_history_index_paging import semantic_records


async def _request(socket, session_id, params, *, stream=False):
    identity = uuid4().hex
    await socket.send(json.dumps({
        "protocol_version": "1.0", "request_id": identity, "channel": "web",
        "session_id": session_id, "user_id": "artifact-test-owner", "identity_origin": "user",
        "method": "history.get", "is_stream": stream,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), "params": params,
    }))
    async with asyncio.timeout(10):
        while True:
            data = json.loads(await socket.recv())
            if data.get("request_id") == identity:
                return parse_agent_server_wire_unary(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_format", ["json", "jsonl"])
@pytest.mark.parametrize("stream", [False, True])
async def test_front_history_page_retains_tool_payload_and_usage(stored, semantic_records, source_format, stream):
    from e2a.wire_codec import parse_agent_server_wire_chunk

    path = stored.path.with_suffix("." + source_format)
    if path != stored.path:
        stored.path.unlink()
    text = json.dumps(semantic_records) if source_format == "json" else "".join(
        json.dumps(record) + "\n" for record in semantic_records
    )
    path.write_text(text, encoding="utf-8")
    original = path.read_bytes()
    front = AgentServerFront(port=0)
    await front.start()
    try:
        port = front._server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            await socket.recv()
            if stream:
                rid = uuid4().hex
                await socket.send(json.dumps({
                    "protocol_version": "1.0", "request_id": rid, "channel": "web",
                    "session_id": stored.sid, "user_id": "paging-test-owner", "identity_origin": "user",
                    "method": "history.get", "is_stream": True,
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "params": {"session_id": stored.sid, "page_idx": 1},
                }))
                messages = []
                async with asyncio.timeout(10):
                    while True:
                        raw = json.loads(await socket.recv())
                        if raw.get("request_id") != rid:
                            continue
                        reply = parse_agent_server_wire_chunk(raw)
                        assert not reply.payload.get("error"), reply.payload
                        if "message" in reply.payload:
                            messages.append(reply.payload["message"])
                        if reply.is_complete:
                            assert reply.payload["status"] == "done"
                            break
            else:
                reply = await _request(socket, stored.sid, {"session_id": stored.sid, "page_idx": 1})
                assert reply.ok, reply.payload
                messages = reply.payload["messages"]
            assert messages == list(reversed(semantic_records))
    finally:
        await front.stop()
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_session", [False, True])
async def test_front_reads_complete_utf8_artifact_in_chunks(stored, explicit_session):
    expected = ("ORIGINAL \u4e2d\u6587\U0001f642 quote\" slash\\ line\n" * 5000).encode()
    reference = put_text(stored.sid, expected.decode())
    front = AgentServerFront(port=0)
    await front.start()
    try:
        port = front._server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            await socket.recv()
            offset = 0
            parts = []
            while True:
                params = {"artifact_sha256": reference["sha256"], "offset": offset, "max_bytes": 4096}
                if explicit_session:
                    params["session_id"] = stored.sid
                reply = await _request(socket, "unused" if explicit_session else stored.sid, params)
                assert reply.ok, reply.payload
                data = reply.payload
                block = data["content"].encode()
                assert 0 < len(block) <= 4096 and data["next_offset"] == offset + len(block)
                parts.append(block)
                offset = data["next_offset"]
                if data["eof"]:
                    break
            actual = b"".join(parts)
            assert actual == expected and len(actual) == reference["bytes"]
            assert hashlib.sha256(actual).hexdigest() == reference["sha256"]
    finally:
        await front.stop()

@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_front_index_page_budget_failure_is_an_explicit_error(stored, monkeypatch, stream):
    from e2a.wire_codec import parse_agent_server_wire_chunk
    from jiuwenswarm.server.runtime.session import history_paging

    stored.write([user("normal", "preserve original")])
    original = stored.path.read_bytes()
    monkeypatch.setattr(history_paging, "_PAGE_BYTES", 1)
    front = AgentServerFront(port=0)
    await front.start()
    try:
        port = front._server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            await socket.recv()
            rid = uuid4().hex
            await socket.send(json.dumps({
                "protocol_version": "1.0", "request_id": rid, "channel": "web",
                "session_id": stored.sid, "user_id": "budget-test-owner", "identity_origin": "user",
                "method": "history.get", "is_stream": stream,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "params": {"session_id": stored.sid, "page_idx": 1},
            }))
            async with asyncio.timeout(10):
                while True:
                    raw = json.loads(await socket.recv())
                    if raw.get("request_id") == rid:
                        break
            reply = parse_agent_server_wire_chunk(raw) if stream else parse_agent_server_wire_unary(raw)
            assert reply.payload["code"] == "HISTORY_RESTORE_LIMIT"
            if stream:
                assert reply.is_complete and reply.payload["status"] == "error"
            else:
                assert not reply.ok
    finally:
        await front.stop()
    assert stored.path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"artifact_sha256": "../invalid"}, {"offset": -1}, {"max_bytes": 65537},
    {"max_bytes": True}, {"session_id": "another-session"}, {"stream": True},
])
async def test_front_artifact_failures_keep_the_public_error_contract(stored, change):
    reference = put_text(stored.sid, "original")
    params = {"artifact_sha256": reference["sha256"]}
    params.update({key: value for key, value in change.items() if key != "stream"})
    front = AgentServerFront(port=0)
    await front.start()
    try:
        port = front._server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            await socket.recv()
            reply = await _request(socket, stored.sid, params, stream=change.get("stream", False))
            assert not reply.ok and reply.payload["code"] == "HISTORY_ARTIFACT_FAILED"
    finally:
        await front.stop()


@pytest.mark.asyncio
async def test_front_normal_history_paging_still_works(stored):
    stored.write([user("normal", "ordinary conversation")])
    front = AgentServerFront(port=0)
    await front.start()
    try:
        port = front._server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            await socket.recv()
            reply = await _request(socket, stored.sid, {"session_id": stored.sid, "page_idx": 1})
            assert reply.ok and reply.payload["messages"][0]["content"] == "ordinary conversation"
    finally:
        await front.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_front_cursor_transport_reads_original_and_isolates_artifact_errors(stored, stream):
    from e2a.wire_codec import parse_agent_server_wire_chunk
    from jiuwenswarm.server.runtime.session.history_artifacts import ARTIFACT_CURSOR_PREFIX

    stored.write([user("normal", "ordinary conversation survives artifact reads")])
    history_before = stored.path.read_bytes()
    original = ("x" * 8191 + "中文🙂\n\"\\" * 3000).encode()
    reference = put_text(stored.sid, original.decode())
    front = AgentServerFront(port=0)
    await front.start()
    try:
        port = front._server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            await socket.recv()
            offset = 0
            restored = bytearray()
            while offset < len(original):
                cursor = f"{ARTIFACT_CURSOR_PREFIX}{reference['sha256']}:{offset}"
                params = {"session_id": stored.sid, "cursor": cursor, "limit": 1}
                if stream:
                    rid = uuid4().hex
                    await socket.send(json.dumps({
                        "protocol_version": "1.0", "request_id": rid, "channel": "web",
                        "session_id": stored.sid, "user_id": "artifact-test-owner", "identity_origin": "user",
                        "method": "history.get", "is_stream": True,
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), "params": params,
                    }))
                    chunks = []
                    async with asyncio.timeout(10):
                        while True:
                            raw = json.loads(await socket.recv())
                            if raw.get("request_id") != rid:
                                continue
                            reply = parse_agent_server_wire_chunk(raw)
                            assert reply.payload["cursor"] == cursor
                            assert reply.payload["session_id"] == stored.sid
                            assert not reply.payload.get("error"), reply.payload
                            if "message" in reply.payload:
                                chunks.append(reply.payload["message"])
                            if reply.is_complete:
                                assert reply.payload["status"] == "done"
                                break
                else:
                    reply = await _request(socket, stored.sid, params)
                    assert reply.ok, reply.payload
                    chunks = reply.payload["messages"]
                assert len(chunks) == 1 and chunks[0]["event_type"] == "history.artifact"
                chunk = chunks[0]
                encoded = chunk["content"].encode()
                assert 0 < len(encoded) <= 8192
                assert chunk["offset"] == offset and chunk["next_offset"] == offset + len(encoded)
                restored.extend(encoded)
                offset = chunk["next_offset"]
                assert chunk["eof"] == (offset == len(original))
            assert bytes(restored) == original
            for other_session, digest in [(stored.sid, "f" * 64), ("other-session", reference["sha256"])]:
                reply = await _request(socket, other_session, {
                    "session_id": other_session,
                    "cursor": f"{ARTIFACT_CURSOR_PREFIX}{digest}:0", "limit": 1,
                })
                assert not reply.ok and reply.payload["code"] == "HISTORY_ARTIFACT_FAILED"
            reply = await _request(socket, stored.sid, {"session_id": stored.sid, "page_idx": 1})
            assert reply.ok and reply.payload["messages"][0]["content"].startswith("ordinary conversation")
    finally:
        await front.stop()
    assert stored.path.read_bytes() == history_before
