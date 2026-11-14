"""Recovery reads the installed window; audit projections are not checkpoint authority."""
import copy
import json
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage

from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
from jiuwenswarm.server.runtime.session import history_artifacts as artifacts
from jiuwenswarm.server.runtime.session import history_stream as streaming
from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session import session_history as history
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError


async def native_session(stored, monkeypatch):
    from openjiuwen.core.session.agent import create_agent_session
    from openjiuwen.core.session.checkpointer import CheckpointerFactory
    from openjiuwen.core.session.checkpointer.inmemory import InMemoryCheckpointer
    from openjiuwen.core.single_agent import AgentCard
    from jiuwenswarm.runtime.context_checkpointer import ContextCheckpointer

    class RecordingCheckpointer(InMemoryCheckpointer):
        def __init__(self):
            super().__init__()
            self.disk = {}
            self.fail = False

        async def post_agent_execute(self, session):
            if self.fail:
                raise OSError("controlled commit failure")
            await super().post_agent_execute(session)
            self.disk = copy.deepcopy(session.state().get_global(None))

        async def interrupt_agent_execute(self, session):
            if self.fail:
                raise OSError("controlled commit failure")
            await super().interrupt_agent_execute(session)
            self.disk = copy.deepcopy(session.state().get_global(None))

    delegate = RecordingCheckpointer()
    checkpointer = ContextCheckpointer(delegate)
    monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", checkpointer)
    session = create_agent_session(session_id=stored.sid, card=AgentCard(id="test-agent", name="test"))
    await session.pre_run(inputs=None)
    stored.session = session
    return checkpointer, delegate


class Session:
    def __init__(self, sid):
        self.sid = sid
        self.values = {}
        self.disk = {}

    def get_session_id(self):
        return self.sid

    def get_agent_id(self):
        return "test-agent"

    def get_state(self, key):
        return self.values.get(key)

    def update_state(self, value):
        self.values.update(value)

    async def commit(self):
        self.disk = copy.deepcopy(self.values)


@pytest.fixture
def stored(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    for module in (lc, history, artifacts, snapshots):
        monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lc, "get_agent_root_dir", lambda: tmp_path)
    sid = "snapshot-test"
    lc.claim_runtime(sid)
    path = history.get_write_history_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    def write(rows):
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    return SimpleNamespace(sid=sid, path=path, write=write, session=Session(sid))


def user(rid, content="question"):
    return {"role": "user", "request_id": rid, "channel_id": "web", "content": content}


def final(rid, content="answer"):
    return {"role": "assistant", "request_id": rid, "channel_id": "web", "content": content,
            "event_type": "chat.final"}


async def save(stored, messages):
    engine = ContextEngine()
    await engine.create_context(session=stored.session, history_messages=messages)
    assert await snapshots.save_context_snapshot(context_engine=engine, session=stored.session,
                                                 consumed_request_ids={"old"})
    return engine


@pytest.mark.asyncio
async def test_restart_preserves_exact_compacted_window_and_tool_pairs(stored):
    stored.write([user("old", "ORIGINAL_MUST_NOT_RETURN"), final("old", "RAW_OLD_ANSWER")])
    messages = [UserMessage(content="COMPACTED_EFFECTIVE_MEMORY"),
                AssistantMessage(content="", tool_calls=[{"type": "function", "id": "tc", "name": "read", "arguments": "{}"}]),
                ToolMessage(content="retained tool result", tool_call_id="tc"),
                UserMessage(content="retained latest request")]
    await save(stored, messages)
    reference = stored.session.disk[snapshots.SNAPSHOT_KEY]
    restored = ContextEngine()
    stored.session.values = copy.deepcopy(stored.session.disk)
    assert await snapshots.restore_checkpoint_context(context_engine=restored, session=stored.session,
                                                      processors=[], before_request_id="new")
    actual = restored.get_context(session_id=stored.sid).get_messages()
    assert [item.content for item in actual] == [item.content for item in messages]
    assert isinstance(actual[1], AssistantMessage) and actual[1].tool_calls[0].id == "tc"
    assert isinstance(actual[2], ToolMessage) and actual[2].tool_call_id == "tc"
    assert "ORIGINAL" not in str(actual)
    assert reference["bytes"] < 65536


