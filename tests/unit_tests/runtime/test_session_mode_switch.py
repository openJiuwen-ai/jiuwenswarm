"""Explicit TUI mode switches keep one Session and its project index."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock
from weakref import WeakValueDictionary

import psutil
import pytest

from agent_gateway.channel_manager.tui.tui_connect import CLI_FORWARD_REQ_METHODS
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.service import AgentRuntime, SessionModeSwitchError
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.server.runtime.session import lifecycle, session_history, session_metadata


@pytest.fixture(params=[("default_code", ""), ("project-123", "C:/projects/sample")])
def mode_session(tmp_path, monkeypatch, request):
    sessions = tmp_path / "sessions"
    project_id, project_dir = request.param
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path)
    session_metadata.init_session_metadata(
        session_id="one-session",
        channel_id="tui",
        user_id="user-1",
        mode="agent.code.normal",
        project_id=project_id,
        project_dir=project_dir,
        work_mode="code",
    )
    yield project_id, project_dir
    session_metadata._METADATA_QUEUE.join()
    session_metadata.remove_session_metadata_cache("one-session")


def _switch(runtime, target_mode, expected_mode, *, user_id="user-1", channel_id="tui"):
    """Call the public mode.set entry for the fixture Session."""
    return runtime.switch_session_mode(
        session_id="one-session",
        target_mode=target_mode,
        expected_mode=expected_mode,
        channel_id=channel_id,
        user_id=user_id,
    )


def test_mode_set_preserves_project_index_and_blocks_stale_write(mode_session, monkeypatch):
    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    stale = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )

    project_id, project_dir = mode_session
    assert _switch(runtime, "agent.work.normal", "agent.code.normal") == {
        "previous_mode": "agent.code.normal",
        "mode": "agent.work.normal",
        "work_mode": "code",
        "project_id": project_id,
        "changed": True,
    }
    assert [item["mode"] for item in markers] == ["agent.work.normal"]

    stale["mode"] = "agent.code.normal"
    stale["title"] = "late title"
    session_metadata._write_metadata_sync(
        "one-session",
        stale,
        session_metadata._MetadataWriteOptions(merge_fields=frozenset({"mode", "title"})),
    )
    persisted = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )
    assert persisted["mode"] == "agent.work.normal"
    assert persisted["title"] == "late title"
    assert (persisted["project_id"], persisted["project_dir"], persisted["work_mode"]) == (
        project_id, project_dir, "code",
    )

    # A partial payload without session_id must not undo the guard through
    # identity recovery.
    session_metadata._write_metadata_sync(
        "one-session", {"mode": "agent.code.normal", "title": "partial write"},
    )
    persisted = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )
    assert (persisted["mode"], persisted["mode_revision"]) == ("agent.work.normal", 1)

    assert _switch(runtime, "agent.work.normal", "agent.code.normal")["changed"] is False
    assert len(markers) == 1


def test_mode_set_rejects_stale_expectation_busy_and_foreign_user(mode_session):
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    with pytest.raises(SessionModeSwitchError) as conflict:
        _switch(runtime, "team.work.normal", "agent.work.normal")
    assert conflict.value.code == "MODE_CONFLICT"

    with pytest.raises(SessionModeSwitchError) as denied:
        _switch(runtime, "agent.work.normal", "agent.code.normal", user_id="user-2")
    assert denied.value.code == "NOT_FOUND"

    runtime.begin_chat_request("one-session", "in-flight")
    with pytest.raises(SessionModeSwitchError) as busy:
        _switch(runtime, "team.work.normal", "agent.code.normal")
    assert busy.value.code == "SESSION_BUSY"
    runtime.end_chat_request("one-session", "in-flight")

    assert session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )["mode"] == "agent.code.normal"


def test_mode_get_reads_persisted_mode_and_checks_user(mode_session):
    project_id, _project_dir = mode_session
    assert AgentRuntime.get_session_mode(session_id="one-session", user_id="user-1") == {
        "mode": "agent.code.normal",
        "work_mode": "code",
        "project_id": project_id,
    }
    with pytest.raises(SessionModeSwitchError) as denied:
        AgentRuntime.get_session_mode(session_id="one-session", user_id="user-2")
    assert denied.value.code == "NOT_FOUND"


@pytest.mark.parametrize("mode_fields", [
    {}, {"mode": None}, {"mode": "unknown"}, {"mode": ""},
    {"mode": "future.mode"}, {"mode": 1}, {"mode": []}, {"mode": {}},
    {"mode": "team.design"},
])
def test_unknown_mode_requires_explicit_repair(mode_session, monkeypatch, mode_fields):
    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    metadata_path = session_metadata.get_agent_sessions_dir() / "one-session" / "metadata.json"
    stored = json.loads(metadata_path.read_text(encoding="utf-8"))
    stored.pop("mode", None)
    stored.update(mode_fields)
    metadata_path.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(SessionModeSwitchError) as unknown:
        AgentRuntime.get_session_mode(session_id="one-session", user_id="user-1")
    assert unknown.value.code == "MODE_UNKNOWN"
    assert "/mode" in str(unknown.value)
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == stored

    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    with pytest.raises(SessionModeSwitchError) as denied:
        _switch(runtime, "team.work.normal", "agent.code.normal", user_id="user-2")
    assert denied.value.code == "NOT_FOUND"

    runtime.begin_chat_request("one-session", "in-flight")
    with pytest.raises(SessionModeSwitchError) as busy:
        _switch(runtime, "team.work.normal", "agent.code.normal")
    assert busy.value.code == "SESSION_BUSY"
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == stored
    runtime.end_chat_request("one-session", "in-flight")

    result = _switch(runtime, "team.work.normal", "agent.code.normal")
    assert result["mode"] == "team.work.normal"
    assert result["previous_mode"] is None
    assert result["changed"] is True
    persisted = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert persisted["mode"] == "team.work.normal"
    assert persisted["mode_revision"] == 1
    for field in ("project_id", "project_dir", "work_mode"):
        assert persisted[field] == stored[field]
    assert AgentRuntime.get_session_mode(session_id="one-session", user_id="user-1")["mode"] == result["mode"]
    assert len(markers) == 1
    assert markers[0]["previous_mode"] is None

    # A later client cannot use the unknown-route repair to overwrite a known route.
    with pytest.raises(SessionModeSwitchError) as conflict:
        _switch(runtime, "agent.work.normal", "agent.code.normal")
    assert conflict.value.code == "MODE_CONFLICT"


@pytest.mark.parametrize("stored, client", [
    ("agent.code.plan", "agent.code.normal"),
    ("agent.code.normal", "agent.code.plan"),
])
def test_mode_set_accepts_plan_drift_and_keeps_queued_fields(mode_session, monkeypatch, stored, client):
    """Plan state bypasses mode.set, and a commit drops only the queued route."""
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    session_metadata.patch_session_metadata("one-session", {"mode": stored})
    queued = {"title": (10**9, "queued"), "mode": (10**9, stored), "mode_revision": (10**9, 0)}
    with session_metadata._CACHE_LOCK:
        session_metadata._PENDING_FIELDS[session_metadata._metadata_cache_key("one-session")] = queued
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())

    result = _switch(runtime, "team.code.normal", client)
    assert (result["previous_mode"], result["mode"], result["changed"]) == (stored, "team.code.normal", True)
    cached = session_metadata.get_session_metadata("one-session", enable_writeback=False)
    assert (cached["title"], cached["mode"], cached["mode_revision"]) == ("queued", "team.code.normal", 1)
    with pytest.raises(SessionModeSwitchError) as conflict:
        _switch(runtime, "agent.code.normal", "agent.work.plan")
    assert conflict.value.code == "MODE_CONFLICT"


@pytest.mark.parametrize("legacy, canonical", [
    ("team", "team.work.normal"), ("code.team", "team.code.normal"),
    ("code.normal", "agent.code.normal"), ("agent.plan", "agent.work.plan"),
])
def test_legacy_idempotent_mode_set_returns_canonical(mode_session, monkeypatch, legacy, canonical):
    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    session_metadata.patch_session_metadata("one-session", {"mode": legacy})
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    result = _switch(runtime, canonical, canonical)
    assert result["mode"] == canonical
    assert result["previous_mode"] == canonical
    assert result["changed"] is False
    assert AgentRuntime.get_session_mode(session_id="one-session", user_id="user-1")["mode"] == canonical
    metadata_path = session_metadata.get_agent_sessions_dir() / "one-session" / "metadata.json"
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["mode"] == legacy
    assert markers == []


@pytest.mark.parametrize("stored_mode", ["agent.code.normal", "unknown"])
def test_mode_set_rejects_foreign_live_runtime_owner(mode_session, stored_mode):
    session_metadata.patch_session_metadata("one-session", {"mode": stored_mode})
    owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        with lifecycle.resource_lock("session", "one-session"):
            state = lifecycle.state("session", "one-session")
            state["runtime_owner"] = {
                "pid": owner.pid,
                "started_at": psutil.Process(owner.pid).create_time(),
            }
            lifecycle.save_locked("session", "one-session", state)

        runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
        with pytest.raises(SessionModeSwitchError) as busy:
            _switch(runtime, "team.work.normal", "agent.code.normal")
        assert busy.value.code == "SESSION_BUSY"
        assert session_metadata.get_session_metadata(
            "one-session", cache_bust=True, enable_writeback=False,
        )["mode"] == stored_mode
    finally:
        owner.terminate()
        owner.wait(timeout=5)


def test_uncommitted_cross_profile_chat_uses_committed_mode(mode_session, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    _switch(runtime, "agent.work.normal", "agent.code.normal")
    request = AgentRequest(
        request_id="late-chat",
        channel_id="tui",
        session_id="one-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent.code.normal", "work_mode": "code", "query": "hello"},
    )
    normalized = AgentRuntime.normalize_request(request)
    assert normalized.params["mode"] == "agent.work.normal"
    assert normalized.params["work_mode"] == "code"


@pytest.mark.asyncio
async def test_cold_chat_reclassifies_after_remote_switch_before_claim(mode_session, monkeypatch):
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    request = AgentRequest(
        request_id="cold-chat",
        channel_id="tui",
        session_id="one-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent.code.normal", "query": "hello"},
    )
    assert AgentRuntime.session_work_kind(AgentRuntime.normalize_request(request)) is SessionWorkKind.CHAT_UNARY

    async def claim_after_remote_commit(_request):
        session_metadata.compare_and_set_session_mode(
            "one-session", "team.work.normal", "agent.code.normal", user_id="user-1",
        )
        lifecycle.claim_runtime("one-session")

    monkeypatch.setattr(runtime, "_ensure_session_registered", claim_after_remote_commit)
    normalized = await runtime._normalize_tui_chat_after_claim(request)
    assert normalized.params["mode"] == "team.work.normal"
    assert AgentRuntime.session_work_kind(normalized) is SessionWorkKind.TEAM_REQUEST


@pytest.mark.asyncio
async def test_adopted_tui_chat_reads_authoritative_metadata_once(mode_session, monkeypatch):
    from types import SimpleNamespace
    from jiuwenswarm.runtime.session.model import RuntimeSessionState

    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    monkeypatch.setattr(
        runtime._session_coordinator, "snapshot_session",
        lambda _session_id: SimpleNamespace(state=RuntimeSessionState.READY),
    )
    registered = AsyncMock()
    monkeypatch.setattr(runtime, "_ensure_session_registered", registered)
    original_read = session_metadata.get_session_metadata
    reads = []

    def read_metadata(*args, **kwargs):
        reads.append(kwargs)
        return original_read(*args, **kwargs)

    monkeypatch.setattr(session_metadata, "get_session_metadata", read_metadata)
    request = AgentRequest(
        request_id="warm-chat", channel_id="tui", session_id="one-session",
        req_method=ReqMethod.CHAT_SEND, params={"mode": "agent.code.normal", "query": "hello"},
    )
    normalized = await runtime._normalize_tui_chat_after_claim(request)
    assert normalized.params["mode"] == "agent.code.normal"
    assert len(reads) == 1
    assert reads[0]["cache_bust"] is True
    registered.assert_awaited_once_with(request)


def test_mode_set_is_a_forwarded_protocol_method():
    assert ReqMethod("mode.set") is ReqMethod.MODE_SET
    assert ReqMethod("mode.get") is ReqMethod.MODE_GET
    assert "mode.set" in CLI_FORWARD_REQ_METHODS
    assert "mode.get" in CLI_FORWARD_REQ_METHODS


@pytest.mark.asyncio
async def test_mode_get_waits_for_timed_out_mode_sets_during_runtime_start(monkeypatch):
    from jiuwenswarm.server import agent_ws_server

    started = asyncio.Event()
    ready = asyncio.Event()
    stored_mode = "agent.code.normal"
    read_modes = []
    completed_sets = []
    responses = []

    class Runtime:
        async def start(self):
            started.set()
            await ready.wait()

        def switch_session_mode(self, *, target_mode, **_kwargs):
            nonlocal stored_mode
            changed = stored_mode != target_mode
            stored_mode = target_mode
            completed_sets.append(changed)
            return {"mode": stored_mode, "changed": changed}

    def get_session_mode(*, session_id, user_id):
        assert (session_id, user_id) == ("one-session", "user-1")
        assert completed_sets == [True, False]
        read_modes.append(stored_mode)
        return {"mode": stored_mode}

    async def send_wire(_ws, payload):
        responses.append(payload)

    server = object.__new__(agent_ws_server.AgentWebSocketServer)
    server._mode_request_locks = WeakValueDictionary()
    monkeypatch.setattr(server, "_execution_runtime", lambda: Runtime())
    monkeypatch.setattr(AgentRuntime, "get_session_mode", staticmethod(get_session_mode))
    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send_wire)
    send_lock = asyncio.Lock()

    def request(request_id, method):
        return AgentRequest(
            request_id=request_id,
            channel_id="tui",
            session_id="one-session",
            user_id="user-1",
            req_method=method,
            params={
                "session_id": "one-session",
                "mode": "team.work.normal",
                "expected_mode": "agent.code.normal",
            },
        )

    first = asyncio.create_task(server._handle_mode_set(None, request("set-1", ReqMethod.MODE_SET), send_lock))
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(server._handle_mode_set(None, request("set-2", ReqMethod.MODE_SET), send_lock))
    await asyncio.sleep(0)
    read = asyncio.create_task(server._handle_mode_get(None, request("get", ReqMethod.MODE_GET), send_lock))
    await asyncio.sleep(0)
    assert not read.done()
    assert read_modes == []

    ready.set()
    await asyncio.wait_for(asyncio.gather(first, second, read), timeout=1)
    assert read_modes == ["team.work.normal"]
    assert len(responses) == 3


def _capture_transcript_items(monkeypatch, session_history):
    """Capture written transcript items while keeping the metadata write real."""
    items: list[dict[str, object]] = []
    monkeypatch.setattr(
        session_history,
        "_enqueue_history_item",
        lambda _sid, item, **_kwargs: items.append(item),
    )
    return items


def _commit_and_flush(runtime, target_mode="agent.work.normal"):
    _switch(runtime, target_mode, "agent.code.normal")
    session_metadata.flush_pending_writes()


def _persisted_mode():
    metadata = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )
    return metadata["mode"], metadata.get("mode_revision")


def test_late_team_record_keeps_label_but_not_routing_mode(mode_session, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    items = _capture_transcript_items(monkeypatch, session_history)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    _commit_and_flush(runtime)
    assert _persisted_mode() == ("agent.work.normal", 1)

    # A team monitor frame flushed after the switch: ``mode`` here is the
    # transcript label (``_is_team_relevant`` reads it), never a routing
    # decision. Writing it back would resurrect the route mode.set committed.
    session_history.append_history_record(
        session_id="one-session",
        request_id="team-member-1",
        channel_id="tui",
        role="assistant",
        content="",
        timestamp=1_700_000_000.0,
        event_type="chat.tool_call",
        extra={"tool_call": {"name": "team_member", "arguments": {}}},
        mode="team",
    )
    session_metadata.flush_pending_writes()

    assert _persisted_mode() == ("agent.work.normal", 1)
    assert items[-1]["mode"] == "team"


def test_late_subagent_record_cannot_restore_stale_routing_mode(mode_session, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    items = _capture_transcript_items(monkeypatch, session_history)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    _commit_and_flush(runtime)
    assert _persisted_mode() == ("agent.work.normal", 1)

    # agent_observability forwards a sub-agent LLM record into the parent
    # Session without ``subagent_id``, carrying the mode captured when the
    # parent request started — already stale after the switch above.
    session_history.append_history_record(
        session_id="one-session",
        request_id="llm-end-1",
        channel_id="tui",
        role="assistant",
        content="sub-agent llm record",
        timestamp=1_700_000_001.0,
        event_type="llm.end",
        mode="agent.code.normal",
    )
    session_metadata.flush_pending_writes()

    assert _persisted_mode() == ("agent.work.normal", 1)
    assert items[-1]["mode"] == "agent.code.normal"


def test_child_transcript_record_still_ignores_its_subagent_label(mode_session, monkeypatch):
    items = _capture_transcript_items(monkeypatch, session_history)
    before = _persisted_mode()

    session_history.append_history_record(
        session_id="one-session",
        request_id="subagent-1",
        channel_id="subagent",
        role="assistant",
        content="child record",
        timestamp=1_700_000_002.0,
        event_type="chat.tracer_agent",
        subagent_id="child-a",
        mode="subagent",
    )
    session_metadata.flush_pending_writes()

    assert _persisted_mode() == before
    assert items[-1]["mode"] == "subagent"


def _legacy_snapshot(mode="team.plan"):
    """Snapshot shape a read-time migration sees: legacy route, no revision."""
    snapshot = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )
    snapshot["mode"] = mode
    snapshot.pop("mode_revision", None)
    return snapshot


def test_single_read_mode_migration_cannot_undo_a_committed_switch(mode_session, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    stale = _legacy_snapshot()  # read before the switch: legacy "team.plan"
    migrated = dict(stale, mode="team.work.plan")  # what the migration computes

    _commit_and_flush(runtime)  # mode.set lands between read and write-back
    assert _persisted_mode() == ("agent.work.normal", 1)

    # Single-read write-back path: no expected_fields, only the revision guard.
    session_metadata._enqueue_write(
        "one-session", migrated, preserve_pin_fields=True,
        merge_fields=frozenset({"mode"}),
    )
    session_metadata.flush_pending_writes()

    assert _persisted_mode() == ("agent.work.normal", 1)


def test_batch_mode_migration_skips_a_route_changed_after_the_read(mode_session, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    stale = _legacy_snapshot()
    # Equal revision on purpose: only the field-level CAS can reject this write.
    migrated = dict(stale, mode="team.work.plan", mode_revision=1)

    _commit_and_flush(runtime)
    assert _persisted_mode() == ("agent.work.normal", 1)

    session_metadata._enqueue_inference_write("one-session", migrated, {"mode"}, stale)
    session_metadata.flush_pending_writes()

    assert _persisted_mode() == ("agent.work.normal", 1)


def test_team_history_filter_still_reads_the_record_mode():
    relevant = session_history._is_team_relevant
    assert relevant({"event_type": "chat.tool_call", "mode": "team"}) is True
    assert relevant({"event_type": "chat.tool_call", "mode": "team.code.normal"}) is True
    assert relevant({"event_type": "chat.tool_call", "mode": "agent.work.normal"}) is False

@pytest.mark.asyncio
@pytest.mark.parametrize("method,channel_id,params", [
    (ReqMethod.MODE_SET, "web", {"session_id": "one-session", "mode": "team.work.normal"}),
    (ReqMethod.MODE_GET, "web", {"session_id": "one-session"}),
    (ReqMethod.MODE_SET, "tui", {
        "session_id": "one-session", "mode": "agent.work.normal", "expected_mode": 3,
    }),
    (ReqMethod.MODE_SET, "tui", {"session_id": "other-session", "mode": "agent.work.normal"}),
])
async def test_mode_handlers_reject_foreign_channel_and_malformed_params(
    monkeypatch, method, channel_id, params,
):
    """Every entry guard runs before Runtime work, so a bad caller costs nothing."""
    from jiuwenswarm.server import agent_ws_server

    responses = []

    async def send_wire(_ws, payload):
        responses.append(payload)

    server = object.__new__(agent_ws_server.AgentWebSocketServer)
    server._mode_request_locks = WeakValueDictionary()
    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send_wire)
    monkeypatch.setattr(
        agent_ws_server, "encode_agent_response_for_wire",
        lambda response, response_id=None: response,
    )
    monkeypatch.setattr(
        server, "_execution_runtime",
        lambda: pytest.fail("a rejected request must not start the Runtime"),
    )
    request = AgentRequest(
        request_id="reject-1",
        channel_id=channel_id,
        session_id="one-session",
        user_id="user-1",
        req_method=method,
        params=params,
    )
    handler = server._handle_mode_set if method is ReqMethod.MODE_SET else server._handle_mode_get
    await handler(None, request, asyncio.Lock())

    assert len(responses) == 1
    assert responses[0].ok is False
    assert responses[0].payload["code"] == "BAD_REQUEST"
