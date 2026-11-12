"""Model restore is bounded, complete, and independent of audit-history size."""
from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from jiuwenswarm.server.runtime.session import history_replay as replay
from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session import session_history as history


@pytest.fixture
def stored(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(lc, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lc, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: sessions)
    sid = "bounded-replay"
    lc.claim_runtime(sid)
    path = history.get_write_history_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(records):
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
        replay.invalidate_history_head(path)
        return path

    return sid, path, write


def user(text="question", rid="old", channel="web"):
    return {"role": "user", "request_id": rid, "id": rid, "channel_id": channel, "content": text}


def test_large_audit_events_do_not_become_restored_context(stored, monkeypatch):
    sid, path, write = stored
    first = user()
    final = {"role": "assistant", "event_type": "chat.final", "content": "answer"}
    deltas = [{"role": "assistant", "event_type": "chat.delta", "content": "x" * 8192 + "🙂"}] * 128
    write([first, *deltas, {"event_type": "chat.llm_call_start", "prompt": "y" * 8192}, final,
           user("new input", "current")])
    original = Path.read_text

    def no_whole_read(target, *args, **kwargs):
        assert target != path, "model restore must not allocate the entire file"
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_whole_read)
    result = replay.read_history_replay(sid, before_request_id="current",
        limits=replay.HistoryReplayLimits(max_record_bytes=16384, max_retained_bytes=1024))
    assert result.records == [first, final, user("new input", "current")]
    assert result.bytes_read == path.stat().st_size
    assert result.prefix_cursor.offset < result.cursor.offset


@pytest.mark.parametrize("failure", ["line", "bytes", "count", "scan"])
def test_limits_fail_without_returning_a_partial_prefix(stored, failure):
    sid, _, write = stored
    write([user("a" * 200), user("b" * 200, "other")])
    overrides = {"line": {"max_record_bytes": 64}, "bytes": {"max_retained_bytes": 400},
                 "count": {"max_records": 1}, "scan": {"max_scan_bytes": 64}}[failure]
    with pytest.raises(replay.HistoryReplayError, match="HISTORY_RESTORE_LIMIT"):
        replay.read_history_replay(sid, limits=replay.HistoryReplayLimits(**overrides))


def test_memory_error_is_a_resource_failure_not_a_skipped_record(stored, monkeypatch):
    sid, _, write = stored
    write([user(), user("second", "second")])
    loads = replay.json.loads

    def fail_second(value, *args, **kwargs):
        if isinstance(value, bytes) and b'"second"' in value:
            raise MemoryError()
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(replay.json, "loads", fail_second)
    with pytest.raises(replay.HistoryReplayError, match="HISTORY_RESTORE_RESOURCE"):
        replay.read_history_replay(sid)


def test_corrupt_line_never_returns_an_earlier_valid_prefix(stored):
    sid, path, write = stored
    write([user()])
    with path.open("ab") as out:
        out.write(b'{"role":broken}\n')
    with pytest.raises(replay.HistoryReplayError, match="JSONDecodeError"):
        replay.read_history_replay(sid)


def test_voice_cursor_skips_known_text_only_appends_and_reads_only_new_speech(stored, monkeypatch):
    sid, path, write = stored
    write([user(), user("old voice", "voice-old", "video_duplex"), user("new input", "current")])
    restored = replay.read_history_replay(sid, before_request_id="current")
    history._append_record_jsonl(path, {"role": "assistant", "event_type": "chat.final", "content": "typed answer"})
    original = Path.open

    def no_history_scan(target, *args, **kwargs):
        assert target != path, "no new voice must not scan any history body"
        return original(target, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", no_history_scan)
        empty = replay.read_history_replay(sid, cursor=restored.prefix_cursor, voice_only=True,
                                           is_restorable=lambda row: row.get("channel_id") == "video_duplex")
    assert empty.bytes_read == 0 and empty.records == []
    prior_size = path.stat().st_size
    speech = user("new voice", "voice-new", "video_duplex")
    history._append_record_jsonl(path, speech)
    batch = replay.read_history_replay(sid, cursor=empty.cursor, voice_only=True,
                                       is_restorable=lambda row: row.get("channel_id") == "video_duplex")
    assert batch.records == [speech]
    assert batch.bytes_read == path.stat().st_size - prior_size
    assert replay.read_history_replay(sid, cursor=batch.cursor, voice_only=True).bytes_read == 0


def test_external_append_is_not_hidden_by_the_voice_head_cache(stored):
    sid, path, write = stored
    write([user()])
    initial = replay.read_history_replay(sid)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(user("external voice", "external", "video_duplex")) + "\n")
    batch = replay.read_history_replay(sid, cursor=initial.cursor, voice_only=True)
    assert [row["content"] for row in batch.records] == ["external voice"]
    assert batch.bytes_read > 0