@pytest.mark.asyncio
async def test_queued_input_is_not_covered_and_late_output_is_not_duplicated(stored):
    stored.write([user("old"), user("queued", "QUEUED_INPUT"), final("old", "late output")])
    await save(stored, [UserMessage(content="question"), AssistantMessage(content="late output")])
    with stored.path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(final("queued", "QUEUED_REPLY")) + "\n")
        stream.write(json.dumps(user("new", "CURRENT_REQUEST")) + "\n")
    restored = ContextEngine()
    assert await snapshots.restore_checkpoint_context(context_engine=restored, session=stored.session,
                                                      processors=[], before_request_id="new")
    contents = [message.content for message in restored.get_context(session_id=stored.sid).get_messages()]
    assert contents == ["question", "late output", "QUEUED_INPUT", "QUEUED_REPLY"]


@pytest.mark.asyncio
async def test_failed_commit_never_adopts_an_orphan_snapshot(stored):
    stored.write([user("old"), final("old")])
    engine = await save(stored, [UserMessage(content="COMMITTED")])
    original = copy.deepcopy(stored.session.disk)
    context = engine.get_context(session_id=stored.sid)
    context.set_messages([UserMessage(content="UNCOMMITTED")])
    stored.session.commit = AsyncMock(side_effect=OSError("injected checkpoint failure"))
    with pytest.raises(OSError, match="injected"):
        await snapshots.save_context_snapshot(context_engine=engine, session=stored.session,
                                               consumed_request_ids={"old"})
    assert stored.session.disk == original
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, original[snapshots.SNAPSHOT_KEY])
    assert loaded.contexts["default_context_id"]["messages"][0].content == "COMMITTED"
    assert len(list(snapshots.snapshot_directory(stored.sid).glob("*.json"))) >= 3


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "checksum", "wrong_session", "truncated"])
async def test_invalid_committed_snapshot_fails_closed(stored, damage):
    stored.write([user("old")])
    await save(stored, [UserMessage(content="saved")])
    ref = stored.session.get_state(snapshots.SNAPSHOT_KEY)
    path = snapshots.snapshot_directory(stored.sid) / ref["file"]
    if damage == "missing":
        path.unlink()
    elif damage == "checksum":
        path.write_bytes(path.read_bytes().replace(b"saved", b"other"))
    elif damage == "wrong_session":
        value = json.loads(path.read_bytes())
        value["session_id"] = "another-session"
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        import hashlib
        ref.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    else:
        path.write_bytes(b'{"version":')
    with pytest.raises(HistoryReplayError, match="missing or invalid"):
        snapshots.load_checkpoint_snapshot(stored.sid, ref)


@pytest.mark.asyncio
async def test_history_replacement_invalidates_old_context(stored):
    stored.write([user("old")])
    await save(stored, [UserMessage(content="stale")])
    ref = stored.session.get_state(snapshots.SNAPSHOT_KEY)
    history._write_records_to_path(stored.path, [user("rewound")])
    assert snapshots.load_checkpoint_snapshot(stored.sid, ref) is None
    assert not snapshots.has_snapshot_hint(stored.sid)


