"""Artifact retrieval must work through the public Front control route."""
import asyncio
import datetime
import hashlib
import json
from uuid import uuid4

import pytest
import websockets

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.server.front.server import AgentServerFront
from jiuwenswarm.server.runtime.session.history_artifacts import put_text
from tests.unit_tests.test_context_snapshots import stored, user


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