def test_windows_fstat_ctime_mismatch_does_not_void_restore(stored, monkeypatch):
    """Windows fstat/path.stat disagree on st_ctime_ns for an unchanged file."""

    sid, path, write = stored
    write([user(), {"role": "assistant", "event_type": "chat.final", "content": "ok"}])
    real_fstat = replay.os.fstat

    def skewed_fstat(fd: int):
        result = real_fstat(fd)
        return SimpleNamespace(
            st_dev=result.st_dev,
            st_ino=result.st_ino,
            st_size=result.st_size,
            st_mtime_ns=result.st_mtime_ns,
            # Skew creation-time like Windows fstat vs Path.stat.
            st_ctime_ns=result.st_ctime_ns + 10_000_000,
        )

    monkeypatch.setattr(replay.os, "fstat", skewed_fstat)
    batch = replay.read_history_replay(sid)
    assert [row.get("content") for row in batch.records] == ["question", "ok"]


def test_append_during_restore_still_rejected(stored, monkeypatch):
    sid, path, write = stored
    write([user()])
    real_readline = None

    class _MutatingHandle:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def seek(self, *args, **kwargs):
            return self._handle.seek(*args, **kwargs)

        def tell(self):
            return self._handle.tell()

        def fileno(self):
            return self._handle.fileno()

        def readline(self, *args, **kwargs):
            line = self._handle.readline(*args, **kwargs)
            if line:
                with path.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(user("racer", "race")) + "\n")
            return line

    original_open = Path.open

    def open_and_race(target, *args, **kwargs):
        handle = original_open(target, *args, **kwargs)
        if target == path and "b" in (args[0] if args else kwargs.get("mode", "r")):
            return _MutatingHandle(handle)
        return handle

    monkeypatch.setattr(Path, "open", open_and_race)
    with pytest.raises(replay.HistoryReplayError, match="HISTORY_RESTORE_CHANGED"):
        replay.read_history_replay(sid)


@pytest.mark.parametrize("change", ["rewrite", "generation"])
def test_changed_history_rejects_a_stale_cursor(stored, change):
    sid, path, write = stored
    write([user()])
    cursor = replay.read_history_replay(sid).cursor
    if change == "rewrite":
        history.write_history_records(sid, [user("replacement")])
    else:
        with lc.resource_lock("session", sid):
            state = lc.state("session", sid)
            state["generation"] = state.get("generation", 0) + 1
            lc.save_locked("session", sid, state)
    with pytest.raises(replay.HistoryReplayError, match="HISTORY_RESTORE_CHANGED"):
        replay.read_history_replay(sid, cursor=cursor)


def test_legacy_history_is_compatible_but_cannot_bypass_the_byte_limit(stored, monkeypatch):
    sid, path, _ = stored
    legacy = path.with_suffix(".json")
    legacy.write_text(json.dumps([user()]), encoding="utf-8")
    assert replay.read_history_replay(sid).records == [user()]
    with pytest.raises(replay.HistoryReplayError, match="HISTORY_RESTORE_LIMIT"):
        replay.read_history_replay(sid, limits=replay.HistoryReplayLimits(max_record_bytes=8))