def test_giant_prompt_is_skipped_with_bounded_allocations(stored):
    with stored.path.open("wb") as stream:
        stream.write(b'{"role":"assistant","prompt":"')
        for _ in range(256):
            stream.write(b"x" * 65536)
        stream.write(b'","event_type":"chat.llm_call_start"}\n')
        stream.write(json.dumps(user("old", "semantic input")).encode() + b"\n")
    tracemalloc.start()
    rows = list(streaming.iter_history_rows(stored.path, session_id=stored.sid))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert rows[1].record["content"] == "semantic input"
    assert "prompt" not in rows[0].record
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("source_format", ["json", "jsonl"])
def test_context_projection_keeps_diagnostic_omission_bounded(stored, source_format):
    path = stored.path.with_suffix("." + source_format)
    record = {"role": "assistant", "event_type": "chat.tool_result", "rendered_result": "context result",
              "event_payload": {"duplicate_audit": "x" * (5 * 1024 * 1024)},
              "usage": {"total_tokens": 168}, "session_usage": {"total_tokens": 336}}
    path.write_text(json.dumps([record] if source_format == "json" else record), encoding="utf-8")
    tracemalloc.start()
    try:
        rows = list(streaming.iter_history_rows(path, session_id=stored.sid))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [row.record for row in rows] == [
        {"role": "assistant", "event_type": "chat.tool_result", "rendered_result": "context result"},
    ]
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("source_format", ["json", "jsonl"])
def test_streaming_scan_budget_pauses_for_consumer(stored, monkeypatch, source_format):
    rows = [user("first", "first question"), user("second", "second question")]
    path = stored.path.with_suffix("." + source_format)
    text = json.dumps(rows) if source_format == "json" else "".join(json.dumps(row) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(streaming, "time", SimpleNamespace(monotonic=lambda: clock.now))
    iterator = streaming.iter_history_rows(path, session_id=stored.sid, max_seconds=1)
    first = next(iterator)
    clock.now += 10
    remaining = list(iterator)
    assert [first.record["content"], *[row.record["content"] for row in remaining]] == [
        "first question", "second question",
    ]


@pytest.mark.parametrize("source_format", ["json", "jsonl"])
def test_streaming_scan_budget_still_limits_active_io(stored, monkeypatch, source_format):
    rows = [user("first", "question")]
    path = stored.path.with_suffix("." + source_format)
    text = json.dumps(rows) if source_format == "json" else json.dumps(rows[0]) + "\n"
    path.write_text(text, encoding="utf-8")
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(streaming, "time", SimpleNamespace(monotonic=lambda: clock.now))
    original_open = Path.open

    class TimedFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            clock.now += .6
            return self.stream.read(size)

    def timed_open(opened_path, mode="r", *args, **kwargs):
        stream = original_open(opened_path, mode, *args, **kwargs)
        return TimedFile(stream) if opened_path == path and mode == "rb" else stream

    monkeypatch.setattr(Path, "open", timed_open)
    with pytest.raises(HistoryReplayError, match="time budget exceeded") as error:
        list(streaming.iter_history_rows(path, session_id=stored.sid, max_seconds=1))
    assert error.value.code == "HISTORY_RESTORE_LIMIT"


@pytest.mark.parametrize("payload", [b'[{"role":"user"},]', b'{"role":"user","x":"bad\\q"}',
                                    b'{"role":"user","x": [1,]}', b'{"role":"user","role":"assistant"}',
                                    b'{"x":"\\uZZZZ"}', b'{"x":"unterminated'])
def test_streaming_migration_rejects_corrupt_history(stored, payload):
    path = stored.path.with_suffix(".json") if payload.startswith(b"[") else stored.path
    path.write_bytes(payload)
    with pytest.raises(HistoryReplayError):
        list(streaming.iter_history_rows(path, session_id=stored.sid))


def test_large_legacy_array_conversion_preserves_original(stored):
    legacy = stored.path.with_suffix(".json")
    rows = [user("old", "中文🙂"), {"role": "assistant", "event_type": "chat.llm_call_start", "prompt": "x" * 5_000_000}]
    legacy.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    original = legacy.read_bytes()
    stored.path.unlink()
    streaming.convert_legacy_jsonl(stored.sid, legacy, stored.path)
    assert legacy.read_bytes() == original
    assert [json.loads(line) for line in stored.path.read_bytes().splitlines()] == rows


def test_artifact_retrieval_returns_every_byte_and_preserves_unicode(stored):
    text = ('中文🙂"\\\n' * 30000)
    payload = artifacts.externalize_payload(stored.sid, {"rendered_result": text}, fields=["rendered_result"])
    ref = payload["payload_refs"]["rendered_result"]
    assert len(payload["rendered_result"]) < 4096
    offset = 0
    parts = []
    while True:
        chunk = artifacts.read_artifact_chunk(stored.sid, ref["sha256"], offset=offset, max_bytes=4096)
        parts.append(chunk["content"])
        assert chunk["next_offset"] > offset
        offset = chunk["next_offset"]
        if chunk["eof"]:
            break
    assert "".join(parts) == text
    assert offset == ref["bytes"]


@pytest.mark.parametrize("digest,offset,budget", [("../private", 0, 1024), ("a" * 64, -1, 1024),
                                                    ("a" * 64, 0, 1000000), ("a" * 64, True, 100)])
def test_artifact_reader_rejects_unbounded_or_unscoped_queries(stored, digest, offset, budget):
    with pytest.raises(ValueError):
        artifacts.read_artifact_chunk(stored.sid, digest, offset=offset, max_bytes=budget)


def test_full_prompt_contains_tool_calls_and_full_tool_schema(stored):
    messages = [AssistantMessage(content="", tool_calls=[{"type": "function", "id": "tc", "name": "read", "arguments": '{"path":"a"}'}]),
                ToolMessage(tool_call_id="tc", content="TOOL_CONTENT" * 20000)]
    tools = [{"name": "read", "parameters": {"description": "SCHEMA_MARKER"}}]
    ref = artifacts.put_prompt(stored.sid, messages, tools)
    value = json.loads(Path(ref["path"]).read_bytes())
    assert value["messages"][0]["tool_calls"][0]["id"] == "tc"
    assert value["messages"][1]["content"] == messages[1].content
    assert value["tools"] == tools


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [False, True])
async def test_native_checkpoint_is_consistent_before_outer_after_invoke(stored, monkeypatch, commit):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="OLD_WINDOW")])
    old_disk = copy.deepcopy(delegate.disk)
    with stored.path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(user("pending", "NATIVE_INPUT")) + "\n")
    engine.get_context(session_id=stored.sid).set_messages([
        UserMessage(content="NATIVE_INPUT", metadata={"jiuwenswarm_request_ids": ["pending"]}),
        AssistantMessage(content="", tool_calls=[{"type": "function", "id": "approval", "name": "write", "arguments": "{}"}]),
    ])
    await engine.save_contexts(stored.session)
    stored.session.update_state({"pending_permission": {"tool_call_id": "approval", "nonce": "consistent"}})
    if commit:
        await stored.session.commit()
    disk = delegate.disk
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, disk[snapshots.SNAPSHOT_KEY])
    contents = [item.content for item in loaded.contexts["default_context_id"]["messages"]]
    assert contents == (["NATIVE_INPUT", ""] if commit else ["OLD_WINDOW"])
    assert (disk.get("pending_permission") is not None) == commit
    if not commit:
        assert disk == old_disk


