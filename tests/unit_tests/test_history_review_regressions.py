"""Review regressions exercise recovery, persistence, and failure projections."""
import asyncio
import copy
import json
import threading
import tracemalloc
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.foundation.llm import UserMessage

from jiuwenswarm.server.runtime.session import history_artifacts as artifacts
from jiuwenswarm.server.runtime.session import history_replay as replay
from jiuwenswarm.server.runtime.session import history_stream as streaming
from jiuwenswarm.server.runtime.session import session_history as history
from tests.unit_tests.test_context_snapshots import stored, user, native_session


def _agent(engine):
    return SimpleNamespace(react_agent=SimpleNamespace(
        context_engine=engine, _config=SimpleNamespace(context_processors=[]),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_restarted_unbound_session_can_commit_restored_owner(stored, monkeypatch, interrupted):
    from jiuwenswarm.runtime import context_checkpointer as checkpoint

    _, delegate = await native_session(stored, monkeypatch)
    monkeypatch.setattr(checkpoint, "_by_key", weakref.WeakValueDictionary())
    owner = {"session_id": stored.sid, "agent_id": "test-agent", "token": "previous-process"}
    stored.session.update_state({checkpoint.OWNER_KEY: owner, "rewound": True})
    if interrupted:
        await stored.session.commit_interrupt()
    else:
        await stored.session.commit()
    assert delegate.disk["rewound"] is True


@pytest.mark.asyncio
async def test_empty_migration_restores_live_context_and_session_state(stored):
    from jiuwenswarm.server.runtime.session.context_migration import migrate_history_context

    stored.write([{"event_type": "chat.llm_call_start", "prompt": "audit only"}])
    engine = ContextEngine()
    await engine.create_context(session=stored.session, history_messages=[UserMessage(content="PREVIOUS")])
    previous = copy.deepcopy(stored.session.values)
    assert not await migrate_history_context(
        context_engine=engine, session=stored.session, processors=[], before_request_id=None,
    )
    assert [message.content for message in engine.get_context(session_id=stored.sid).get_messages()] == ["PREVIOUS"]
    assert stored.session.values.get("context") == previous.get("context")
    assert not stored.session.disk


@pytest.mark.asyncio
async def test_missing_snapshot_reference_does_not_fail_an_already_committed_window(stored, monkeypatch):
    from jiuwenswarm.server.runtime.session import context_snapshot as snapshots

    engine = ContextEngine()
    await engine.create_context(session=stored.session, history_messages=[UserMessage(content="COMMITTED")])
    monkeypatch.setattr(snapshots, "prepare_context_snapshot", AsyncMock(side_effect=lambda **kwargs: kwargs["states"]))
    assert await snapshots.save_context_snapshot(context_engine=engine, session=stored.session)
    assert stored.session.disk["context"]["default_context_id"]["messages"][0].content == "COMMITTED"


@pytest.mark.asyncio
async def test_legacy_warmup_does_not_install_an_append_cursor(stored, monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service as service

    stored.path.unlink()
    legacy = stored.path.with_suffix(".json")
    legacy.write_text(json.dumps([user("legacy", "OLD INPUT")]), encoding="utf-8")
    monkeypatch.setattr(service, "resolve_live_agent_session", lambda *_args: stored.session)
    engine = ContextEngine()
    assert await service.warmup_session_context(deep_agent=_agent(engine), session_id=stored.sid)
    context = engine.get_context(session_id=stored.sid)
    assert context.duplex_history_cursor is None
    assert context.history_recovery_cursor.path == legacy
    assert [message.content for message in context.get_messages()] == ["OLD INPUT"]


@pytest.mark.asyncio
async def test_unexpected_history_io_failure_has_the_restore_error_contract(stored, monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service as service

    stored.write([user("old")])
    monkeypatch.setattr(service, "resolve_live_agent_session", lambda *_args: stored.session)
    monkeypatch.setattr(service, "read_history_replay", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unreadable")))
    with pytest.raises(replay.HistoryReplayError) as error:
        await service.warmup_session_context(deep_agent=_agent(ContextEngine()), session_id=stored.sid)
    assert error.value.code == "HISTORY_RESTORE_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
async def test_warmup_rolls_back_and_preserves_process_control_exceptions(stored, monkeypatch, signal):
    from jiuwenswarm.agents.harness.common import session_ops_service as service

    stored.write([user("old")])
    monkeypatch.setattr(service, "resolve_live_agent_session", lambda *_args: stored.session)
    engine = SimpleNamespace(get_context=lambda **_kwargs: None, create_context=AsyncMock(side_effect=signal()))
    with pytest.raises(signal):
        await service.warmup_session_context(deep_agent=_agent(engine), session_id=stored.sid)
    assert stored.session.get_state("context") is None


@pytest.mark.asyncio
async def test_voice_import_accepts_a_reader_without_a_durable_cursor(stored, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend import conversation_context as voice

    engine = ContextEngine()
    context = await engine.create_context(session=stored.session)
    monkeypatch.setattr(voice, "_load_voice_batch", AsyncMock(return_value=SimpleNamespace(records=[], cursor=None)))
    await voice.sync_conversation_context(_agent(engine), stored.sid)
    assert context.duplex_history_cursor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
async def test_voice_import_rolls_back_and_preserves_process_control_exceptions(stored, monkeypatch, signal):
    from jiuwenswarm.agents.harness.common import session_ops_service as service
    from jiuwenswarm.extensions.video_duplex.backend import conversation_context as voice

    engine = ContextEngine()
    context = await engine.create_context(session=stored.session, history_messages=[UserMessage(content="PRIOR")])
    speech = {**user("spoken", "NEW"), "channel_id": "video_duplex", "id": "spoken"}
    monkeypatch.setattr(voice, "_load_voice_batch", AsyncMock(return_value=SimpleNamespace(records=[speech], cursor=None)))
    monkeypatch.setattr(service, "resolve_live_agent_session", lambda *_args: stored.session)
    monkeypatch.setattr(context, "add_messages", AsyncMock(side_effect=signal()))
    with pytest.raises(signal):
        await voice.sync_conversation_context(_agent(engine), stored.sid)
    assert [message.content for message in context.get_messages()] == ["PRIOR"]
    assert "spoken" not in getattr(context, "duplex_history_ids", set())


def test_empty_legacy_file_reads_and_converts_as_empty_history(stored):
    stored.path.unlink()
    legacy = stored.path.with_suffix(".json")
    legacy.touch()
    assert replay.read_history_replay(stored.sid).records == []
    assert replay.read_history_for_rewrite(legacy) == []
    streaming.convert_legacy_jsonl(stored.sid, legacy, stored.path)
    assert stored.path.read_bytes() == b""
    assert legacy.read_bytes() == b""


def test_deleting_consumed_history_invalidates_the_restore_cursor(stored):
    stored.write([user("old")])
    cursor = replay.read_history_replay(stored.sid).cursor
    stored.path.unlink()
    with pytest.raises(replay.HistoryReplayError) as error:
        replay.read_history_replay(stored.sid, cursor=cursor)
    assert error.value.code == "HISTORY_RESTORE_CHANGED"


def test_invalid_utf8_in_a_legacy_string_is_a_classified_restore_failure(stored):
    stored.path.write_bytes(b'{"role":"user","content":"\xff"}\n')
    with pytest.raises(replay.HistoryReplayError) as error:
        list(streaming.iter_history_rows(stored.path, session_id=stored.sid))
    assert error.value.code == "HISTORY_RESTORE_FAILED"


@pytest.mark.parametrize("source_format", ["json", "jsonl"])
def test_legacy_append_can_preserve_large_history_in_bounded_space(stored, monkeypatch, source_format):
    monkeypatch.setenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", "1")
    stored.path.unlink()
    source = stored.path.with_suffix("." + source_format)
    row = json.dumps(user("old", "x" * 2048)).encode()
    with source.open("wb") as stream:
        if source_format == "json":
            stream.write(b"[")
        for index in range(8300):
            if index and source_format == "json":
                stream.write(b",")
            stream.write(row + (b"\n" if source_format == "jsonl" else b""))
        if source_format == "json":
            stream.write(b"]")
    tracemalloc.start()
    try:
        history._write_item(stored.sid, user("new", "NEW INPUT"))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    records = json.loads(stored.path.with_suffix(".json").read_bytes())
    assert len(records) == 8301 and records[-1] == user("new", "NEW INPUT")
    assert records[0] == user("old", "x" * 2048)
    assert peak < 4 * 1024 * 1024


def test_failed_legacy_bootstrap_is_reported_once_and_retried_after_repair(stored, monkeypatch, caplog):
    monkeypatch.delenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", raising=False)
    stored.path.unlink()
    legacy = stored.path.with_suffix(".json")
    legacy.write_bytes(b'[{"content":"broken')
    original = streaming.convert_legacy_jsonl
    attempts = []

    def convert(*args):
        attempts.append(args)
        return original(*args)

    monkeypatch.setattr(streaming, "convert_legacy_jsonl", convert)
    for _ in range(2):
        with pytest.raises(replay.HistoryReplayError):
            history._write_item(stored.sid, user("new"))
    assert len(attempts) == 1
    assert not stored.path.exists() and legacy.read_bytes() == b'[{"content":"broken'
    assert any(record.levelname == "ERROR" and "HISTORY_RESTORE_FAILED" in record.message for record in caplog.records)
    legacy.write_text(json.dumps([user("repaired")]), encoding="utf-8")
    history._write_item(stored.sid, user("new"))
    assert len(attempts) == 2
    assert [json.loads(line)["request_id"] for line in stored.path.read_bytes().splitlines()] == ["repaired", "new"]


@pytest.mark.parametrize("failure", ["corrupt", "io"])
def test_failed_legacy_rewrite_preserves_original_and_removes_temporary_files(stored, monkeypatch, failure):
    source = stored.path.with_suffix(".json")
    original = json.dumps([user("old")]).encode()
    if failure == "corrupt":
        original = original[:-1] + b",broken]"
    else:
        def failed_copy(*_args):
            yield b'{"partial":'
            raise OSError("controlled write interruption")

        monkeypatch.setattr(streaming, "_range_chunks", failed_copy)
    source.write_bytes(original)
    with pytest.raises(replay.HistoryReplayError if failure == "corrupt" else OSError):
        streaming.rewrite_legacy_json(stored.sid, source, source, append_record=user("new"))
    assert source.read_bytes() == original
    assert not list(source.parent.glob(".*.tmp"))


def test_streaming_legacy_error_dedup_preserves_unrelated_records(stored, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", "1")
    stored.path.unlink()
    source = stored.path.with_suffix(".json")
    error = {"role": "assistant", "request_id": "failed", "cron_failure_record": True, "content": "FIRST"}
    rows = [user("old"), error]
    source.write_text(json.dumps(rows), encoding="utf-8")
    history._write_item(stored.sid, {**error, "content": "DUPLICATE"})
    assert json.loads(source.read_bytes()) == rows


@pytest.mark.parametrize("failure", [OSError, RuntimeError])
def test_fork_artifact_copy_failure_aborts_and_removes_the_target(stored, monkeypatch, failure):
    from jiuwenswarm.agents.harness.common import session_ops_service as service

    ref = artifacts.put_text(stored.sid, "ORIGINAL")
    source = {**user("old"), "payload_refs": {"prompt": ref}}
    monkeypatch.setattr(service, "get_agent_sessions_dir", lambda: stored.path.parent.parent)
    monkeypatch.setattr(service, "load_history_records", lambda *_args: [source])
    monkeypatch.setattr(service, "flush_history_writes", lambda: None)
    monkeypatch.setattr(artifacts, "put_chunks", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure("copy failed")))
    with pytest.raises(artifacts.ArtifactCopyError):
        service.fork_session(source_session_id=stored.sid, target_session_id="forked")
    assert not (stored.path.parent.parent / "forked").exists()
    assert artifacts.read_artifact_chunk(stored.sid, ref["sha256"])["content"] == "ORIGINAL"


def test_recovery_budget_is_shared_by_concurrent_event_loops():
    from jiuwenswarm.server.runtime.session.recovery_budget import admit_context_recovery

    barrier = threading.Barrier(3)
    lock = threading.Lock()
    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        barrier.wait(timeout=5)
        async with admit_context_recovery():
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                await asyncio.sleep(.08)
            finally:
                with lock:
                    active -= 1

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(lambda: asyncio.run(worker())) for _ in range(3)]
        for future in futures:
            future.result(timeout=10)
    assert active == 0 and peak == 2


@pytest.mark.asyncio
async def test_call_lifecycle_does_not_write_prompt_artifacts(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
    from tests.unit_tests.agentserver.test_stream_event_rail_wire_budget import _StreamSession

    session = _StreamSession()
    callback = SimpleNamespace(session=session, context=None, extra={},
                               inputs=SimpleNamespace(messages=[UserMessage(content="QUESTION " * 2000)], tools=[]))
    put_prompt = Mock(side_effect=OSError("disk full"))
    monkeypatch.setattr(artifacts, "put_prompt", put_prompt)
    rail = JiuSwarmStreamEventRail()
    await rail.before_model_call(callback)
    assert len(session.events) == 1 and session.events[0].type == "llm_call_start"
    payload = session.events[0].payload
    assert "prompt" not in payload
    assert "payload_refs" not in payload
    await rail._emit_llm_call_signal(callback, "llm_call_end")
    assert len(session.events) == 2 and session.events[1].type == "llm_call_end"
    assert "payload_refs" not in session.events[1].payload
    put_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_final_model_input_is_preserved_without_prompt_artifact(stored, monkeypatch):
    from openjiuwen.core.foundation.llm import SystemMessage
    from jiuwenswarm.agents.harness.common.rails import stream_event_rail as rail_module
    from tests.unit_tests.agentserver.test_stream_event_rail_wire_budget import _StreamSession

    session = _StreamSession()
    monkeypatch.setattr(session, "get_session_id", lambda: stored.sid)
    monkeypatch.setattr(rail_module, "record_model_token_usage", AsyncMock())
    rail = rail_module.JiuSwarmStreamEventRail()
    put_prompt = Mock()
    monkeypatch.setattr(artifacts, "put_prompt", put_prompt)
    monkeypatch.setattr(rail, "_emit_context_usage", AsyncMock())
    callback = SimpleNamespace(session=session, context=None, extra={}, inputs=SimpleNamespace(
        messages=[UserMessage(content="PREVIEW")], tools=[], response=None,
    ))
    await rail.before_model_call(callback)
    assert "payload_refs" not in session.events[0].payload

    final_messages = [SystemMessage(content="FINAL SYSTEM RULES"), UserMessage(content="FINAL QUESTION")]
    callback.inputs.messages = final_messages
    callback.inputs.tools = [{"name": "final_tool", "parameters": {"type": "object"}}]
    await rail.after_model_call(callback)
    assert [event.type for event in session.events] == ["llm_call_start", "llm_call_end"]
    assert all("prompt" not in event.payload and "payload_refs" not in event.payload for event in session.events)
    assert callback.inputs.messages == final_messages
    assert callback.inputs.tools == [{"name": "final_tool", "parameters": {"type": "object"}}]
    put_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_tool_storage_failure_still_sends_a_bounded_result(monkeypatch):
    from tests.unit_tests.agentserver.test_stream_event_rail_wire_budget import _emit, _wire_bytes

    monkeypatch.setattr(artifacts, "externalize_payload", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    result = {"result": "TOOL ORIGINAL " * 100000, "mcp_app": {"resourceUri": "ui://tool/view"}}
    payload = (await _emit(result, rendered_result=result["result"]))["tool_result"]
    assert payload["tool_call_id"] == "tc-mcp"
    assert payload["raw_output"] == {"mcp_app": result["mcp_app"]}
    assert payload["raw_output_truncated"] is True
    assert "TOOL ORIGINAL" in payload["rendered_result"]
    assert "unavailable" in payload["rendered_result"] and "payload_refs" not in payload
    assert _wire_bytes(payload) < 128 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_session", [False, True])
async def test_artifact_get_uses_the_same_session_resolution_as_history(stored, explicit_session):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.handlers.session import handle_history_get

    ref = artifacts.put_text(stored.sid, "RETRIEVED ORIGINAL")
    params = {"artifact_sha256": ref["sha256"]}
    if explicit_session:
        params["session_id"] = stored.sid
    request = AgentRequest(request_id="artifact-get", channel_id="web", params=params,
                           session_id="unused" if explicit_session else stored.sid, is_stream=False)
    sink = SimpleNamespace(send_response=AsyncMock())
    await handle_history_get(SimpleNamespace(request=request, sink=sink))
    response = sink.send_response.call_args.args[0]
    assert response.ok and response.payload["content"] == "RETRIEVED ORIGINAL"


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_phase", ["start", "end", "usage_before_end"])
async def test_llm_calls_do_not_copy_prompt_or_artifact_into_usage(monkeypatch, reference_phase):
    from tests.unit_tests.agentserver.test_deep_adapter_stream_end_final import _adapter_ready, _run_stream, _usage_chunk

    refs = {"prompt": {"sha256": "a" * 64, "bytes": 12, "path": "unused"}}
    start = {"prompt": "FIRST"}
    if reference_phase == "start":
        start["payload_refs"] = refs
    chunks = [SimpleNamespace(type="llm_call_start", payload=start)]
    if reference_phase == "end":
        chunks.append(SimpleNamespace(type="llm_call_end", payload={"payload_refs": refs}))
    chunks.append(_usage_chunk())
    if reference_phase == "usage_before_end":
        chunks.append(SimpleNamespace(type="llm_call_end", payload={"payload_refs": refs}))
    chunks.extend([
        SimpleNamespace(type="llm_call_start", payload={"prompt": "SECOND"}),
        _usage_chunk(),
    ])
    events = await _run_stream(_adapter_ready(monkeypatch), chunks)
    usage = [event["metadata"]["usage_metadata"] for event in events if event.get("event_type") == "chat.usage_metadata"]
    assert len(usage) == 2
    assert all("prompt" not in item and "payload_refs" not in item for item in usage)
    calls = [event for event in events if event.get("event_type") in {"chat.llm_call_start", "chat.llm_call_end"}]
    assert sum(event["event_type"] == "chat.llm_call_start" for event in calls) == 2
    assert all("prompt" not in item and "payload_refs" not in item for item in calls)



@pytest.mark.asyncio
@pytest.mark.parametrize("usage_first", [False, True])
async def test_usage_is_delivered_once_after_call_end_in_either_sdk_order(monkeypatch, usage_first):
    from tests.unit_tests.agentserver.test_deep_adapter_stream_end_final import _adapter_ready, _run_stream, _usage_chunk

    preview = {"prompt": {"sha256": "a" * 64, "bytes": 12, "path": "preview"}}
    final = {"prompt": {"sha256": "b" * 64, "bytes": 24, "path": "final"}}
    end = SimpleNamespace(type="llm_call_end", payload={"payload_refs": final})
    ordered = [_usage_chunk(), end] if usage_first else [end, _usage_chunk()]
    chunks = [SimpleNamespace(type="llm_call_start", payload={"prompt": "PREVIEW", "payload_refs": preview})]
    events = await _run_stream(_adapter_ready(monkeypatch), chunks + ordered)
    types = [event.get("event_type") for event in events]
    assert types.count("chat.usage_metadata") == 1
    assert types.index("chat.llm_call_end") < types.index("chat.usage_metadata")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["next_call", "answer", "stream_end"])
async def test_usage_without_call_end_is_delivered_once_at_the_next_boundary(monkeypatch, boundary):
    from tests.unit_tests.agentserver.test_deep_adapter_stream_end_final import _adapter_ready, _run_stream, _usage_chunk

    chunks = [SimpleNamespace(type="llm_call_start", payload={"prompt": "FIRST"}), _usage_chunk()]
    if boundary == "next_call":
        chunks.append(SimpleNamespace(type="llm_call_start", payload={"prompt": "SECOND"}))
    elif boundary == "answer":
        chunks.append(SimpleNamespace(type="answer", payload={
            "output": {"output": "ANSWER", "chunked": False}, "result_type": "answer",
        }))
    events = await _run_stream(_adapter_ready(monkeypatch), chunks)
    types = [event.get("event_type") for event in events]
    usage = [event for event in events if event.get("event_type") == "chat.usage_metadata"]
    assert len(usage) == 1 and usage[0]["metadata"]["usage_metadata"]["total_tokens"] == 12
    assert "payload_refs" not in usage[0]["metadata"]["usage_metadata"]
    if boundary == "next_call":
        assert types.index("chat.usage_metadata") < len(types) - 1 - types[::-1].index("chat.llm_call_start")
    else:
        assert types.index("chat.usage_metadata") < types.index("chat.final")


@pytest.mark.parametrize("same_session", [False, True])
def test_indexed_scan_allows_writes_after_capturing_committed_boundary(stored, monkeypatch, same_session):
    from jiuwenswarm.server.control.history_service import load_history_page
    from jiuwenswarm.server.runtime.session import history_paging as paging, lifecycle

    monkeypatch.delenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", raising=False)
    stored.write([user("old", "committed input")])
    target = stored.sid if same_session else "other-session"
    if not same_session:
        lifecycle.claim_runtime(target)
    entered, release, writer_started = threading.Event(), threading.Event(), threading.Event()
    snapshots = []
    original = paging.read_indexed_history

    def paused_scan(*args, **kwargs):
        snapshots.append(kwargs.get("source_stat"))
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)

    def write():
        writer_started.set()
        history.append_history_record(
            session_id=target, request_id="during-scan", channel_id="web", role="user",
            content="new input", timestamp=1.0,
        )
        history.flush_history_writes()

    monkeypatch.setattr(paging, "read_indexed_history", paused_scan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        page = pool.submit(load_history_page, stored.sid, 1)
        try:
            assert entered.wait(3)
            written = pool.submit(write)
            assert writer_started.wait(3)
            written.result(timeout=5)
        finally:
            release.set()
        result = page.result(timeout=5)
    assert snapshots[0] is not None
    assert [row["request_id"] for row in result["messages"]] == ["old"]
    assert history.load_history_records(target)[-1]["request_id"] == "during-scan"
    if same_session:
        assert load_history_page(stored.sid, 1)["messages"][0]["request_id"] == "during-scan"
