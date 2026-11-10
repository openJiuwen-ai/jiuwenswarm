# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Final model input remains recoverable without repeating its message bodies."""
import hashlib
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
import websockets

from openjiuwen.core.foundation.llm.call_scope import LlmCallScope
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.events import LLMCallEvents
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from jiuwenswarm.agents.harness.common import llm_audit_observer as observer
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.server.front.server import AgentServerFront
from jiuwenswarm.server.runtime.session import llm_audit as audit


@pytest.fixture
def audit_home(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(audit, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(audit.lc, "get_agent_sessions_dir", lambda: sessions)
    framework = AsyncCallbackFramework()
    monkeypatch.setattr(Runner, "callback_framework", framework)
    observer.install_llm_audit_observer()
    return SimpleNamespace(sessions=sessions, framework=framework, sid="audit-session")


class StreamSession:
    def __init__(self):
        self.events = []

    async def write_stream(self, event):
        self.events.append(event)


async def invoke(home, messages, *, identity="call-one", tools=None, failure=False):
    session = StreamSession()
    observer.bind_audit_scope(session_id=home.sid, request_id="request-one")
    with LlmCallScope(identity):
        await home.framework.trigger(LLMCallEvents.LLM_STREAM_INPUT, session=session,
                                     messages=messages, tools=tools or [], model="controlled-model")
        if failure:
            await home.framework.trigger(LLMCallEvents.LLM_CALL_ERROR, error="controlled HTTP failure")
            await observer.finish_pending_calls()
        else:
            await home.framework.trigger(LLMCallEvents.LLM_STREAM_COMPLETED,
                                         response={"role": "assistant", "content": "COMPLETE",
                                                   "usage_metadata": {"input_tokens": 42, "output_tokens": 3}})
    return session


def read_original(home, call_id, collection, index):
    chunks, offset = [], 0
    while True:
        part = audit.read_part(home.sid, call_id, collection=collection, index=index, offset=offset, max_bytes=4096)
        raw = part["content"].encode()
        assert len(raw) <= 4096 and part["next_offset"] == offset + len(raw)
        chunks.append(raw)
        offset = part["next_offset"]
        if part["eof"]:
            break
    original = b"".join(chunks)
    assert len(original) == part["bytes"] and hashlib.sha256(original).hexdigest() == part["sha256"]
    return json.loads(original)


@pytest.mark.asyncio
async def test_repeated_windows_share_original_blocks_and_preserve_each_call(audit_home):
    home = audit_home
    messages = [{"role": "user", "content": marker + ("\u4e2d\u6587\U0001f642 quote\" slash\\\n" * 5000)}
                for marker in ("A", "B", "C")]
    tool = {"name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}
    paths = []
    for size in range(1, 4):
        session = await invoke(home, messages[:size], identity=f"call-{size}", tools=[tool])
        assert [event.type for event in session.events] == ["llm_call_start", "llm_call_input", "llm_call_end"]
        assert len(json.dumps([event.model_dump() for event in session.events]).encode()) < 15000
        assert {event.payload["call_id"] for event in session.events} == {f"call-{size}"}
        for index, message in enumerate(messages[:size]):
            assert read_original(home, f"call-{size}", "messages", index) == message
        assert read_original(home, f"call-{size}", "tools", 0) == tool
        first = audit.get_parts(home.sid, f"call-{size}")["parts"][0]
        path = home.sessions / home.sid / "llm_audit/blocks" / first["sha256"]
        paths.append((first["sha256"], path.stat().st_mtime_ns))
    assert paths[0] == paths[1] == paths[2], "Reused message content was rewritten"
    blocks = list((home.sessions / home.sid / "llm_audit/blocks").iterdir())
    assert len(blocks) == 8  # A/B/C, one tool, one identical reply, three manifests.
    calls = audit.list_calls(home.sid, limit=2)
    older = audit.list_calls(home.sid, cursor=calls["next_cursor"], limit=2)
    assert [item["call_id"] for item in calls["calls"] + older["calls"]] == ["call-3", "call-2", "call-1"]
    assert older["next_cursor"] is None


@pytest.mark.asyncio
async def test_failed_call_retains_the_actual_original_input(audit_home):
    message = {"role": "user", "content": "FAILED CALL ORIGINAL"}
    session = await invoke(audit_home, [message], failure=True)
    record = audit.get_call(audit_home.sid, "call-one")
    assert record["status"] == "failed" and "controlled HTTP failure" in record["error"]
    assert read_original(audit_home, "call-one", "messages", 0) == message
    assert session.events[-1].payload["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["save_input", "save_response"])
async def test_artifact_failure_preserves_model_call_lifecycle(audit_home, monkeypatch, stage):
    def fail(*_args, **_kwargs):
        raise OSError("controlled disk failure")

    monkeypatch.setattr(audit, stage, fail)
    session = await invoke(audit_home, [{"role": "user", "content": "ORIGINAL"}])
    assert session.events[0].type == "llm_call_start" and session.events[-1].type == "llm_call_end"
    record = audit.get_call(audit_home.sid, "call-one")
    assert record["status"] == "completed" and record["capture_error"]


async def front_request(socket, sid, query, method="history.get"):
    identity = uuid.uuid4().hex
    await socket.send(json.dumps({"protocol_version": "1.0", "request_id": identity, "session_id": sid,
        "channel": "web", "user_id": "audit-owner", "identity_origin": "user", "method": method,
        "is_stream": False, "params": {"session_id": sid, "llm_audit": query}}))
    async with asyncio.timeout(10):
        while True:
            data = json.loads(await socket.recv())
            if data.get("request_id") == identity:
                return parse_agent_server_wire_unary(data)


@pytest.mark.asyncio
async def test_public_front_reads_saved_original_without_a_worker(audit_home):
    expected = {"role": "user", "content": "PUBLIC ORIGINAL \u4e2d\u6587\U0001f642"}
    await invoke(audit_home, [expected])
    front = AgentServerFront(port=0)
    await front.start()
    try:
        port = front._server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            await socket.recv()
            response = await front_request(socket, audit_home.sid, {"action": "list"})
            assert response.ok and response.payload["calls"][0]["call_id"] == "call-one"
            response = await front_request(socket, audit_home.sid,
                {"action": "read", "call_id": "call-one", "collection": "messages", "index": 0})
            assert response.ok and json.loads(response.payload["content"]) == expected
            response = await front_request(socket, audit_home.sid, {"action": "call", "call_id": "call-one"},
                                           method="session.get_metadata")
            assert response.ok and response.payload["llm_audit"]["status"] == "completed"
            wrong = await front_request(socket, "another-session", {"action": "call", "call_id": "call-one"})
            assert not wrong.ok and wrong.payload["code"] == "LLM_AUDIT_FAILED"
    finally:
        await front.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    {"action": "list", "limit": True}, {"action": "list", "limit": 101},
    {"action": "list", "cursor": "broken"}, {"action": "read", "call_id": "../invalid"},
    {"action": "read", "call_id": "call-one", "offset": -1},
    {"action": "read", "call_id": "call-one", "max_bytes": 65537},
])
async def test_front_audit_rejects_invalid_queries(audit_home, query):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.control.llm_audit_service import handle_llm_audit_request

    await invoke(audit_home, [{"role": "user", "content": "ORIGINAL"}])
    request = AgentRequest(request_id="read", session_id=audit_home.sid, channel_id="web",
                           req_method=ReqMethod.HISTORY_GET, params={"llm_audit": query})
    response = await handle_llm_audit_request(request)
    assert not response.ok and response.payload["code"] == "LLM_AUDIT_FAILED"


@pytest.mark.asyncio
async def test_fork_keeps_original_after_source_removal_and_excludes_future_response(audit_home):
    import shutil

    await invoke(audit_home, [{"role": "user", "content": "FORK ORIGINAL"}])
    audit.copy_call(audit_home.sid, "fork-partial", "call-one", include_response=False)
    partial = audit.get_call("fork-partial", "call-one")
    assert partial["status"] == "interrupted" and "response" not in partial
    audit.copy_call(audit_home.sid, "fork-complete", "call-one", include_response=False)
    audit.copy_call(audit_home.sid, "fork-complete", "call-one", include_response=True)
    assert audit.get_call("fork-complete", "call-one")["status"] == "completed"
    source = (audit_home.sessions / audit_home.sid).resolve()
    assert source.parent == audit_home.sessions.resolve()
    shutil.rmtree(source)
    target = SimpleNamespace(sid="fork-complete", sessions=audit_home.sessions)
    assert read_original(target, "call-one", "messages", 0)["content"] == "FORK ORIGINAL"


@pytest.mark.asyncio
async def test_parallel_calls_keep_session_request_and_native_call_identity(audit_home):
    async def run(sid, identity):
        home = SimpleNamespace(sid=sid, framework=audit_home.framework)
        await invoke(home, [{"role": "user", "content": identity}], identity=identity)
        assert read_original(home, identity, "messages", 0)["content"] == identity

    await asyncio.gather(run("first-session", "first-call"), run("second-session", "second-call"))
    assert [call["call_id"] for call in audit.list_calls("first-session")["calls"]] == ["first-call"]
    assert [call["call_id"] for call in audit.list_calls("second-session")["calls"]] == ["second-call"]


@pytest.mark.asyncio
async def test_cancelled_call_closes_pending_audit_and_keeps_original(audit_home):
    started = asyncio.Event()

    async def cancelled_model():
        observer.bind_audit_scope(session_id=audit_home.sid, request_id="cancel-request")
        try:
            with LlmCallScope("cancel-call"):
                await audit_home.framework.trigger(LLMCallEvents.LLM_STREAM_INPUT, session=StreamSession(),
                    messages=[{"role": "user", "content": "CANCEL ORIGINAL"}], tools=[])
                started.set()
                await asyncio.Event().wait()
        finally:
            await observer.finish_pending_calls("Request cancelled")

    task = asyncio.create_task(cancelled_model())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = audit.get_call(audit_home.sid, "cancel-call")
    assert record["status"] == "failed" and record["error"] == "Request cancelled"
    assert read_original(audit_home, "cancel-call", "messages", 0)["content"] == "CANCEL ORIGINAL"


@pytest.mark.asyncio
async def test_provider_recovery_overwrites_failed_status(audit_home):
    observer.bind_audit_scope(session_id=audit_home.sid, request_id="retry-request")
    with LlmCallScope("retry-call"):
        await audit_home.framework.trigger(LLMCallEvents.LLM_STREAM_INPUT, session=StreamSession(),
            messages=[{"role": "user", "content": "RETRY ORIGINAL"}], tools=[])
        await audit_home.framework.trigger(LLMCallEvents.LLM_CALL_ERROR, error="retryable provider error")
        await audit_home.framework.trigger(LLMCallEvents.LLM_STREAM_COMPLETED,
            response={"role": "assistant", "content": "RECOVERED"})
    await observer.finish_pending_calls()
    record = audit.get_call(audit_home.sid, "retry-call")
    assert record["status"] == "completed" and "error" not in record
    assert read_original(audit_home, "retry-call", "response", 0)["content"] == "RECOVERED"


@pytest.mark.asyncio
async def test_scheduler_cancellation_without_rail_cleanup_is_finalized(audit_home):
    started = asyncio.Event()

    async def model_task():
        observer.bind_audit_scope(session_id=audit_home.sid, request_id="native-cancel")
        with LlmCallScope("scheduler-cancel"):
            await audit_home.framework.trigger(LLMCallEvents.LLM_STREAM_INPUT, session=StreamSession(),
                messages=[{"role": "user", "content": "SCHEDULER ORIGINAL"}], tools=[])
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(model_task())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(5):
        while audit.get_call(audit_home.sid, "scheduler-cancel")["status"] == "running":
            await asyncio.sleep(.01)
    assert read_original(audit_home, "scheduler-cancel", "messages", 0)["content"] == "SCHEDULER ORIGINAL"
    await asyncio.gather(*observer._CLEANUPS)
    assert not observer._CLEANUPS


@pytest.mark.asyncio
async def test_stream_producer_normal_exit_waits_for_consumer_completion(audit_home):
    observer.bind_audit_scope(session_id=audit_home.sid, request_id="stream-consumer")

    async def producer():
        with LlmCallScope("producer-call"):
            await audit_home.framework.trigger(LLMCallEvents.LLM_STREAM_INPUT, session=StreamSession(),
                messages=[{"role": "user", "content": "STREAM ORIGINAL"}], tools=[])

    await asyncio.create_task(producer())
    await asyncio.sleep(.01)
    assert audit.get_call(audit_home.sid, "producer-call")["status"] == "running"
    with LlmCallScope("producer-call"):
        await audit_home.framework.trigger(LLMCallEvents.LLM_STREAM_COMPLETED,
            response={"role": "assistant", "content": "CONSUMER COMPLETED"})
    assert audit.get_call(audit_home.sid, "producer-call")["status"] == "completed"
    assert read_original(audit_home, "producer-call", "response", 0)["content"] == "CONSUMER COMPLETED"


@pytest.mark.asyncio
async def test_owner_reuse_projects_interrupted_and_manifest_corruption_fails(audit_home):
    await invoke(audit_home, [{"role": "user", "content": "OWNER ORIGINAL"}])
    record = audit.get_call(audit_home.sid, "call-one")
    record.update(status="running", worker_created=record["worker_created"] - 1)
    audit.write_call(audit_home.sid, record)
    assert audit.get_call(audit_home.sid, "call-one")["status"] == "interrupted"
    manifest = audit_home.sessions / audit_home.sid / "llm_audit/blocks" / record["input"]["sha256"]
    raw = manifest.read_bytes()
    manifest.write_bytes(raw.replace(b"messages", b"messAgeS"))
    with pytest.raises(ValueError, match="integrity"):
        audit.get_parts(audit_home.sid, "call-one")