@pytest.mark.asyncio
async def test_committed_snapshot_rotation_is_bounded(stored):
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="window")])
    for _ in range(10):
        await snapshots.save_context_snapshot(context_engine=engine, session=stored.session)
    files = [path for path in snapshots.snapshot_directory(stored.sid).glob("*.json") if snapshots._NAME.fullmatch(path.name)]
    assert len(files) <= 4
    assert snapshots.load_checkpoint_snapshot(stored.sid, stored.session.disk[snapshots.SNAPSHOT_KEY]) is not None


@pytest.mark.asyncio
async def test_hint_without_checkpoint_reference_has_no_authority(stored):
    stored.write([user("old")])
    await save(stored, [UserMessage(content="ORPHAN_CANDIDATE")])
    stored.session.values = {}
    engine = ContextEngine()
    assert snapshots.has_snapshot_hint(stored.sid)
    assert not await snapshots.restore_checkpoint_context(context_engine=engine, session=stored.session,
                                                          processors=[], before_request_id="new")
    assert engine.get_context(session_id=stored.sid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_bounded_migration_keeps_tool_groups_and_rolls_back_on_failure(stored, monkeypatch, corrupt):
    from jiuwenswarm.server.runtime.session import context_migration as migration

    monkeypatch.setattr(migration, "_COMPRESS_AT_BYTES", 1024)
    monkeypatch.setattr(migration, "_MAX_CONTEXT_BYTES", 16384)
    engine = ContextEngine()
    previous = await engine.create_context(session=stored.session, history_messages=[UserMessage(content="PREVIOUS")])
    await engine.save_contexts(stored.session)
    await stored.session.commit()
    old_disk = copy.deepcopy(stored.session.disk)
    rows = []
    for index in range(10):
        rows.extend([
            user(f"turn-{index}", "x" * 500),
            {"role": "assistant", "event_type": "chat.tool_call", "tool_call": {
                "tool_call_id": f"tc-{index}", "name": "read", "arguments": "{}"}},
            {"role": "assistant", "event_type": "chat.tool_result", "tool_call_id": f"tc-{index}",
             "rendered_result": f"RESULT_{index}:" + "y" * 500},
        ])
    rows.append(user("current", "EXCLUDED_CURRENT"))
    stored.write(rows)
    if corrupt:
        raw = stored.path.read_bytes()
        raw = raw[:raw.rfind(b'{"role": "user"')] + b'{"broken": [}\n'
        stored.path.write_bytes(raw)
    original = stored.path.read_bytes()
    original_create = engine.create_context
    tool_ids = []
    windows = []

    async def create(**kwargs):
        target = await original_create(**kwargs)

        async def compress(**_kwargs):
            current = target.get_messages()
            calls = {call.id for message in current if isinstance(message, AssistantMessage)
                     for call in message.tool_calls or ()}
            results = [message.tool_call_id for message in current if isinstance(message, ToolMessage)]
            assert set(results) <= calls
            tool_ids.extend(results)
            windows.append(sum(len(message.content or "") for message in current))
            target.set_messages([UserMessage(content="MIGRATED_SUMMARY")])
            return "compressed"

        monkeypatch.setattr(target, "compress_context", compress)
        return target

    monkeypatch.setattr(engine, "create_context", create)
    if corrupt:
        with pytest.raises(HistoryReplayError):
            await migration.migrate_history_context(context_engine=engine, session=stored.session,
                                                      processors=[], before_request_id="current")
        assert stored.session.disk == old_disk
        assert engine.get_context(session_id=stored.sid).get_messages()[0].content == "PREVIOUS"
    else:
        assert await migration.migrate_history_context(context_engine=engine, session=stored.session,
                                                        processors=[], before_request_id="current")
        assert set(tool_ids) == {f"tc-{index}" for index in range(10)}
        assert len(tool_ids) == 10
        assert "EXCLUDED_CURRENT" not in str(engine.get_context(session_id=stored.sid).get_messages())
        assert stored.session.disk[snapshots.SNAPSHOT_KEY]
    assert windows and max(windows) < 16384
    assert stored.path.read_bytes() == original


@pytest.mark.asyncio
async def test_checkpoint_does_not_recollect_completed_request_metadata(stored, monkeypatch):
    from jiuwenswarm.common.schema.message import SESSION_REQUEST_COMPLETED_EVENT
    from jiuwenswarm.runtime.context_checkpointer import bind_context_checkpoint

    _, delegate = await native_session(stored, monkeypatch)

    rows = []
    messages = []
    for index in range(300):
        rid = f"completed-{index}"
        rows.extend([user(rid), {"role": "assistant", "request_id": rid, "event_type": SESSION_REQUEST_COMPLETED_EVENT}])
        messages.append(UserMessage(content="kept", metadata={"jiuwenswarm_request_ids": [rid]}))
    stored.write(rows)
    engine = ContextEngine()
    await engine.create_context(session=stored.session, history_messages=messages)
    bind_context_checkpoint(engine, stored.session)
    await engine.save_contexts(stored.session)
    await stored.session.commit()
    await engine.save_contexts(stored.session)
    await stored.session.commit()
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, delegate.disk[snapshots.SNAPSHOT_KEY])
    assert not loaded.covered_requests
    assert all(message.metadata["jiuwenswarm_recovery_recorded"]
               for message in delegate.disk["context"]["default_context_id"]["messages"])