def test_restore_admission_is_bounded(stored, monkeypatch):
    sid, _, write = stored
    write([user()])
    slots = Mock()
    slots.acquire.return_value = False
    monkeypatch.setattr(replay, "_READ_SLOTS", slots)
    with pytest.raises(replay.HistoryReplayError, match="HISTORY_RESTORE_BUSY"):
        replay.read_history_replay(sid)
    slots.release.assert_not_called()


def test_error_dedup_does_not_materialize_the_audit_file(stored, monkeypatch):
    sid, path, write = stored
    error = {"role": "assistant", "event_type": "chat.error", "request_id": "request", "error": "failed"}
    write([user(), {"event_type": "chat.llm_call_start", "prompt": "x" * 10000}, error])
    monkeypatch.setattr(history, "_read_history_jsonl", lambda _path: pytest.fail("unbounded error dedup"))
    history._write_item(sid, {**error, "cron_failure_record": True})
    assert len([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]) == 3


def test_reply_suggestions_inspect_only_a_bounded_tail(stored, monkeypatch):
    sid, path, write = stored
    completed = {"event_type": "chat.request_completed", "request_id": "done", "status": "success"}
    write([user(), {"event_type": "chat.delta", "content": "🙂" * 20000}, completed])
    monkeypatch.setattr(history, "load_history_records", lambda _sid: pytest.fail("full history suggestion read"))
    assert replay.latest_completed_request_bounded(sid, max_tail_bytes=4096) == "done"
    history._append_record_jsonl(path, user("pending input", "next"))
    assert replay.latest_completed_request_bounded(sid, max_tail_bytes=4096) is None


@pytest.mark.parametrize("value", [0, -1, True])
def test_invalid_budgets_cannot_become_an_unbounded_read(value):
    with pytest.raises(ValueError):
        replay.HistoryReplayLimits(max_record_bytes=value)
    with pytest.raises(ValueError):
        replay.latest_completed_request_bounded("invalid-budget", max_tail_bytes=value)


@pytest.mark.asyncio
async def test_voice_import_failure_rolls_back_context_snapshot_and_cursor(stored, monkeypatch):
    from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig
    from openjiuwen.core.context_engine.token.string_length_counter import StringLengthCounter
    from openjiuwen.core.foundation.llm import UserMessage
    from jiuwenswarm.extensions.video_duplex.backend import conversation_context as sync

    sid, path, write = stored
    write([user("typed input")])
    state = {}
    session = SimpleNamespace(get_session_id=lambda: sid, get_state=state.get, update_state=state.update)
    engine = ContextEngine(ContextEngineConfig(enable_openrouter_model_context_window_tokens=False))
    context = await engine.create_context(session=session, token_counter=StringLengthCounter())
    await context.add_messages(UserMessage(content="typed input"))
    await engine.save_contexts(session)
    context.duplex_history_cursor = replay.read_history_replay(sid).cursor
    old_cursor = context.duplex_history_cursor
    history._append_record_jsonl(path, user("My project is Cedar", "voice-name", "video_duplex"))
    agent = SimpleNamespace(react_agent=SimpleNamespace(context_engine=engine), _interaction_session=session)
    save = engine.save_contexts

    async def fail_after_save(target):
        await save(target)
        raise OSError("snapshot store unavailable")

    monkeypatch.setattr(engine, "save_contexts", fail_after_save)
    with pytest.raises(replay.HistoryReplayError):
        await sync.sync_conversation_context(agent, sid)
    assert context.duplex_history_cursor == old_cursor
    assert [message.content for message in context.get_messages()] == ["typed input"]
    await engine.create_context(session=session)
    assert [message.content for message in context.get_messages()] == ["typed input"]
    monkeypatch.setattr(engine, "save_contexts", save)
    await sync.sync_conversation_context(agent, sid)
    await engine.create_context(session=session)
    assert [message.content for message in context.get_messages()] == ["typed input", "My project is Cedar"]
    await sync.sync_conversation_context(agent, sid)
    assert len(context.get_messages()) == 2