def test_misaligned_utf8_artifact_offset_fails_instead_of_returning_empty_chunk(stored):
    ref = artifacts.put_text(stored.sid, "🙂")
    with pytest.raises(ValueError, match="aligned"):
        artifacts.read_artifact_chunk(stored.sid, ref["sha256"], offset=1, max_bytes=4)


def test_fork_payload_survives_parent_blob_deletion(stored):
    original = artifacts.externalize_payload(stored.sid, {"rendered_result": "完整内容🙂" * 20000}, fields=["rendered_result"])
    source_ref = original["payload_refs"]["rendered_result"]
    copied = artifacts.copy_history_artifacts(copy.deepcopy(original), stored.sid, "independent-fork")
    target_ref = copied["payload_refs"]["rendered_result"]
    Path(source_ref["path"]).unlink()
    assert Path(target_ref["path"]).read_text(encoding="utf-8") == "完整内容🙂" * 20000
    assert source_ref["path"] not in copied["rendered_result"]
    assert target_ref["path"] in copied["rendered_result"]


@pytest.mark.asyncio
async def test_history_handler_returns_a_bounded_artifact_chunk(stored):
    from jiuwenswarm.server.handlers.session import handle_history_get
    ref = artifacts.put_text(stored.sid, "完整日志🙂" * 20000)
    sink = SimpleNamespace(send_response=AsyncMock())
    ctx = SimpleNamespace(request=SimpleNamespace(params={"session_id": stored.sid, "artifact_sha256": ref["sha256"],
                                                         "offset": 0, "max_bytes": 4096},
                                                   is_stream=False, request_id="artifact-read", channel_id="web"),
                          sink=sink)
    await handle_history_get(ctx)
    response = sink.send_response.await_args.args[0]
    assert response.ok and response.payload["bytes"] == ref["bytes"]
    assert len(response.payload["content"].encode("utf-8")) <= 4096
    assert 0 < response.payload["next_offset"] <= 4096 and not response.payload["eof"]


@pytest.mark.asyncio
async def test_legacy_array_bookmark_keeps_unconsumed_input_once(stored):
    legacy = stored.path.with_suffix(".json")
    stored.path.unlink()
    legacy.write_text(json.dumps([user("old"), final("old"), user("queued", "UNCONSUMED")], indent=2), encoding="utf-8")
    await save(stored, [UserMessage(content="question"), AssistantMessage(content="answer")])
    restored = ContextEngine()
    assert await snapshots.restore_checkpoint_context(context_engine=restored, session=stored.session,
                                                      processors=[], before_request_id="next")
    actual = restored.get_context(session_id=stored.sid)
    assert [message.content for message in actual.get_messages()] == ["question", "answer", "UNCONSUMED"]
    assert actual.duplex_history_cursor is None


@pytest.mark.asyncio
async def test_legacy_voice_ids_survive_compaction_and_restart(stored):
    from jiuwenswarm.extensions.video_duplex.backend.conversation_context import sync_conversation_context
    legacy = stored.path.with_suffix(".json")
    stored.path.unlink()
    rows = [{**user("voice", "OLD_VOICE"), "channel_id": "video_duplex", "id": "voice:user"},
            {**final("voice", "OLD_REPLY"), "channel_id": "video_duplex", "id": "voice:assistant"}]
    legacy.write_text(json.dumps(rows), encoding="utf-8")
    engine = ContextEngine()
    context = await engine.create_context(session=stored.session, history_messages=[UserMessage(content="VOICE_SUMMARY")])
    context.duplex_history_ids = {"voice:user", "voice:assistant"}
    await snapshots.save_context_snapshot(context_engine=engine, session=stored.session)
    restored = ContextEngine()
    assert await snapshots.restore_checkpoint_context(context_engine=restored, session=stored.session,
                                                      processors=[], before_request_id="next")
    agent = SimpleNamespace(react_agent=SimpleNamespace(context_engine=restored), _interaction_session=stored.session)
    await sync_conversation_context(agent, stored.sid)
    assert [message.content for message in restored.get_context(session_id=stored.sid).get_messages()] == ["VOICE_SUMMARY"]


def test_tool_artifact_references_survive_adapter_normalization():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    ref = {"sha256": "a" * 64, "bytes": 100000, "media_type": "application/json"}
    chunk = SimpleNamespace(type="tool_result", payload={"tool_result": {
        "tool_call_id": "tc", "tool_name": "read", "result": "preview", "payload_refs": {"result": ref},
        "raw_output_truncated": True,
    }})
    converted = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert converted["payload_refs"]["result"] == ref
    assert converted["raw_output_truncated"] is True


@pytest.mark.asyncio
async def test_recovery_admission_limits_concurrency_and_releases_on_cancel():
    import asyncio
    from jiuwenswarm.server.runtime.session.recovery_budget import admit_context_recovery

    entered = []
    release = asyncio.Event()

    async def worker(index):
        async with admit_context_recovery():
            async with admit_context_recovery():
                entered.append(index)
                await release.wait()

    tasks = [asyncio.create_task(worker(index)) for index in range(3)]
    for _ in range(100):
        if len(entered) == 2:
            break
        await asyncio.sleep(.001)
    assert len(entered) == 2
    tasks[0].cancel()
    await asyncio.gather(tasks[0], return_exceptions=True)
    for _ in range(100):
        if len(entered) == 3:
            break
        await asyncio.sleep(.001)
    assert len(entered) == 3
    release.set()
    await asyncio.gather(*tasks[1:])


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_restore_old_sdk_window_into_empty_history(stored):
    stored.write([user("old")])
    await save(stored, [UserMessage(content="OLD_MUST_NOT_RETURN")])
    history._write_records_to_path(stored.path, [])
    engine = ContextEngine()
    assert not await snapshots.restore_checkpoint_context(context_engine=engine, session=stored.session,
                                                          processors=[], before_request_id="new")
    assert stored.session.get_state("context") is None
    assert stored.session.get_state(snapshots.SNAPSHOT_KEY) is None


@pytest.mark.asyncio
async def test_empty_native_context_pool_preserves_loaded_checkpoint(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="KEEP_CHECKPOINT")])
    ref = copy.deepcopy(delegate.disk[snapshots.SNAPSHOT_KEY])
    await engine.clear_context(session_id=stored.sid)
    await engine.save_contexts(stored.session)
    await stored.session.commit()
    assert delegate.disk[snapshots.SNAPSHOT_KEY] == ref
    assert delegate.disk["context"]["default_context_id"]["messages"][0].content == "KEEP_CHECKPOINT"


@pytest.mark.asyncio
async def test_summary_only_legacy_history_does_not_silently_start_empty(stored):
    from jiuwenswarm.server.runtime.session.context_migration import migrate_history_context
    stored.write([{"role": "assistant", "event_type": "context.compact_summary", "content": "UNVERIFIED_AUDIT"}])
    with pytest.raises(HistoryReplayError, match="Only audit summaries remain"):
        await migrate_history_context(context_engine=ContextEngine(), session=stored.session,
                                       processors=[], before_request_id="current")
    assert not stored.session.disk


@pytest.mark.asyncio
async def test_diagnostic_only_legacy_history_preserves_native_checkpoint_window(stored):
    from jiuwenswarm.server.runtime.session.context_migration import migrate_history_context
    engine = ContextEngine()
    await engine.create_context(session=stored.session, history_messages=[UserMessage(content="SDK_ONLY_MEMORY")])
    await engine.save_contexts(stored.session)
    await engine.clear_context(session_id=stored.sid)
    stored.write([{"role": "assistant", "event_type": "chat.llm_call_start", "prompt": "diagnostic only"}])
    assert await migrate_history_context(context_engine=engine, session=stored.session,
                                         processors=[], before_request_id="current")
    assert [message.content for message in engine.get_context(session_id=stored.sid).get_messages()] == ["SDK_ONLY_MEMORY"]


def test_cursor_identity_accepts_legacy_and_modern_formats(stored):
    stored.path.write_text(json.dumps(user("old")) + "\n", encoding="utf-8")
    stat = stored.path.stat()
    current = snapshots._identity(stored.path)
    offset = stat.st_size
    payload = {"file": stored.path.name, "generation": 0, "offset": offset,
               "anchor": snapshots._anchor(stored.path, offset)}
    legacy = dict(payload, identity=[stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, 123456789])
    modern = dict(payload, identity=list(current))
    assert snapshots._read_cursor(stored.sid, legacy).identity == current
    assert snapshots._read_cursor(stored.sid, modern).identity == current
    for broken in ([stat.st_dev, stat.st_ino, stat.st_size],
                   [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, 1, 2],
                   [stat.st_dev, stat.st_ino, stat.st_size, "mtime"]):
        with pytest.raises(HistoryReplayError, match="Invalid context snapshot boundary"):
            snapshots._read_cursor(stored.sid, dict(payload, identity=broken))
